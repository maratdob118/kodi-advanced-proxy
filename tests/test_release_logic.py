# -*- coding: utf-8 -*-
"""Release tooling tests: version drift guard, addon validation, release planner.

Run:  python3 tests/test_release_logic.py
      python3 -m unittest tests.test_release_logic

Covers (RED -> GREEN):
  * version drift between addon.xml / build.sh / runtime pins / stamps
  * addon + license metadata validation (XML, extensions, GPL, notices)
  * release planner create-vs-skip, asset discovery, checksums,
    draft/upload/publish flow, and abort-without-publish on failure.

All fixtures live in temp dirs; no network and no real `gh` invocations.
Subprocess calls in release.py are isolated behind an injectable runner.
"""
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from contextlib import redirect_stdout
from types import SimpleNamespace

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, ".."))
SCRIPTS = os.path.join(REPO, "scripts")
sys.path.insert(0, SCRIPTS)

import release          # noqa: E402
import validate_addon   # noqa: E402

# ---------------------------------------------------------------------------
# Fixture templates (self-contained; do not depend on the real repo state)
# ---------------------------------------------------------------------------

ADDON_XML = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<addon id="service.advancedproxy" name="Advanced Proxy" version="%s" provider-name="advancedproxy">
    <requires>
        <import addon="xbmc.python" version="3.0.0"/>
    </requires>
    <extension point="xbmc.service" library="main.py" start="startup"/>
    <extension point="xbmc.python.pluginsource" library="default.py">
        <provides>executable</provides>
    </extension>
    <extension point="xbmc.python.module" library="src"/>
    <extension point="xbmc.addon.metadata">
        <summary lang="en_GB">Fixture addon</summary>
        <description lang="en_GB">Fixture</description>
        <platform>all</platform>
        <license>%s</license>
        <assets>
            <icon>resources/icon.png</icon>
            <fanart>resources/fanart.jpg</fanart>
        </assets>
    </extension>
</addon>
"""

BUILD_SH = """#!/bin/bash
SINGBOX_VERSION="%s"
XRAY_VERSION="%s"
ADDON_VERSION="%s"
"""

BINARY_MANAGER = """SINGBOX_VERSION = "%s"
XRAY_VERSION = "%s"
"""

SETTINGS_XML = """<settings version="1">
  <section id="service.advancedproxy">
    <category id="connection" label="32000" help="32001">
      <setting id="engine" type="integer" label="32100" help="32101">
        <level>0</level>
        <default>0</default>
        <control type="list" format="string"><heading>32100</heading></control>
      </setting>
    </category>
  </section>
</settings>
"""

STRINGS_PO = """msgid ""
msgstr ""
"Project-Id-Version: service.advancedproxy\\n"
"Content-Type: text/plain; charset=UTF-8\\n"

msgctxt "#32000"
msgid "Connection"
msgstr ""

msgctxt "#32001"
msgid "Help text"
msgstr ""

msgctxt "#32100"
msgid "Engine"
msgstr ""

