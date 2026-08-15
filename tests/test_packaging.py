import hashlib
import os
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import unittest
import warnings
import zipfile


HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, ".."))
ADDON = "service.advancedproxy"
VERSION = "0.4.8"
SB_VERSION = "1.13.15"
XR_VERSION = "26.7.28"
UNIVERSAL_PLATFORMS = (
    "android_arm64", "darwin_arm64", "darwin_x64", "linux_arm64",
    "linux_armv7", "linux_x64", "linux_x86", "windows_x64",
)
ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)
EXECUTABLES = ("sing-box", "sing-box.exe", "xray", "xray.exe")


def write(path, content, mode="w"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, mode, encoding=None if "b" in mode else "utf-8") as stream:
        stream.write(content)


class PackagingFixture:
    def __init__(self, root):
        self.root = root
        self.repo = os.path.join(root, "repo")
        self.dist = os.path.join(self.repo, "dist")
        self._copy_source_repo()

    def _copy_source_repo(self):
        os.makedirs(self.repo)
        for name in ("build.sh", "LICENSE", "THIRD_PARTY_NOTICES.md"):
            shutil.copy2(os.path.join(REPO, name), os.path.join(self.repo, name))
        shutil.copytree(
            os.path.join(REPO, ADDON), os.path.join(self.repo, ADDON),
            ignore=shutil.ignore_patterns("bin", "__pycache__", "*.pyc"),
        )
        os.makedirs(os.path.join(self.repo, "scripts"))
        for name in ("verify_zip.sh", "check_versions.sh", "make_universal.py"):
            shutil.copy2(
                os.path.join(REPO, "scripts", name),
                os.path.join(self.repo, "scripts", name),
            )

    def source(self, relative):
        with open(os.path.join(self.repo, relative), "rb") as stream:
            return stream.read()

    def zip_path(self, platform="linux_x64", version=VERSION):
        return os.path.join(
            self.root, "%s-%s.%s.zip" % (ADDON, version, platform)
        )

    def make_zip(self, platform="linux_x64", version=VERSION, mutation=None,
                 into=None):
        windows = platform.startswith("windows_")
        xray_expected = platform != "android_arm64"
        prefix = "%s/resources/bin/%s" % (ADDON, platform)
        entries = {
            "%s/addon.xml" % ADDON: self.source("%s/addon.xml" % ADDON),
            "%s/LICENSE" % ADDON: self.source("LICENSE"),
            "%s/THIRD_PARTY_NOTICES.md" % ADDON: self.source("THIRD_PARTY_NOTICES.md"),
            "%s/resources/licenses/sing-box/LICENSE" % ADDON:
                self.source("%s/resources/licenses/sing-box/LICENSE" % ADDON),
            "%s/resources/licenses/sing-box/NOTICE" % ADDON:
                self.source("%s/resources/licenses/sing-box/NOTICE" % ADDON),
            "%s/resources/licenses/xray/LICENSE" % ADDON:
                self.source("%s/resources/licenses/xray/LICENSE" % ADDON),
            "%s/%s" % (prefix, "sing-box.exe" if windows else "sing-box"): b"sb\n",
            "%s/version" % prefix: (SB_VERSION + "\n").encode(),
            "%s/sing-box-LICENSE" % prefix:
                self.source("%s/resources/licenses/sing-box/LICENSE" % ADDON),
            "%s/sing-box-NOTICE" % prefix:
                self.source("%s/resources/licenses/sing-box/NOTICE" % ADDON),
        }
        if xray_expected:
            entries["%s/%s" % (prefix, "xray.exe" if windows else "xray")] = b"xr\n"
            entries["%s/xray_version" % prefix] = (XR_VERSION + "\n").encode()
            entries["%s/xray-LICENSE" % prefix] = self.source(
                "%s/resources/licenses/xray/LICENSE" % ADDON
            )
            entries["%s/geoip.dat" % prefix] = b"geoip\n"
            entries["%s/geosite.dat" % prefix] = b"geosite\n"
        if mutation:
            mutation(entries, prefix)
        if into is None:
            path = self.zip_path(platform, version)
        else:
            path = os.path.join(into, "%s-%s.%s.zip" % (ADDON, version, platform))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
            for name, content in entries.items():
                archive.writestr(name, content)
        return path

    def make_platform_zips(self, version=VERSION, mutation=None,
                           platforms=UNIVERSAL_PLATFORMS):
        return [self.make_zip(platform, version, mutation, into=self.dist)
                for platform in platforms]

    def platform_zip_path(self, platform, version=VERSION):
        return os.path.join(self.dist, "%s-%s.%s.zip" % (ADDON, version, platform))

    def universal_path(self, version=VERSION):
        return os.path.join(self.dist, "%s-%s.zip" % (ADDON, version))

    def make_universal(self, *options):
        return subprocess.run(
            [sys.executable,
             os.path.join(self.repo, "scripts", "make_universal.py"), *options],
            cwd=self.repo, capture_output=True, text=True,
        )

    def verify_universal(self, path, version=VERSION, *options):
        return subprocess.run(
            [os.path.join(self.repo, "scripts", "verify_zip.sh"), "--universal",
             *options, path, version],
            cwd=self.repo, capture_output=True, text=True,
        )

    def rewrite_zip(self, path, mutation):
        """Rebuild PATH from its own payload after MUTATION edited the entries."""
        with zipfile.ZipFile(path) as archive:
            entries = {info.filename: archive.read(info.filename)
                       for info in archive.infolist()}
        mutation(entries)
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
            for name in sorted(entries):
                archive.writestr(name, entries[name])
        return path

    def restamp_zip(self, path):
        """Rewrite PATH with reversed order, alien timestamps, modes, compression."""
        with zipfile.ZipFile(path) as archive:
            entries = [(info.filename, archive.read(info.filename))
                       for info in archive.infolist()]
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
            for name, payload in reversed(entries):
                info = zipfile.ZipInfo(name, (2001, 2, 3, 4, 5, 6))
                info.compress_type = zipfile.ZIP_STORED
                info.create_system = 0
                info.external_attr = 0o100777 << 16
                archive.writestr(info, payload)
        return path

    def verify(self, path, platform="linux_x64", version=VERSION, *options):
        return subprocess.run(
            [os.path.join(self.repo, "scripts", "verify_zip.sh"), *options,
             path, platform, version],
            cwd=self.repo, capture_output=True, text=True,
        )

    def install_fake_curl(self, fail=""):
        tools = os.path.join(self.root, "tools")
        os.makedirs(tools, exist_ok=True)
        sb_archive = os.path.join(self.root, "sing-box.tar.gz")
        sb_mismatch_archive = os.path.join(self.root, "sing-box-mismatch.tar.gz")
        sb_symlink_archive = os.path.join(self.root, "sing-box-symlink.tar.gz")
        sb_win_archive = os.path.join(self.root, "sing-box-windows.zip")
        xr_archive = os.path.join(self.root, "xray.zip")
        xr_win_archive = os.path.join(self.root, "xray-windows.zip")
        payload = os.path.join(self.root, "archive-payload")
        os.makedirs(payload, exist_ok=True)
        write(os.path.join(payload, "sing-box"), b"sing-box\n", "wb")
        with tarfile.open(sb_archive, "w:gz") as archive:
            archive.add(os.path.join(payload, "sing-box"), arcname="release/sing-box")
        write(os.path.join(payload, "sing-box-mismatch"), b"different sing-box\n", "wb")
        with tarfile.open(sb_mismatch_archive, "w:gz") as archive:
            archive.add(
                os.path.join(payload, "sing-box-mismatch"),
                arcname="release/sing-box",
            )
        secret = os.path.join(self.root, "must-not-be-packaged")
        write(secret, b"private fixture data\n", "wb")
        with tarfile.open(sb_symlink_archive, "w:gz") as archive:
            link = tarfile.TarInfo("release/sing-box")
            link.type = tarfile.SYMTYPE
            link.linkname = secret
            archive.addfile(link)
        with zipfile.ZipFile(sb_win_archive, "w") as archive:
            archive.writestr("release/sing-box.exe", b"sing-box\n")
        with zipfile.ZipFile(xr_archive, "w") as archive:
            archive.writestr("xray", b"xray\n")
            archive.writestr("geoip.dat", b"geoip\n")
            archive.writestr("geosite.dat", b"geosite\n")
        with zipfile.ZipFile(xr_win_archive, "w") as archive:
            archive.writestr("xray.exe", b"xray\n")
            archive.writestr("geoip.dat", b"geoip\n")
            archive.writestr("geosite.dat", b"geosite\n")

        def digest(path):
            with open(path, "rb") as stream:
                return hashlib.sha256(stream.read()).hexdigest()

        build_path = os.path.join(self.repo, "build.sh")
        with open(build_path, encoding="utf-8") as stream:
            build_source = stream.read()
        fake_singbox_digests = {
            "linux-amd64.tar.gz": digest(sb_archive),
            "linux-386.tar.gz": digest(sb_archive),
            "linux-arm64.tar.gz": digest(sb_archive),
            "linux-armv7-glibc.tar.gz": digest(sb_archive),
            "android-arm64.tar.gz": digest(sb_archive),
            "windows-amd64.zip": digest(sb_win_archive),
            "darwin-amd64.tar.gz": digest(sb_archive),
            "darwin-arm64.tar.gz": digest(sb_archive),
        }

        def replace_test_digest(match):
            asset = match.group(2)
            return match.group(1) + fake_singbox_digests[asset] + match.group(3)

        build_source = re.sub(
            r'(\["1\.13\.15\|([^\"]+)"\]=")[0-9a-f]{64}(")',
            replace_test_digest,
            build_source,
        )
        write(build_path, build_source)
        fake = os.path.join(tools, "curl")
        write(fake, """#!/bin/sh
out=""
url=""
while [ "$#" -gt 0 ]; do
  if [ "$1" = "-o" ]; then out="$2"; shift 2; else url="$1"; shift; fi
done
case "$url" in
  *XTLS*windows*.dgst)
    [ "$FAKE_CURL_FAIL" = "xray-digest-missing" ] && exit 22
    [ "$FAKE_CURL_FAIL" = "xray-digest-malformed" ] && { printf malformed > "$out"; exit 0; }
    [ "$FAKE_CURL_FAIL" = "xray-digest-mismatch" ] && { printf 'SHA2-256= %064d\n' 0 > "$out"; exit 0; }
    printf 'SHA2-256= %s\n' "$FAKE_XR_WIN_SHA256" > "$out" ;;
  *XTLS*.dgst)
    [ "$FAKE_CURL_FAIL" = "xray-digest-missing" ] && exit 22
    [ "$FAKE_CURL_FAIL" = "xray-digest-malformed" ] && { printf malformed > "$out"; exit 0; }
    [ "$FAKE_CURL_FAIL" = "xray-digest-mismatch" ] && { printf 'SHA2-256= %064d\n' 0 > "$out"; exit 0; }
    printf 'SHA2-256= %s\n' "$FAKE_XR_SHA256" > "$out" ;;
  *SagerNet*windows*)
    [ "$FAKE_CURL_FAIL" = "sing-box" ] && exit 22
    [ "$FAKE_CURL_FAIL" = "extract-sing-box" ] && { printf corrupt > "$out"; exit 0; }
    cp "$FAKE_SB_WIN_ARCHIVE" "$out" ;;
  *SagerNet*)
    [ "$FAKE_CURL_FAIL" = "sing-box" ] && exit 22
    [ "$FAKE_CURL_FAIL" = "extract-sing-box" ] && { printf corrupt > "$out"; exit 0; }
    [ "$FAKE_CURL_FAIL" = "sing-box-digest-mismatch" ] && { cp "$FAKE_SB_MISMATCH_ARCHIVE" "$out"; exit 0; }
    [ "$FAKE_CURL_FAIL" = "symlink-sing-box" ] && { cp "$FAKE_SB_SYMLINK_ARCHIVE" "$out"; exit 0; }
    cp "$FAKE_SB_ARCHIVE" "$out" ;;
  *XTLS*windows*)
    [ "$FAKE_CURL_FAIL" = "xray" ] && exit 22
    [ "$FAKE_CURL_FAIL" = "extract-xray" ] && { printf corrupt > "$out"; exit 0; }
    cp "$FAKE_XR_WIN_ARCHIVE" "$out" ;;
  *XTLS*)
    [ "$FAKE_CURL_FAIL" = "xray" ] && exit 22
    [ "$FAKE_CURL_FAIL" = "extract-xray" ] && { printf corrupt > "$out"; exit 0; }
    cp "$FAKE_XR_ARCHIVE" "$out" ;;
  *) exit 22 ;;
esac
""")
        os.chmod(fake, os.stat(fake).st_mode | stat.S_IXUSR)
        env = os.environ.copy()
        env.update({
            "PATH": tools + os.pathsep + env["PATH"],
            "FAKE_CURL_FAIL": fail,
            "FAKE_SB_ARCHIVE": sb_archive,
            "FAKE_SB_MISMATCH_ARCHIVE": sb_mismatch_archive,
            "FAKE_SB_SYMLINK_ARCHIVE": sb_symlink_archive,
            "FAKE_SB_WIN_ARCHIVE": sb_win_archive,
            "FAKE_XR_ARCHIVE": xr_archive,
            "FAKE_XR_WIN_ARCHIVE": xr_win_archive,
            "FAKE_XR_SHA256": digest(xr_archive),
            "FAKE_XR_WIN_SHA256": digest(xr_win_archive),
        })
        return env


