# -*- coding: utf-8 -*-
"""Engine binary lifecycle: sing-box and Xray-core.

Locate (bundled -> writable work dir, else download from the official release),
chmod +x, launch, monitor, stop, and per-engine config validation. Kodi-free.
"""
import os
import shutil
import stat
import subprocess
import tarfile
import tempfile
import time
import urllib.request
import zipfile

import osarch
import port_utils

SINGBOX_VERSION = "1.13.14"
XRAY_VERSION = "25.8.3"

XRAY_ASSET = {
    "linux_x64": "Xray-linux-64.zip",
    "linux_x86": "Xray-linux-32.zip",
    "linux_arm64": "Xray-linux-arm64-v8a.zip",
    "linux_armv7": "Xray-linux-arm32-v7a.zip",
    "linux_armv6": "Xray-linux-arm32-v6.zip",
    "linux_armv5": "Xray-linux-arm32-v5.zip",
    "windows_x64": "Xray-windows-64.zip",
    "windows_x86": "Xray-windows-32.zip",
    "darwin_x64": "Xray-macos-64.zip",
    "darwin_arm64": "Xray-macos-arm64-v8a.zip",
}


def _noop_log(msg, level="info"):
    pass


class BinaryManager(object):
    def __init__(self, addon_dir, work_dir, engine="sing-box",
                 platform_override="auto", logger=None, custom_path=""):
        self.addon_dir = addon_dir
        self.work_dir = work_dir
        self.engine = engine
        self.log = logger or _noop_log
        self.platform = osarch.get_platform(platform_override)
        self.custom_path = custom_path or ""
        self.proc = None

    # ----- names -----------------------------------------------------
    @property
    def binary_name(self):
        base = "xray" if self.engine == "xray" else "sing-box"
        if self.platform.startswith("windows"):
            return base + ".exe"
        return base

    @property
    def version(self):
        return XRAY_VERSION if self.engine == "xray" else SINGBOX_VERSION

    @property
    def bundled_binary(self):
        return os.path.join(self.addon_dir, "resources", "bin",
                            self.platform, self.binary_name)

    @property
    def work_binary(self):
        return os.path.join(self.work_dir, "bin", self.engine,
                            self.platform, self.binary_name)

    @property
    def work_dir_bin(self):
        return os.path.dirname(self.work_binary)

    # ----- prepare ---------------------------------------------------
    def ensure_binary(self):
        """Return path to a ready-to-run engine binary.

        Priority: custom_path (user-supplied) > bundled > download.
        """
        if self.custom_path:
            custom = self._resolve_custom()
            if custom:
                return custom
            self.log("custom binary path invalid (%s); falling back to bundle/download"
                     % self.custom_path, "warn")

        os.makedirs(self.work_dir_bin, exist_ok=True)
        if self._sync_from_bundle():
            return self.work_binary
        self.log("Bundled %s not found for %s, downloading..." % (self.engine, self.platform))
        self._download_binary()
        if not os.path.exists(self.work_binary):
            raise RuntimeError("%s binary unavailable for platform %s" % (self.engine, self.platform))
        self._make_exec(self.work_binary)
        return self.work_binary

    def _resolve_custom(self):
        """Validate and return the user-supplied custom binary path, else None."""
        path = os.path.expanduser(self.custom_path.strip())
        if not path or not os.path.isfile(path):
            return None
        self._make_exec(path)
        if not self._binary_runs(path):
            return None
        self.log("Using custom %s binary: %s" % (self.engine, path))
        return path

    def _binary_runs(self, path):
        try:
            out = subprocess.run([path, "version"], capture_output=True, timeout=10)
            return out.returncode == 0
        except Exception:
            return False

    def _sync_from_bundle(self):
        src = self.bundled_binary
        if not os.path.exists(src):
            return False
        dst = self.work_binary
        if (not os.path.exists(dst)) or (os.path.getsize(src) != os.path.getsize(dst)):
            self.log("Installing bundled %s (%s) to work dir" % (self.engine, self.platform))
            shutil.copy2(src, dst)
        self._make_exec(dst)
        return True

    def _make_exec(self, path):
        try:
            st = os.stat(path)
            os.chmod(path, st.st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
        except OSError as e:
            self.log("chmod +x failed for %s: %s" % (path, e), "warn")

    def _asset_url(self):
        if self.engine == "xray":
            asset = XRAY_ASSET.get(self.platform)
            if not asset:
                raise ValueError("no xray asset for %s" % self.platform)
            return ("https://github.com/XTLS/Xray-core/releases/download/"
                    "v%s/%s" % (self.version, asset))
        return osarch.asset_url(self.platform, self.version)

    def _download_binary(self):
        url = self._asset_url()
        self.log("Downloading %s" % url)
        tmpdir = tempfile.mkdtemp(prefix="engine-dl-")
        try:
            archive = os.path.join(tmpdir, "pkg.zip" if url.endswith(".zip") else "pkg.tar.gz")
            # The engine provides the proxy itself; downloads must not go
            # through a proxy that may not be running yet.
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(url, timeout=60) as resp, open(archive, "wb") as out:
                shutil.copyfileobj(resp, out)
            extracted = os.path.join(tmpdir, "x")
            os.makedirs(extracted, exist_ok=True)
            if archive.endswith(".zip"):
                with zipfile.ZipFile(archive) as zf:
                    zf.extractall(extracted)
            else:
                with tarfile.open(archive, "r:gz") as tf:
                    tf.extractall(extracted)
            inner = self.binary_name
            candidate = None
            for root, _dirs, files in os.walk(extracted):
                if inner in files:
                    candidate = os.path.join(root, inner)
                    break
            if not candidate:
                raise RuntimeError("binary %s not found in %s" % (inner, url))
            shutil.copy2(candidate, self.work_binary)
            self._make_exec(self.work_binary)
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    # ----- run -------------------------------------------------------
    def is_running(self):
        return self.proc is not None and self.proc.poll() is None

    def start(self, config_path, port=None, ready_timeout=10.0):
        if self.is_running():
            self.log("%s already running (pid %s)" % (self.engine, self.proc.pid))
            return self.proc
        binary = self.ensure_binary()
        args = [binary, "run", "-c", config_path]
        self.log("Starting %s: %s (platform %s)" % (self.engine, " ".join(args), self.platform))

        kwargs = dict(cwd=os.path.dirname(binary), env=os.environ.copy(),
                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if os.name == "nt":
            si = subprocess.STARTUPINFO()
            si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            kwargs["startupinfo"] = si
            if hasattr(subprocess, "CREATE_NO_WINDOW"):
                kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        else:
            kwargs["close_fds"] = True
            kwargs["start_new_session"] = True

        self.proc = subprocess.Popen(args, **kwargs)
        self.log("%s started, pid %s" % (self.engine, self.proc.pid))
        if port is not None and not self._wait_for_readiness(port, ready_timeout):
            self.log("%s not ready on port %s within %ss; stopping spawned process"
                     % (self.engine, port, ready_timeout), "warn")
            self.stop(port=port)
            raise RuntimeError("%s failed to listen on port %s within %ss"
                               % (self.engine, port, ready_timeout))
        return self.proc

    def _wait_for_readiness(self, port, ready_timeout):
        """Poll the listener on `port` while the process stays alive.

        Returns True once the listener is up and the process is alive, False
        on timeout or when the process exits first.
        """
        deadline = time.time() + ready_timeout
        while True:
            if self.proc is None or self.proc.poll() is not None:
                return False
            if port_utils.port_in_use(port):
                return True
            if time.time() >= deadline:
                return False
            time.sleep(0.1)

    def stop(self, port=None, term_timeout=5.0, kill_timeout=5.0, release_timeout=5.0):
        if self.proc is None:
            return True
        if self.is_running():
            self.log("Stopping %s (pid %s)" % (self.engine, self.proc.pid))
            try:
                self.proc.terminate()
                self.proc.wait(timeout=term_timeout)
            except subprocess.TimeoutExpired:
                try:
                    self.proc.kill()
                    self.proc.wait(timeout=kill_timeout)
                except subprocess.TimeoutExpired:
                    self.log("Process %s (pid %s) did not exit after SIGKILL" % (self.engine, self.proc.pid), "warn")
                    return False
                except Exception as e:
                    self.log("Failed to kill process %s (pid %s): %s" % (self.engine, self.proc.pid, e), "warn")
                    return False
            except Exception as e:
                self.log("Failed to terminate process %s (pid %s): %s" % (self.engine, self.proc.pid, e), "warn")
                try:
                    self.proc.kill()
                    self.proc.wait(timeout=kill_timeout)
                except Exception as e:
                    self.log("Failed to kill process %s (pid %s): %s" % (self.engine, self.proc.pid, e), "warn")
                    return False
        self.proc = None
        if port is not None:
            self._wait_for_listener_release(port, release_timeout)
        return True

    def _wait_for_listener_release(self, port, release_timeout):
        """Poll until the listener on `port` is released or the bound elapses.

        A timeout is logged but non-fatal: another process may legitimately
        own the port. Returns True when the listener is free.
        """
        deadline = time.time() + release_timeout
        while True:
            if not port_utils.port_in_use(port):
                return True
            if time.time() >= deadline:
                break
            time.sleep(0.1)
        self.log("Listener on port %s still busy after %ss" % (port, release_timeout), "warn")
        return False

    def restart(self, config_path):
        self.stop()
        return self.start(config_path)

    def check(self, config_path):
        """Validate config. Returns (ok, output)."""
        binary = self.ensure_binary()
        if self.engine == "xray":
            args = [binary, "run", "-test", "-c", config_path]
        else:
            args = [binary, "check", "-c", config_path]
        try:
            out = subprocess.run(args, capture_output=True, text=True, timeout=30)
            ok = out.returncode == 0
            return ok, (out.stdout + out.stderr)
        except Exception as e:
            return False, str(e)