msgctxt "#32101"
msgid "Engine help"
msgstr ""
"""

LICENSE_TEXT = "GNU GENERAL PUBLIC LICENSE\nVersion 3, 29 June 2007\n"
NOTICES_TEXT = "# Third-Party Notices\n"
SINGBOX_LICENSE = "GPL-3.0-or-later text (sing-box)\n"
SINGBOX_NOTICE = "JA3: BSD-3-Clause notice\n"
XRAY_LICENSE = "MPL-2.0 text (Xray-core)\n"


def write(path, content):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content)


def make_fixture(root, addon_version="1.2.3", sb_build="1.13.14", xr_build="25.8.3",
                 sb_runtime=None, xr_runtime=None, license_name="GPL-3.0-or-later"):
    sb_runtime = sb_build if sb_runtime is None else sb_runtime
    xr_runtime = xr_build if xr_runtime is None else xr_runtime
    base = os.path.join(root, "service.advancedproxy")
    write(os.path.join(base, "addon.xml"), ADDON_XML % (addon_version, license_name))
    write(os.path.join(root, "build.sh"), BUILD_SH % (sb_build, xr_build, addon_version))
    write(os.path.join(base, "src", "binary_manager.py"),
          BINARY_MANAGER % (sb_runtime, xr_runtime))
    write(os.path.join(base, "resources", "settings.xml"), SETTINGS_XML)
    write(os.path.join(base, "resources", "language", "resource.language.en_gb", "strings.po"),
          STRINGS_PO)
    write(os.path.join(root, "LICENSE"), LICENSE_TEXT)
    write(os.path.join(root, "THIRD_PARTY_NOTICES.md"), NOTICES_TEXT)
    write(os.path.join(base, "resources", "licenses", "sing-box", "LICENSE"), SINGBOX_LICENSE)
    write(os.path.join(base, "resources", "licenses", "sing-box", "NOTICE"), SINGBOX_NOTICE)
    write(os.path.join(base, "resources", "licenses", "xray", "LICENSE"), XRAY_LICENSE)
    write(os.path.join(base, "resources", "bin", "linux_x64", "version"), sb_build + "\n")
    write(os.path.join(base, "resources", "bin", "linux_x64", "xray_version"), xr_build + "\n")
    return root


# ---------------------------------------------------------------------------
# check_versions.sh
# ---------------------------------------------------------------------------

class TestCheckVersions(unittest.TestCase):
    SCRIPT = os.path.join(SCRIPTS, "check_versions.sh")

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="checkver-")
        self.addCleanup(shutil.rmtree, self.tmp)

    def run_check(self, root=None):
        cmd = ["bash", self.SCRIPT] + ([root] if root else [])
        return subprocess.run(cmd, capture_output=True, text=True)

    def test_consistent_repo_passes(self):
        root = make_fixture(self.tmp)
        p = self.run_check(root)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("OK", p.stdout)

    def test_accepts_optional_repo_root_argument(self):
        root = make_fixture(self.tmp)
        p = self.run_check(root)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn(root, p.stdout)

    def test_default_repo_root_does_not_crash(self):
        p = self.run_check()
        self.assertIn(p.returncode, (0, 1))
        self.assertIn("repo", p.stdout.lower())

    def test_addon_build_version_drift_fails(self):
        root = make_fixture(self.tmp, addon_version="1.2.3")
        write(os.path.join(root, "build.sh"), BUILD_SH % ("1.13.14", "25.8.3", "9.9.9"))
        p = self.run_check(root)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("addon", (p.stdout + p.stderr).lower())

    def test_singbox_build_runtime_drift_fails(self):
        root = make_fixture(self.tmp, sb_build="1.13.14", sb_runtime="9.9.9")
        p = self.run_check(root)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("sing-box", (p.stdout + p.stderr).lower())

    def test_xray_build_runtime_drift_fails(self):
        root = make_fixture(self.tmp, xr_build="25.8.3", xr_runtime="1.0.0")
        p = self.run_check(root)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("xray", (p.stdout + p.stderr).lower())

    def test_platform_stamp_drift_fails(self):
        root = make_fixture(self.tmp)
        write(os.path.join(root, "service.advancedproxy", "resources", "bin",
                           "linux_x64", "version"), "0.0.1\n")
        p = self.run_check(root)
        self.assertNotEqual(p.returncode, 0)

    def test_missing_addon_xml_fails(self):
        root = make_fixture(self.tmp)
        os.remove(os.path.join(root, "service.advancedproxy", "addon.xml"))
        p = self.run_check(root)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("addon.xml", p.stdout + p.stderr)

    def test_bad_addon_version_format_fails(self):
        root = make_fixture(self.tmp)
        addon_path = os.path.join(root, "service.advancedproxy", "addon.xml")
        text = open(addon_path, encoding="utf-8").read().replace('version="1.2.3"', 'version="v1.2"')
        write(addon_path, text)
        p = self.run_check(root)
        self.assertNotEqual(p.returncode, 0)


# ---------------------------------------------------------------------------
# validate_addon.py
# ---------------------------------------------------------------------------

class TestValidateAddon(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="valaddon-")
        self.addCleanup(shutil.rmtree, self.tmp)

    def validate(self, root):
        return validate_addon.validate_addon(root)

    def test_valid_fixture_passes(self):
        root = make_fixture(self.tmp)
        self.assertEqual(self.validate(root), [])

    def test_malformed_addon_xml_fails(self):
        root = make_fixture(self.tmp)
        write(os.path.join(root, "service.advancedproxy", "addon.xml"), "<addon><oops></addon>")
        self.assertNotEqual(self.validate(root), [])

    def test_bad_version_fails(self):
        root = make_fixture(self.tmp, addon_version="banana")
        probs = self.validate(root)
        self.assertTrue(any("version" in p.lower() for p in probs))

    def test_missing_extension_fails(self):
        root = make_fixture(self.tmp)
        p = os.path.join(root, "service.advancedproxy", "addon.xml")
        text = open(p, encoding="utf-8").read()
        text = text.replace('<extension point="xbmc.python.module" library="src"/>', "")
        write(p, text)
        probs = self.validate(root)
        self.assertTrue(any("extension" in pr for pr in probs))

    def test_non_gpl_license_fails(self):
        root = make_fixture(self.tmp, license_name="MIT")
        probs = self.validate(root)
        self.assertTrue(any("license" in pr.lower() for pr in probs))

    def test_missing_license_notice_files_fails(self):
        root = make_fixture(self.tmp)
        os.remove(os.path.join(self.tmp, "LICENSE"))
        os.remove(os.path.join(self.tmp, "THIRD_PARTY_NOTICES.md"))
        probs = self.validate(root)
        self.assertTrue(any("LICENSE" in pr or "NOTICES" in pr for pr in probs))

    def test_missing_engine_license_files_fails(self):
        root = make_fixture(self.tmp)
        os.remove(os.path.join(self.tmp, "service.advancedproxy", "resources",
                               "licenses", "xray", "LICENSE"))
        probs = self.validate(root)
        self.assertTrue(any("xray" in pr.lower() for pr in probs))

    def test_missing_localized_setting_id_fails(self):
        root = make_fixture(self.tmp)
        p = os.path.join(root, "service.advancedproxy", "resources", "language",
                         "resource.language.en_gb", "strings.po")
        text = open(p, encoding="utf-8").read()
        text = re.sub(r'msgctxt "#32100"\nmsgid "[^"]*"\nmsgstr ""\n\n', "", text)
        write(p, text)
        probs = self.validate(root)
        self.assertTrue(any("32100" in pr for pr in probs))

    def test_malformed_settings_xml_fails(self):
        root = make_fixture(self.tmp)
        write(os.path.join(root, "service.advancedproxy", "resources", "settings.xml"),
              "<settings><unclosed>")
        self.assertNotEqual(self.validate(root), [])

    def test_cli_exit_zero_on_valid_fixture(self):
        root = make_fixture(self.tmp)
        p = subprocess.run([sys.executable, os.path.join(SCRIPTS, "validate_addon.py"), root],
                           capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("OK", p.stdout)

    def test_cli_exit_nonzero_on_license_mismatch(self):
        root = make_fixture(self.tmp, license_name="MIT")
        p = subprocess.run([sys.executable, os.path.join(SCRIPTS, "validate_addon.py"), root],
                           capture_output=True, text=True)
        self.assertNotEqual(p.returncode, 0)


# ---------------------------------------------------------------------------
# release.py
# ---------------------------------------------------------------------------

class FakeRunner:
    """Records gh commands; configurable view/upload/publish behavior.

    view_state: 'published' | 'draft' | 'missing' — what `gh release view`
    reports for the tag. 'draft' simulates a stranded draft left behind by a
    previous failed run.
    """

    def __init__(self, view_state="missing", fail_upload_at=None, fail_publish=False):
        self.calls = []
        self.view_state = view_state
        self.fail_upload_at = fail_upload_at
        self.fail_publish = fail_publish
        self._uploads = 0

    def __call__(self, cmd):
        self.calls.append(list(cmd))
        if cmd[:3] == ["gh", "release", "view"]:
            if self.view_state == "missing":
                return SimpleNamespace(returncode=1, stdout="", stderr="release not found")
            payload = json.dumps({"isDraft": self.view_state == "draft"})
            return SimpleNamespace(returncode=0, stdout=payload, stderr="")
        if cmd[:3] == ["gh", "release", "upload"]:
            self._uploads += 1
            rc = 1 if self.fail_upload_at is not None and self._uploads >= self.fail_upload_at else 0
            return SimpleNamespace(returncode=rc, stdout="", stderr="")
        if cmd[:3] == ["gh", "release", "edit"]:
            rc = 1 if self.fail_publish else 0
            return SimpleNamespace(returncode=rc, stdout="", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")


def make_assets(dirpath, names=None):
    names = names or ("service.advancedproxy-1.2.3.linux_x64.zip",
                      "service.advancedproxy-1.2.3.windows_x64.zip")
    os.makedirs(dirpath, exist_ok=True)
    paths = []
    for name in names:
        p = os.path.join(dirpath, name)
        with zipfile.ZipFile(p, "w") as zf:
            zf.writestr("payload.txt", name)
        paths.append(p)
    return paths


def sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        digest.update(fh.read())
    return digest.hexdigest()


class TestReleasePlanner(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="release-")
        self.addCleanup(shutil.rmtree, self.tmp)
        self.assets_dir = os.path.join(self.tmp, "dist")
        self.assets = make_assets(self.assets_dir)

    def planner(self, **kw):
        kw.setdefault("version", "1.2.3")
        kw.setdefault("assets_dir", self.assets_dir)
        kw.setdefault("sha", "abc123")
        kw.setdefault("runner", FakeRunner())
        return release.ReleasePlanner(**kw)

    # -- assets ------------------------------------------------------------

    def test_discovers_only_zips_sorted(self):
        write(os.path.join(self.assets_dir, "notes.md"), "x")
        write(os.path.join(self.assets_dir, "SHA256SUMS"), "old")
        pl = self.planner()
        got = [os.path.basename(a) for a in pl.discover_assets()]
        self.assertEqual(got, sorted([os.path.basename(a) for a in self.assets]))

    def test_discovers_only_zip_files_ignores_directories(self):
        os.makedirs(os.path.join(self.assets_dir, "fake.zip"), exist_ok=True)
        write(os.path.join(self.assets_dir, "notes.md"), "x")
        pl = self.planner()
        got = [os.path.basename(a) for a in pl.discover_assets()]
        self.assertEqual(got, sorted([os.path.basename(a) for a in self.assets]))

    def test_missing_assets_dir_fails(self):
        pl = self.planner(assets_dir=os.path.join(self.tmp, "nope"))
        self.assertEqual(pl.run(), 2)

    def test_no_zip_assets_fails(self):
        pl = self.planner()
        for a in self.assets:
            os.remove(a)
        self.assertEqual(pl.run(), 2)

    def test_invalid_version_fails(self):
        pl = self.planner(version="banana")
        self.assertEqual(pl.run(), 2)

    # -- checksums ---------------------------------------------------------

    def test_checksums_match_zip_contents(self):
        pl = self.planner()
        sums_path = pl.write_checksums()
        lines = {}
        with open(sums_path, encoding="utf-8") as fh:
            for line in fh:
                hexd, _, name = line.strip().partition("  ")
                lines[name] = hexd
        self.assertEqual(set(lines), {os.path.basename(a) for a in self.assets})
        for a in self.assets:
            self.assertEqual(lines[os.path.basename(a)], sha256(a))

    def test_checksums_are_deterministic(self):
        pl = self.planner()
        first = pl.write_checksums()
        with open(first, encoding="utf-8") as fh:
            text1 = fh.read()
        second = pl.write_checksums()
        with open(second, encoding="utf-8") as fh:
            text2 = fh.read()
        self.assertEqual(text1, text2)

    # -- create-vs-skip ----------------------------------------------------

    def test_existing_release_skips_cleanly(self):
        runner = FakeRunner(view_state="published")
        pl = self.planner(runner=runner)
        rc = pl.run()
        self.assertEqual(rc, 0)
        self.assertEqual(runner.calls,
                         [["gh", "release", "view", "v1.2.3", "--json", "isDraft"]])

    def test_new_release_creates_draft(self):
        runner = FakeRunner(view_state="missing")
        pl = self.planner(runner=runner, sha="deadbeef")
        rc = pl.run()
        self.assertEqual(rc, 0)
        create = [c for c in runner.calls if c[:3] == ["gh", "release", "create"]]
        self.assertEqual(len(create), 1)
        self.assertIn("--draft", create[0])
        self.assertIn("--target", create[0])
        self.assertIn("deadbeef", create[0])

    def test_create_without_sha_omits_target(self):
        runner = FakeRunner(view_state="missing")
        pl = self.planner(runner=runner, sha=None)
        pl.run()
        create = [c for c in runner.calls if c[:3] == ["gh", "release", "create"]]
        self.assertNotIn("--target", create[0])

    # -- draft/upload/publish flow ----------------------------------------

    def test_draft_upload_checksums_publish_in_order(self):
        runner = FakeRunner(view_state="missing")
        pl = self.planner(runner=runner, sha="deadbeef")
        rc = pl.run()
        self.assertEqual(rc, 0)
        expected = [
            ["gh", "release", "view", "v1.2.3", "--json", "isDraft"],
            ["gh", "release", "create", "v1.2.3", "--draft",
             "--target", "deadbeef",
             "--title", "Advanced Proxy 1.2.3",
             "--notes", "Advanced Proxy 1.2.3: 2 platform assets + SHA256SUMS."],
            ["gh", "release", "upload", "v1.2.3", self.assets[0]],
            ["gh", "release", "upload", "v1.2.3", self.assets[1]],
            ["gh", "release", "upload", "v1.2.3", os.path.join(self.assets_dir, "SHA256SUMS")],
            ["gh", "release", "edit", "v1.2.3", "--draft=false"],
        ]
        self.assertEqual(runner.calls, expected)

    def test_upload_failure_aborts_without_publish(self):
        runner = FakeRunner(view_state="missing", fail_upload_at=1)
        pl = self.planner(runner=runner)
        rc = pl.run()
        self.assertNotEqual(rc, 0)
        publishes = [c for c in runner.calls if c[:3] == ["gh", "release", "edit"]]
        self.assertEqual(publishes, [])

    def test_create_failure_aborts(self):
        class FailingCreateRunner(FakeRunner):
            def __call__(self, cmd):
                if cmd[:3] == ["gh", "release", "create"]:
                    self.calls.append(list(cmd))
                    return SimpleNamespace(returncode=1, stdout="", stderr="")
                return super().__call__(cmd)
        runner = FailingCreateRunner(view_state="missing")
        pl = self.planner(runner=runner)
        rc = pl.run()
        self.assertNotEqual(rc, 0)
        self.assertEqual([c for c in runner.calls if c[:3] == ["gh", "release", "edit"]], [])

    # -- leftover-draft recovery / publish failure ------------------------

    def test_leftover_draft_resumes_with_clobber_and_publishes(self):
        runner = FakeRunner(view_state="draft")
        pl = self.planner(runner=runner, sha="deadbeef")
        rc = pl.run()
        self.assertEqual(rc, 0)
        expected = [
            ["gh", "release", "view", "v1.2.3", "--json", "isDraft"],
            ["gh", "release", "upload", "v1.2.3", self.assets[0], "--clobber"],
            ["gh", "release", "upload", "v1.2.3", self.assets[1], "--clobber"],
            ["gh", "release", "upload", "v1.2.3",
             os.path.join(self.assets_dir, "SHA256SUMS"), "--clobber"],
            ["gh", "release", "edit", "v1.2.3", "--draft=false"],
        ]
        self.assertEqual(runner.calls, expected)

    def test_leftover_draft_resume_never_creates(self):
        runner = FakeRunner(view_state="draft")
        pl = self.planner(runner=runner)
        pl.run()
        creates = [c for c in runner.calls if c[:3] == ["gh", "release", "create"]]
        self.assertEqual(creates, [])

    def test_resume_upload_failure_aborts_without_publish(self):
        runner = FakeRunner(view_state="draft", fail_upload_at=1)
        pl = self.planner(runner=runner)
        rc = pl.run()
        self.assertNotEqual(rc, 0)
        edits = [c for c in runner.calls if c[:3] == ["gh", "release", "edit"]]
        self.assertEqual(edits, [])

    def test_publish_failure_aborts_fresh_flow(self):
        runner = FakeRunner(view_state="missing", fail_publish=True)
        pl = self.planner(runner=runner)
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = pl.run()
        self.assertNotEqual(rc, 0)
        self.assertIn("publish", buf.getvalue().lower())

    def test_publish_failure_aborts_resume_flow(self):
        runner = FakeRunner(view_state="draft", fail_publish=True)
        pl = self.planner(runner=runner)
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = pl.run()
        self.assertNotEqual(rc, 0)
        self.assertIn("publish", buf.getvalue().lower())

    # -- dry-run -----------------------------------------------------------

    def test_dry_run_runs_no_gh_commands(self):
        runner = FakeRunner()
        pl = self.planner(runner=runner, dry_run=True)
        rc = pl.run()
        self.assertEqual(rc, 0)
        self.assertEqual(runner.calls, [])
        self.assertTrue(os.path.isfile(os.path.join(self.assets_dir, "SHA256SUMS")))

    def test_dry_run_prints_plan(self):
        runner = FakeRunner()
        pl = self.planner(runner=runner, dry_run=True)
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = pl.run()
        out = buf.getvalue()
        self.assertEqual(rc, 0)
        self.assertIn("dry-run", out)
        self.assertIn("v1.2.3", out)

    def test_cli_dry_run(self):
        p = subprocess.run(
            [sys.executable, os.path.join(SCRIPTS, "release.py"),
             "--version", "1.2.3", "--assets-dir", self.assets_dir, "--dry-run"],
            capture_output=True, text=True, cwd=REPO)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("dry-run", p.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