class TestZipVerifier(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="packaging-")
        self.addCleanup(shutil.rmtree, self.tmp)
        self.fixture = PackagingFixture(self.tmp)

    def assertRejected(self, path, platform="linux_x64", version=VERSION):
        result = self.fixture.verify(path, platform, version)
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_good_zip_passes_positional_contract(self):
        result = self.fixture.verify(self.fixture.make_zip())
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_incomplete_zip_is_rejected(self):
        def incomplete(entries, prefix):
            del entries[prefix + "/version"]
        self.assertRejected(self.fixture.make_zip(mutation=incomplete))

    def test_mixed_platform_zip_is_rejected(self):
        def mixed(entries, _prefix):
            entries[ADDON + "/resources/bin/windows_x64/sing-box.exe"] = b"mixed"
        self.assertRejected(self.fixture.make_zip(mutation=mixed))

    def test_corrupt_zip_is_rejected(self):
        path = self.fixture.zip_path()
        write(path, b"not a zip", "wb")
        self.assertRejected(path)

    def test_wrong_stamps_and_addon_version_are_rejected(self):
        def wrong(entries, prefix):
            entries[prefix + "/version"] = b"0.0.0\n"
            entries[prefix + "/xray_version"] = b"0.0.0\n"
            entries[ADDON + "/addon.xml"] = entries[ADDON + "/addon.xml"].replace(
                b'version="%s"' % VERSION.encode(), b'version="9.9.9"'
            )
        self.assertRejected(self.fixture.make_zip(mutation=wrong))

    def test_platform_executable_suffix_is_exact(self):
        def linux_exe(entries, prefix):
            entries[prefix + "/sing-box.exe"] = entries.pop(prefix + "/sing-box")
            entries[prefix + "/xray.exe"] = entries.pop(prefix + "/xray")
        self.assertRejected(self.fixture.make_zip(mutation=linux_exe))

        def windows_plain(entries, prefix):
            entries[prefix + "/sing-box"] = entries.pop(prefix + "/sing-box.exe")
            entries[prefix + "/xray"] = entries.pop(prefix + "/xray.exe")
        path = self.fixture.make_zip("windows_x64", mutation=windows_plain)
        self.assertRejected(path, "windows_x64")

    def test_alternate_executable_duplicate_is_rejected(self):
        def duplicate(entries, prefix):
            entries[prefix + "/sing-box.exe"] = b"alternate"
            entries[prefix + "/xray.exe"] = b"alternate"
        self.assertRejected(self.fixture.make_zip(mutation=duplicate))

    def test_tampered_root_canonical_and_beside_notices_are_rejected(self):
        targets = (
            ADDON + "/LICENSE",
            ADDON + "/THIRD_PARTY_NOTICES.md",
            ADDON + "/resources/licenses/sing-box/LICENSE",
            ADDON + "/resources/licenses/sing-box/NOTICE",
            ADDON + "/resources/licenses/xray/LICENSE",
            ADDON + "/resources/bin/linux_x64/sing-box-LICENSE",
            ADDON + "/resources/bin/linux_x64/sing-box-NOTICE",
            ADDON + "/resources/bin/linux_x64/xray-LICENSE",
        )
        for target in targets:
            with self.subTest(target=target):
                def tamper(entries, _prefix, target=target):
                    entries[target] = b"tampered but non-empty\n"
                self.assertRejected(self.fixture.make_zip(mutation=tamper))

    def test_xray_is_optional_only_without_map_entry(self):
        path = self.fixture.make_zip("android_arm64")
        result = self.fixture.verify(path, "android_arm64")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

        def unexpected(entries, prefix):
            entries[prefix + "/xray"] = b"xray"
            entries[prefix + "/xray_version"] = (XR_VERSION + "\n").encode()
            entries[prefix + "/xray-LICENSE"] = entries[
                ADDON + "/resources/licenses/xray/LICENSE"
            ]
        self.assertRejected(
            self.fixture.make_zip("android_arm64", mutation=unexpected),
            "android_arm64",
        )

    def test_platform_and_version_arguments_must_match_filename(self):
        path = self.fixture.make_zip()
        self.assertRejected(path, "windows_x64")
        self.assertRejected(path, "linux_x64", "9.9.9")

    def test_extra_positional_argument_is_a_usage_error(self):
        result = self.fixture.verify(self.fixture.make_zip(), "linux_x64", VERSION, "extra")
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)

    def test_duplicate_archive_entries_are_rejected(self):
        prefix = "%s/resources/bin/linux_x64" % ADDON
        for entry in (prefix + "/version", prefix + "/sing-box",
                      ADDON + "/LICENSE", ADDON + "/addon.xml"):
            with self.subTest(entry=entry):
                source = self.fixture.make_zip()
                duplicated = os.path.join(self.tmp, "dup",
                                          os.path.basename(source))
                os.makedirs(os.path.dirname(duplicated), exist_ok=True)
                shutil.copy(source, duplicated)
                with zipfile.ZipFile(source) as archive:
                    payload = archive.read(entry)
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore", UserWarning)
                    with zipfile.ZipFile(duplicated, "a") as archive:
                        archive.writestr(entry, payload)
                self.assertRejected(duplicated)
                os.remove(duplicated)


