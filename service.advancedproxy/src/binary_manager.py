# -*- coding: utf-8 -*-
"""sing-box binary lifecycle: locate, (optionally) download, launch, monitor, stop.

Kodi-free core: everything here runs on plain stdlib so it can be tested
outside Kodi. Logging is injected via a logger callable.
"""
import os
import shutil
import stat
import subprocess
import tarfile
import tempfile
import urllib.request
import zipfile

import osarch

SINGBOX_VERSION = "1.13.14"


def _noop_log(msg, level="info"):
    pass


class BinaryManager(object):
    def __init__(self, addon_dir, work_dir, platform_override="auto",
                 logger=None, version=SINGBOX_VERSION):
        self.addon_dir = addon_dir
        self.work_dir = work_dir
        self.version = version
        self.log = logger or _noop_log
        self.platform = osarch.get_platform(platform_override)
        self.proc = None

    # ----- locations -------------------------------------------------
    @property
    def bundled_binary(self):
        """Binary shipped inside the addon zip: resources/bin/<platform>/sing-box"""
        return os.path.join(
            self.addon_dir, "resources", "bin", self.platform,
            osarch.binary_filename(self.platform))

    @property
    def work_binary(self):
        """Writable copy in the addon profile/work dir (exec-capable location)."""
        return os.path.join(
            self.work_dir, "bin", self.platform,
            osarch.binary_filename(self.platform))

    @property
    def work_dir_bin(self):
        return os.path.dirname(self.work_binary)

    # ----- prepare ---------------------------------------------------
    def ensure_binary(self):
        """Return path to a ready-to-run sing-box binary.

        Preference: bundled binary copied into the writable work dir (addon
        dir may be read-only / inside a zip). If absent, download from the
        official sing-box release for this platform.
        """
        os.makedirs(self.work_dir_bin, exist_ok=True)

        if self._sync_from_bundle():
            return self.work_binary

        self.log("Bundled binary not found for %s, downloading..." % self.platform)
        self._download_binary()
        if not os.path.exists(self.work_binary):
            raise RuntimeError("sing-box binary unavailable for platform %s" % self.platform)
        self._make_exec(self.work_binary)
        return self.work_binary

    def _sync_from_bundle(self):
        """Copy bundled binary -> work dir if newer/different. Returns True if present."""
        src = self.bundled_binary
        if not os.path.exists(src):
            return False
        dst = self.work_binary
        if (not os.path.exists(dst)) or (not self._same_file(src, dst)):
            self.log("Installing bundled sing-box (%s) to work dir" % self.platform)
            shutil.copy2(src, dst)
        self._make_exec(dst)
        return True

    @staticmethod
    def _same_file(a, b):
        try:
            return os.path.getsize(a) == os.path.getsize(b)
        except OSError:
            return False

    def _make_exec(self, path):
        try:
            st = os.stat(path)
            os.chmod(path, st.st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
        except OSError as e:
            self.log("chmod +x failed for %s: %s" % (path, e), "warn")

    def _download_binary(self):
        url = osarch.asset_url(self.platform, self.version)
        self.log("Downloading %s" % url)
        tmpdir = tempfile.mkdtemp(prefix="singbox-dl-")
        try:
            if url.endswith(".zip"):
                archive = os.path.join(tmpdir, "sing-box.zip")
            else:
                archive = os.path.join(tmpdir, "sing-box.tar.gz")
            urllib.request.urlretrieve(url, archive)

            inner = osarch.binary_filename(self.platform)
            member_dir = osarch.asset_name(self.platform, self.version)
            extracted = os.path.join(tmpdir, "x")
            os.makedirs(extracted, exist_ok=True)
            if archive.endswith(".zip"):
                with zipfile.ZipFile(archive) as zf:
                    zf.extractall(extracted)
            else:
                with tarfile.open(archive, "r:gz") as tf:
                    tf.extractall(extracted)
            candidate = os.path.join(extracted, member_dir, inner)
            if not os.path.exists(candidate):
                # fallback: search recursively
                for root, _dirs, files in os.walk(extracted):
                    if inner in files:
                        candidate = os.path.join(root, inner)
                        break
            shutil.copy2(candidate, self.work_binary)
            self._make_exec(self.work_binary)
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    # ----- run -------------------------------------------------------
    def is_running(self):
        return self.proc is not None and self.proc.poll() is None

    def start(self, config_path):
        """Start sing-box with the given config. Returns the Popen object."""
        if self.is_running():
            self.log("sing-box already running (pid %s)" % self.proc.pid)
            return self.proc
        binary = self.ensure_binary()
        args = [binary, "run", "-c", config_path]
        self.log("Starting sing-box: %s (platform %s)" % (" ".join(args), self.platform))

        env = os.environ.copy()
        kwargs = dict(
            cwd=os.path.dirname(binary),
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if os.name == "nt":
            si = subprocess.STARTUPINFO()
            si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            kwargs["startupinfo"] = si
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0
        else:
            kwargs["close_fds"] = True
            kwargs["start_new_session"] = True

        self.proc = subprocess.Popen(args, **kwargs)
        self.log("sing-box started, pid %s" % self.proc.pid)
        return self.proc

    def stop(self):
        if self.proc is None:
            return
        if self.is_running():
            self.log("Stopping sing-box (pid %s)" % self.proc.pid)
            try:
                self.proc.terminate()
                self.proc.wait(timeout=5)
            except Exception:
                try:
                    self.proc.kill()
                except Exception:
                    pass
        self.proc = None

    def restart(self, config_path):
        self.stop()
        return self.start(config_path)

    def check(self, config_path):
        """Validate a config with `sing-box check`. Returns (ok, output)."""
        binary = self.ensure_binary()
        try:
            out = subprocess.run(
                [binary, "check", "-c", config_path],
                capture_output=True, text=True, timeout=30)
            return out.returncode == 0, (out.stdout + out.stderr)
        except Exception as e:
            return False, str(e)