class TestPackagingBuild(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="packaging-build-")
        self.addCleanup(shutil.rmtree, self.tmp)
        self.fixture = PackagingFixture(self.tmp)

    def run_build(self, *args, fail=""):
        return subprocess.run(
            [os.path.join(self.fixture.repo, "build.sh"), *args],
            cwd=self.fixture.repo,
            env=self.fixture.install_fake_curl(fail=fail),
            capture_output=True,
            text=True,
        )

    def test_source_only_check_allows_clean_checkout_without_bin(self):
        shutil.rmtree(os.path.join(self.fixture.repo, ADDON, "resources", "bin"),
                      ignore_errors=True)
        result = subprocess.run(
            [os.path.join(self.fixture.repo, "scripts", "check_versions.sh"),
             self.fixture.repo],
            capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_singbox_download_failure_returns_nonzero_and_no_zip(self):
        result = self.run_build("linux_x64", fail="sing-box")
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(os.path.exists(os.path.join(
            self.fixture.dist, "%s-%s.linux_x64.zip" % (ADDON, VERSION)
        )))

    def test_mapped_xray_download_failure_returns_nonzero_and_no_zip(self):
        result = self.run_build("linux_x64", fail="xray")
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(os.path.exists(os.path.join(
            self.fixture.dist, "%s-%s.linux_x64.zip" % (ADDON, VERSION)
        )))

    def test_extraction_failure_returns_nonzero_and_no_zip(self):
        result = self.run_build("linux_x64", fail="extract-xray")
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(os.path.exists(os.path.join(
            self.fixture.dist, "%s-%s.linux_x64.zip" % (ADDON, VERSION)
        )))

    def test_package_failure_returns_nonzero_and_no_zip(self):
        env = self.fixture.install_fake_curl()
        write(os.path.join(self.tmp, "tools", "zip"), "#!/bin/sh\nexit 23\n")
        os.chmod(os.path.join(self.tmp, "tools", "zip"), stat.S_IRWXU)
        result = subprocess.run(
            [os.path.join(self.fixture.repo, "build.sh"), "linux_x64"],
            cwd=self.fixture.repo, env=env, capture_output=True, text=True,
        )
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(os.path.exists(os.path.join(
            self.fixture.dist, "%s-%s.linux_x64.zip" % (ADDON, VERSION)
        )))

    def test_verifier_failure_returns_nonzero_and_removes_zip(self):
        verifier = os.path.join(self.fixture.repo, "scripts", "verify_zip.sh")
        write(verifier, "#!/bin/sh\nexit 24\n")
        os.chmod(verifier, stat.S_IRWXU)
        result = self.run_build("linux_x64")
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(os.path.exists(os.path.join(
            self.fixture.dist, "%s-%s.linux_x64.zip" % (ADDON, VERSION)
        )))

    def test_unpinned_singbox_version_override_is_rejected(self):
        result = self.run_build("--version", "9.9.9", "linux_x64")
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("no pinned sing-box checksum", result.stderr)

    def test_unknown_platform_is_rejected_before_cleanup(self):
        sentinel = os.path.join(self.fixture.repo, ADDON, "outside")
        write(os.path.join(sentinel, "sentinel"), "preserve me\n")
        result = self.run_build("../../outside")
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(os.path.isfile(os.path.join(sentinel, "sentinel")))

    def test_symlink_binary_archive_is_rejected(self):
        result = self.run_build("linux_x64", fail="symlink-sing-box")
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(os.path.exists(os.path.join(
            self.fixture.dist, "%s-%s.linux_x64.zip" % (ADDON, VERSION)
        )))

    def test_singbox_digest_mismatch_is_rejected(self):
        result = self.run_build("linux_x64", fail="sing-box-digest-mismatch")
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("sing-box checksum mismatch", result.stderr)

    def test_xray_digest_is_required_well_formed_and_matching(self):
        for failure in (
            "xray-digest-missing",
            "xray-digest-malformed",
            "xray-digest-mismatch",
        ):
            with self.subTest(failure=failure):
                result = self.run_build("linux_x64", fail=failure)
                self.assertNotEqual(
                    result.returncode, 0, result.stdout + result.stderr
                )

    def test_windows_build_bundles_exe_names_only(self):
        result = self.run_build("windows_x64")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        path = os.path.join(
            self.fixture.dist, "%s-%s.windows_x64.zip" % (ADDON, VERSION)
        )
        prefix = "%s/resources/bin/windows_x64/" % ADDON
        with zipfile.ZipFile(path) as archive:
            names = set(archive.namelist())
        self.assertIn(prefix + "sing-box.exe", names)
        self.assertIn(prefix + "xray.exe", names)
        self.assertNotIn(prefix + "sing-box", names)
        self.assertNotIn(prefix + "xray", names)
        verified = self.fixture.verify(path, "windows_x64", VERSION)
        self.assertEqual(verified.returncode, 0, verified.stdout + verified.stderr)

    def test_build_without_mapped_xray_asset_succeeds_without_xray(self):
        result = self.run_build("android_arm64")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        path = os.path.join(
            self.fixture.dist, "%s-%s.android_arm64.zip" % (ADDON, VERSION)
        )
        prefix = "%s/resources/bin/android_arm64/" % ADDON
        with zipfile.ZipFile(path) as archive:
            names = set(archive.namelist())
        self.assertIn(prefix + "sing-box", names)
        self.assertNotIn(prefix + "xray", names)
        self.assertNotIn(prefix + "xray_version", names)
        self.assertNotIn(prefix + "xray-LICENSE", names)
        verified = self.fixture.verify(path, "android_arm64", VERSION)
        self.assertEqual(verified.returncode, 0, verified.stdout + verified.stderr)

    def test_addon_version_override_stages_consistent_verified_zip(self):
        addon_xml = os.path.join(self.fixture.repo, ADDON, "addon.xml")
        with open(addon_xml, "rb") as stream:
            original = stream.read()
        result = self.run_build("--addon-version", "9.9.9", "linux_x64")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        path = os.path.join(
            self.fixture.dist, "%s-9.9.9.linux_x64.zip" % ADDON
        )
        verified = self.fixture.verify(path, "linux_x64", "9.9.9")
        self.assertEqual(verified.returncode, 0, verified.stdout + verified.stderr)
        with open(addon_xml, "rb") as stream:
            self.assertEqual(stream.read(), original)
        with zipfile.ZipFile(path) as archive:
            self.assertIn(b'version="9.9.9"', archive.read(ADDON + "/addon.xml"))


class TestUniversalAssembly(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="packaging-universal-")
        self.addCleanup(shutil.rmtree, self.tmp)
        self.fixture = PackagingFixture(self.tmp)

    def assemble(self, *options, **kwargs):
        self.fixture.make_platform_zips(**kwargs)
        return self.fixture.make_universal(*options)

    def assertNoUniversal(self, result, version=VERSION):
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(os.path.exists(self.fixture.universal_path(version)),
                         "a rejected assembly still wrote an output zip")
        leftovers = [name for name in os.listdir(self.fixture.dist)
                     if name.endswith(".tmp")]
        self.assertEqual(leftovers, [])
        self.assertIn("make_universal:", result.stderr)
        self.assertNotIn("Traceback", result.stderr,
                         "refusal crashed instead of diagnosing")

    def test_build_sh_still_pins_the_eight_universal_platforms(self):
        with open(os.path.join(REPO, "build.sh"), encoding="utf-8") as stream:
            line = [row for row in stream if row.startswith("PLATFORMS=(")][0]
        declared = line.strip()[len("PLATFORMS=("):-1].split()
        self.assertEqual(sorted(declared), sorted(UNIVERSAL_PLATFORMS))

    def test_merges_every_platform_tree_into_one_verified_zip(self):
        result = self.assemble()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        path = self.fixture.universal_path()
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
            addon_xml = archive.read(ADDON + "/addon.xml")
        self.assertEqual(len(names), len(set(names)))
        self.assertEqual(addon_xml, self.fixture.source(ADDON + "/addon.xml"))
        for platform in UNIVERSAL_PLATFORMS:
            prefix = "%s/resources/bin/%s/" % (ADDON, platform)
            windows = platform.startswith("windows_")
            self.assertIn(prefix + ("sing-box.exe" if windows else "sing-box"), names)
            self.assertNotIn(prefix + ("sing-box" if windows else "sing-box.exe"), names)
            self.assertIn(prefix + "version", names)
            self.assertIn(prefix + "sing-box-LICENSE", names)
            self.assertIn(prefix + "sing-box-NOTICE", names)
            if platform == "android_arm64":
                self.assertNotIn(prefix + "xray", names)
            else:
                self.assertIn(prefix + ("xray.exe" if windows else "xray"), names)
                self.assertIn(prefix + "xray-LICENSE", names)
        self.assertEqual([name for name in names if name.endswith(".zip")], [])
        for shared in (ADDON + "/LICENSE", ADDON + "/THIRD_PARTY_NOTICES.md",
                       ADDON + "/resources/licenses/xray/LICENSE"):
            self.assertEqual(names.count(shared), 1)
        verified = self.fixture.verify_universal(path)
        self.assertEqual(verified.returncode, 0, verified.stdout + verified.stderr)

    def test_output_is_byte_identical_after_input_restamping(self):
        self.assertEqual(self.assemble().returncode, 0)
        path = self.fixture.universal_path()
        with open(path, "rb") as stream:
            first = stream.read()
        os.remove(path)
        for platform in UNIVERSAL_PLATFORMS:
            self.fixture.restamp_zip(self.fixture.platform_zip_path(platform))
        result = self.fixture.make_universal()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        with open(path, "rb") as stream:
            self.assertEqual(stream.read(), first)

    def test_entries_are_sorted_with_fixed_timestamp_and_normalized_modes(self):
        self.assertEqual(self.assemble().returncode, 0)
        with zipfile.ZipFile(self.fixture.universal_path()) as archive:
            infos = archive.infolist()
        self.assertEqual([info.filename for info in infos],
                         sorted(info.filename for info in infos))
        for info in infos:
            self.assertFalse(info.is_dir(), info.filename)
            self.assertEqual(info.date_time, ZIP_TIMESTAMP, info.filename)
            self.assertEqual(info.compress_type, zipfile.ZIP_DEFLATED, info.filename)
            self.assertEqual(info.create_system, 3, info.filename)
            executable = os.path.basename(info.filename) in EXECUTABLES
            self.assertEqual(info.external_attr >> 16 & 0o7777,
                             0o755 if executable else 0o644, info.filename)

    def test_missing_platform_zip_is_rejected(self):
        self.fixture.make_platform_zips()
        os.remove(self.fixture.platform_zip_path("darwin_x64"))
        self.assertNoUniversal(self.fixture.make_universal())

    def test_ambiguous_platform_zip_is_rejected(self):
        self.fixture.make_platform_zips()
        self.fixture.make_zip("linux_x64", "9.9.9", into=self.fixture.dist)
        self.assertNoUniversal(self.fixture.make_universal())

    def test_divergent_shared_entry_is_rejected(self):
        def diverge(entries, prefix):
            if prefix.endswith("darwin_arm64"):
                entries[ADDON + "/LICENSE"] += b"\n# drifted\n"
        self.assertNoUniversal(self.assemble(mutation=diverge))

    def test_missing_shared_entry_is_rejected(self):
        def drop(entries, prefix):
            if prefix.endswith("linux_x86"):
                del entries[ADDON + "/THIRD_PARTY_NOTICES.md"]
        self.assertNoUniversal(self.assemble(mutation=drop))

    def test_foreign_platform_tree_is_rejected(self):
        def foreign(entries, prefix):
            if prefix.endswith("linux_arm64"):
                entries[ADDON + "/resources/bin/linux_x64/sing-box"] = b"foreign\n"
        self.assertNoUniversal(self.assemble(mutation=foreign))

    def test_nested_platform_zip_is_rejected(self):
        """Nested in every input, so only the nesting guard can catch it."""
        def nested(entries, _prefix):
            entries["%s/%s-%s.linux_x64.zip" % (ADDON, ADDON, VERSION)] = b"PK\x05\x06"
        self.assertNoUniversal(self.assemble(mutation=nested))

    def test_garbage_platform_zip_is_rejected(self):
        self.fixture.make_platform_zips()
        write(self.fixture.platform_zip_path("windows_x64"), b"not a zip", "wb")
        self.assertNoUniversal(self.fixture.make_universal())

    def test_truncated_platform_zip_is_rejected(self):
        self.fixture.make_platform_zips()
        path = self.fixture.platform_zip_path("linux_armv7")
        with open(path, "rb") as stream:
            payload = stream.read()
        write(path, payload[:len(payload) // 2], "wb")
        self.assertNoUniversal(self.fixture.make_universal())

    def test_crc_corrupted_platform_zip_is_rejected(self):
        self.fixture.make_platform_zips()
        path = self.fixture.platform_zip_path("linux_x64")
        entry = ADDON + "/LICENSE"
        with zipfile.ZipFile(path) as archive:
            info = archive.getinfo(entry)
        start = info.header_offset + 30 + len(entry.encode()) + len(info.extra)
        with open(path, "r+b") as stream:
            stream.seek(start + 8)
            stream.write(b"\xff\xfe\xfd\xfc\xfb\xfa\xf9\xf8")
        self.assertNoUniversal(self.fixture.make_universal())

    def test_explicit_version_selects_matching_zips(self):
        def bump(entries, _prefix):
            entries[ADDON + "/addon.xml"] = entries[ADDON + "/addon.xml"].replace(
                b'version="%s"' % VERSION.encode(), b'version="9.9.9"'
            )
        self.fixture.make_platform_zips(version="9.9.9", mutation=bump)
        result = self.fixture.make_universal("--version", "9.9.9")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        path = self.fixture.universal_path("9.9.9")
        self.assertTrue(os.path.exists(path))
        verified = self.fixture.verify_universal(path, "9.9.9")
        self.assertEqual(verified.returncode, 0, verified.stdout + verified.stderr)

    def test_default_version_comes_from_addon_xml(self):
        def bump(entries, _prefix):
            entries[ADDON + "/addon.xml"] = entries[ADDON + "/addon.xml"].replace(
                b'version="%s"' % VERSION.encode(), b'version="9.9.9"'
            )
        self.fixture.make_platform_zips(version="9.9.9", mutation=bump)
        self.assertNoUniversal(self.fixture.make_universal(), "9.9.9")

    def test_invalid_explicit_version_is_a_usage_error(self):
        self.fixture.make_platform_zips()
        for bad in ("1.2", "v1.2.3", "1.2.3-rc1", "../etc"):
            with self.subTest(version=bad):
                result = self.fixture.make_universal("--version", bad)
                self.assertEqual(result.returncode, 2,
                                 result.stdout + result.stderr)

    def test_addon_xml_version_must_match_target_version(self):
        self.fixture.make_platform_zips(version="9.9.9")
        self.assertNoUniversal(self.fixture.make_universal("--version", "9.9.9"),
                               "9.9.9")

    def test_stale_version_platform_zip_is_rejected(self):
        """addon.xml agrees with the target, only the filename is stale."""
        self.fixture.make_platform_zips(version="9.9.9")
        self.assertNoUniversal(self.fixture.make_universal("--version", VERSION))

    def test_unwritable_output_is_refused_without_leftovers(self):
        self.fixture.make_platform_zips()
        os.makedirs(os.path.join(self.fixture.dist, "blocked.zip"))
        result = self.fixture.make_universal(
            "--output", os.path.join(self.fixture.dist, "blocked.zip")
        )
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("make_universal:", result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertEqual([name for name in os.listdir(self.fixture.dist)
                          if name.endswith(".tmp")], [])


class TestUniversalVerifier(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="packaging-universal-verify-")
        self.addCleanup(shutil.rmtree, self.tmp)
        self.fixture = PackagingFixture(self.tmp)

    def universal(self, mutation=None):
        self.fixture.make_platform_zips()
        result = self.fixture.make_universal()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        path = self.fixture.universal_path()
        if mutation is not None:
            self.fixture.rewrite_zip(path, mutation)
        return path

    def assertRejected(self, path, version=VERSION):
        result = self.fixture.verify_universal(path, version)
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_universal_mode_requires_two_positionals(self):
        path = self.universal()
        result = subprocess.run(
            [os.path.join(self.fixture.repo, "scripts", "verify_zip.sh"),
             "--universal", path, "linux_x64", VERSION],
            cwd=self.fixture.repo, capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)

    def test_platform_zip_is_rejected_in_universal_mode(self):
        self.assertRejected(self.fixture.make_zip("linux_x64"))

    def test_universal_zip_is_rejected_in_single_platform_mode(self):
        path = self.universal()
        for platform in ("linux_x64", "windows_x64"):
            with self.subTest(platform=platform):
                result = self.fixture.verify(path, platform, VERSION)
                self.assertNotEqual(result.returncode, 0,
                                    result.stdout + result.stderr)

    def test_wrong_universal_filename_is_rejected(self):
        path = self.universal()
        for name in ("%s-%s.linux_x64.zip" % (ADDON, VERSION),
                     "%s-9.9.9.zip" % ADDON,
                     "advancedproxy-%s.zip" % VERSION):
            with self.subTest(name=name):
                renamed = os.path.join(self.fixture.dist, name)
                shutil.copy(path, renamed)
                self.assertRejected(renamed)
                os.remove(renamed)

    def test_missing_platform_tree_is_rejected(self):
        def strip(entries):
            prefix = "%s/resources/bin/darwin_x64/" % ADDON
            for name in [n for n in entries if n.startswith(prefix)]:
                del entries[name]
        self.assertRejected(self.universal(strip))

    def test_extra_platform_tree_is_rejected(self):
        def extra(entries):
            entries["%s/resources/bin/linux_armv6/sing-box" % ADDON] = b"sb\n"
        self.assertRejected(self.universal(extra))

    def test_windows_binaries_must_use_exe_names(self):
        def rename(entries):
            prefix = "%s/resources/bin/windows_x64/" % ADDON
            entries[prefix + "sing-box"] = entries.pop(prefix + "sing-box.exe")
            entries[prefix + "xray"] = entries.pop(prefix + "xray.exe")
        self.assertRejected(self.universal(rename))

    def test_non_windows_binaries_must_be_extensionless(self):
        def rename(entries):
            prefix = "%s/resources/bin/darwin_arm64/" % ADDON
            entries[prefix + "sing-box.exe"] = entries.pop(prefix + "sing-box")
            entries[prefix + "xray.exe"] = entries.pop(prefix + "xray")
        self.assertRejected(self.universal(rename))

    def test_wrong_stamp_is_rejected(self):
        for platform, stamp in (("linux_x86", "version"),
                                ("darwin_x64", "xray_version")):
            with self.subTest(platform=platform, stamp=stamp):
                def wrong(entries, platform=platform, stamp=stamp):
                    entries["%s/resources/bin/%s/%s" % (ADDON, platform, stamp)] = b"0.0.0\n"
                self.assertRejected(self.universal(wrong))

    def test_missing_per_platform_notice_is_rejected(self):
        for notice in ("sing-box-LICENSE", "sing-box-NOTICE", "xray-LICENSE"):
            with self.subTest(notice=notice):
                def drop(entries, notice=notice):
                    del entries["%s/resources/bin/linux_arm64/%s" % (ADDON, notice)]
                self.assertRejected(self.universal(drop))

    def test_tampered_shared_file_is_rejected(self):
        for target in (ADDON + "/LICENSE",
                       ADDON + "/THIRD_PARTY_NOTICES.md",
                       ADDON + "/resources/licenses/sing-box/LICENSE",
                       ADDON + "/resources/licenses/sing-box/NOTICE",
                       ADDON + "/resources/licenses/xray/LICENSE"):
            with self.subTest(target=target):
                def tamper(entries, target=target):
                    entries[target] = b"tampered but non-empty\n"
                self.assertRejected(self.universal(tamper))

    def test_wrong_addon_version_inside_zip_is_rejected(self):
        def bump(entries):
            entries[ADDON + "/addon.xml"] = entries[ADDON + "/addon.xml"].replace(
                b'version="%s"' % VERSION.encode(), b'version="9.9.9"'
            )
        self.assertRejected(self.universal(bump))

    def test_nested_platform_zip_entry_is_rejected(self):
        def nested(entries):
            entries["%s/%s-%s.linux_x64.zip" % (ADDON, ADDON, VERSION)] = b"PK\x05\x06"
        self.assertRejected(self.universal(nested))

    def test_unexpected_xray_for_android_is_rejected(self):
        def unexpected(entries):
            prefix = "%s/resources/bin/android_arm64/" % ADDON
            entries[prefix + "xray"] = b"xr\n"
            entries[prefix + "xray_version"] = (XR_VERSION + "\n").encode()
            entries[prefix + "xray-LICENSE"] = entries[
                ADDON + "/resources/licenses/xray/LICENSE"
            ]
        self.assertRejected(self.universal(unexpected))

    def append_entries(self, path, entry, copies):
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            with zipfile.ZipFile(path, "a") as archive:
                for _ in range(copies):
                    archive.writestr(entry, (SB_VERSION + "\n").encode())

    def test_duplicate_verified_entry_is_rejected(self):
        path = self.universal()
        self.append_entries(path, ADDON + "/resources/bin/linux_x64/version", 1)
        self.assertRejected(path)

    def test_duplicate_unverified_entry_is_rejected(self):
        """No per-entry check covers this name, so only the global one can."""
        path = self.universal()
        self.append_entries(path, ADDON + "/resources/settings.xml", 2)
        self.assertRejected(path)


if __name__ == "__main__":
    unittest.main(verbosity=2)
