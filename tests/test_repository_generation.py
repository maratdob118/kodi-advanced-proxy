"""Tests for the classic Kodi repository tree generator.

The generator turns the two source manifests plus one payload ZIP into the
tree that maratdob118/kodi-addons commits and serves from
raw.githubusercontent.com:

    zips/addons.xml, zips/addons.xml.md5,
    zips/service.advancedproxy/{addon.xml, service.advancedproxy-<v>.zip},
    zips/repository.maratdob118/{addon.xml, repository.maratdob118-<v>.zip},
    README.md
"""
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
import zipfile


HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, ".."))
PAYLOAD = "service.advancedproxy"
REPOSITORY = "repository.maratdob118"
XML_DECLARATION = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'


def read_bytes(path):
    with open(path, "rb") as stream:
        return stream.read()


def read_text(path):
    return read_bytes(path).decode("utf-8")


def source_root(addon_id):
    return ET.parse(os.path.join(REPO, addon_id, "addon.xml")).getroot()


def addon_version(path):
    return ET.parse(path).getroot().get("version")


VERSION = source_root(PAYLOAD).get("version")
REPOSITORY_VERSION = source_root(REPOSITORY).get("version")
MODE_DIR = 0o755
MODE_FILE = 0o644


class RepositoryFixture:
    """A throwaway copy of the source repo plus a fake payload ZIP."""

    def __init__(self, root):
        self.root = root
        self.repo = os.path.join(root, "repo")
        self.dist = os.path.join(self.repo, "dist")
        self.out = os.path.join(root, "out")
        os.makedirs(self.dist)
        for relative in (os.path.join(PAYLOAD, "addon.xml"),
                         os.path.join(REPOSITORY, "addon.xml"),
                         os.path.join("scripts", "generate_repo.py")):
            target = os.path.join(self.repo, relative)
            os.makedirs(os.path.dirname(target), exist_ok=True)
            shutil.copy2(os.path.join(REPO, relative), target)

    def source(self, relative):
        return os.path.join(self.repo, relative)

    def payload_xml(self, version=None):
        root = source_root(PAYLOAD)
        if version:
            root.set("version", version)
        return XML_DECLARATION + "\n" + ET.tostring(root, encoding="unicode")

    def payload_path(self, version=None):
        return os.path.join(self.dist,
                            "%s-%s.zip" % (PAYLOAD, version or VERSION))

    def make_payload(self, version=None, inner_version=None, path=None,
                     mutation=None):
        version = version or VERSION
        path = path or self.payload_path(version)
        entries = {
            "%s/addon.xml" % PAYLOAD: self.payload_xml(inner_version or version),
            "%s/main.py" % PAYLOAD: "# payload\n",
            "%s/resources/icon.png" % PAYLOAD: "icon-bytes",
        }
        if mutation:
            mutation(entries)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
            for name in sorted(entries):
                archive.writestr(name, entries[name])
        return path

    def generate(self, *options, out=None, umask=-1):
        out = self.out if out is None else out
        argv = [sys.executable, self.source(os.path.join("scripts",
                                                         "generate_repo.py")),
                "--repo", self.repo, "--out", out, *options]
        return subprocess.run(argv, cwd=self.repo, capture_output=True,
                              text=True, umask=umask)

    def generated(self, *parts, out=None):
        return os.path.join(self.out if out is None else out, *parts)

    def tree(self, out=None):
        base = self.out if out is None else out
        found = []
        for directory, _, names in os.walk(base):
            for name in names:
                found.append(os.path.relpath(os.path.join(directory, name), base))
        return sorted(found)


class RepositoryTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="repository-generation-")
        self.addCleanup(shutil.rmtree, self.tmp)
        self.fixture = RepositoryFixture(self.tmp)

    def generate_ok(self, *options, **kwargs):
        self.fixture.make_payload()
        result = self.fixture.generate(*options, **kwargs)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def assertRefused(self, result, out=None):
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("generate_repo:", result.stderr)
        self.assertNotIn("Traceback", result.stderr,
                         "refusal crashed instead of diagnosing")
        base = self.fixture.out if out is None else out
        parent = os.path.dirname(base)
        siblings = os.listdir(parent) if os.path.isdir(parent) else []
        leftovers = [name for name in siblings
                     if name.startswith(os.path.basename(base) + ".")]
        self.assertEqual(leftovers, [], "refusal left scratch directories behind")


class TestRepositoryAddonManifest(RepositoryTestCase):
    """The hand-written repository.maratdob118/addon.xml is the Kodi 19+ contract."""

    def setUp(self):
        super().setUp()
        self.root = ET.parse(os.path.join(REPO, REPOSITORY, "addon.xml")).getroot()

    def extension(self, point):
        return next(element for element in self.root.iter("extension")
                    if element.get("point") == point)

    def test_addon_root_identifies_the_new_repository_addon(self):
        self.assertEqual(self.root.tag, "addon")
        self.assertEqual(self.root.get("id"), REPOSITORY)
        self.assertRegex(self.root.get("version"), r"^\d+\.\d+\.\d+$")
        for attribute in ("name", "provider-name"):
            self.assertTrue((self.root.get(attribute) or "").strip(), attribute)

    def test_payload_addon_id_is_untouched(self):
        payload = ET.parse(os.path.join(REPO, PAYLOAD, "addon.xml")).getroot()
        self.assertEqual(payload.get("id"), PAYLOAD)

    def test_repository_extension_uses_the_dir_form(self):
        extension = self.extension("xbmc.addon.repository")
        self.assertTrue((extension.get("name") or "").strip())
        directories = [element for element in extension.iter("dir")]
        self.assertEqual(len(directories), 1)

    def test_dir_children_carry_the_conventional_schema_attributes(self):
        directory = next(self.extension("xbmc.addon.repository").iter("dir"))
        self.assertTrue((directory.get("minversion") or "").strip())
        children = {child.tag: child for child in directory}
        for tag in ("info", "checksum", "datadir"):
            self.assertIn(tag, children, "repo addon must declare <%s>" % tag)
        self.assertEqual(children["datadir"].get("zip"), "true")

    def test_endpoints_point_at_the_committed_zips_tree(self):
        directory = next(self.extension("xbmc.addon.repository").iter("dir"))
        children = {child.tag: child for child in directory}
        self.assertIn("raw.githubusercontent.com/maratdob118/kodi-addons",
                      children["info"].text)
        self.assertIn("raw.githubusercontent.com/maratdob118/kodi-addons",
                      children["checksum"].text)
        self.assertIn("raw.githubusercontent.com/maratdob118/kodi-addons",
                      children["datadir"].text)
        self.assertTrue(children["info"].text.endswith("zips/addons.xml"))
        self.assertTrue(children["checksum"].text.endswith("zips/addons.xml.md5"))
        self.assertTrue(children["datadir"].text.endswith("zips/"))
        self.assertEqual((children.get("hashes").text if "hashes" in children
                          else None), "false",
                         "raw.githubusercontent.com sends no Content-SHA256")

    def test_metadata_declares_the_project_license(self):
        metadata = self.extension("xbmc.addon.metadata")
        self.assertEqual(metadata.find("license").text, "GPL-3.0-or-later")

    def test_metadata_source_points_at_the_new_source_repository(self):
        metadata = self.extension("xbmc.addon.metadata")
        self.assertEqual(metadata.find("source").text,
                         "https://github.com/maratdob118/kodi-advanced-proxy")


class TestGeneratedTree(RepositoryTestCase):
    def test_generates_exactly_the_classic_tree(self):
        self.generate_ok()
        self.assertEqual(self.fixture.tree(), sorted([
            "README.md",
            "zips/addons.xml",
            "zips/addons.xml.md5",
            os.path.join("zips", PAYLOAD, "addon.xml"),
            os.path.join("zips", PAYLOAD,
                         "%s-%s.zip" % (PAYLOAD, VERSION)),
            os.path.join("zips", REPOSITORY, "addon.xml"),
            os.path.join("zips", REPOSITORY,
                         "%s-%s.zip" % (REPOSITORY, REPOSITORY_VERSION)),
        ]))

    def test_payload_zip_is_copied_verbatim(self):
        self.generate_ok()
        with open(self.fixture.payload_path(), "rb") as stream:
            expected = stream.read()
        self.assertEqual(
            read_bytes(self.fixture.generated(
                "zips", PAYLOAD, "%s-%s.zip" % (PAYLOAD, VERSION))),
            expected)

    def test_repository_zip_is_a_single_root_archive_of_the_manifest(self):
        self.generate_ok()
        path = self.fixture.generated(
            "zips", REPOSITORY, "%s-%s.zip" % (REPOSITORY, REPOSITORY_VERSION))
        with zipfile.ZipFile(path) as archive:
            self.assertEqual(archive.namelist(), ["%s/addon.xml" % REPOSITORY])
            manifest = ET.fromstring(archive.read("%s/addon.xml" % REPOSITORY))
        self.assertEqual(manifest.get("id"), REPOSITORY)
        self.assertEqual(manifest.get("version"), REPOSITORY_VERSION)

    def test_versions_are_read_from_the_source_manifests(self):
        self.generate_ok()
        addons = read_text(self.fixture.generated("zips", "addons.xml"))
        self.assertIn('id="%s"' % PAYLOAD, addons)
        self.assertIn('id="%s"' % REPOSITORY, addons)

    def test_published_tree_is_world_readable(self):
        self.generate_ok()
        for directory, _, names in os.walk(self.fixture.out):
            self.assertEqual(stat.S_IMODE(os.stat(directory).st_mode), MODE_DIR)
            for name in names:
                self.assertEqual(
                    stat.S_IMODE(os.stat(os.path.join(directory, name)).st_mode),
                    MODE_FILE)

    def test_modes_are_explicit_and_ignore_a_hostile_umask(self):
        self.generate_ok(umask=0o077)
        self.assertEqual(
            stat.S_IMODE(os.stat(self.fixture.generated("zips", "addons.xml")).st_mode),
            MODE_FILE)

    def test_wraps_exactly_the_two_manifests_in_deterministic_order(self):
        self.generate_ok()
        addons = read_text(self.fixture.generated("zips", "addons.xml"))
        self.assertTrue(addons.startswith(XML_DECLARATION + "\n<addons>\n"))
        self.assertTrue(addons.endswith("</addons>\n"))
        first = addons.index("<addon id=")
        second = addons.index("<addon id=", first + 1)
        self.assertLess(first, second)
        self.assertNotIn("<addon id=", addons[second + 1:])

    def test_encoding_is_pinned_utf8_with_lf_endings(self):
        self.generate_ok()
        raw = read_bytes(self.fixture.generated("zips", "addons.xml"))
        self.assertEqual(raw.decode("utf-8"), raw.decode("utf-8").replace("\r", ""))

    def test_md5_file_is_lowercase_digest_of_exact_addons_xml_bytes(self):
        self.generate_ok()
        addons = read_bytes(self.fixture.generated("zips", "addons.xml"))
        md5 = read_text(self.fixture.generated("zips", "addons.xml.md5")).strip()
        self.assertEqual(md5.lower(), hashlib.md5(addons).hexdigest())

    def test_md5_tracks_a_changed_addons_xml(self):
        self.generate_ok()
        first = read_text(self.fixture.generated("zips", "addons.xml.md5"))
        source_xml = self.fixture.source(os.path.join(PAYLOAD, "addon.xml"))
        root = source_root(PAYLOAD)
        root.set("version", "9.9.9")
        with open(source_xml, "w") as stream:
            stream.write(XML_DECLARATION + "\n" + ET.tostring(root,
                                                              encoding="unicode"))
        self.fixture.make_payload(version="9.9.9", inner_version="9.9.9")
        result = self.fixture.generate("--version", "9.9.9")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        second = read_text(self.fixture.generated("zips", "addons.xml.md5"))
        self.assertNotEqual(first, second)

    def test_regeneration_over_an_existing_tree_is_byte_identical(self):
        self.generate_ok()
        before = {name: read_bytes(self.fixture.generated(name))
                  for name in self.fixture.tree()}
        result = self.fixture.generate()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        after = {name: read_bytes(self.fixture.generated(name))
                 for name in self.fixture.tree()}
        self.assertEqual(before, after)

    def test_stale_generated_files_are_removed(self):
        self.generate_ok()
        stale = self.fixture.generated("zips", "stale.txt")
        os.makedirs(os.path.dirname(stale), exist_ok=True)
        with open(stale, "w") as stream:
            stream.write("old")
        result = self.fixture.generate()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("stale.txt", self.fixture.tree())

    def test_a_refusal_leaves_the_previous_tree_intact(self):
        self.generate_ok()
        before = {name: read_bytes(self.fixture.generated(name))
                  for name in self.fixture.tree()}
        os.remove(self.fixture.payload_path())
        result = self.fixture.generate()
        self.assertRefused(result)
        after = {name: read_bytes(self.fixture.generated(name))
                 for name in self.fixture.tree()}
        self.assertEqual(before, after)


class TestGenerationRefusals(RepositoryTestCase):
    def test_missing_payload_zip_is_rejected(self):
        result = self.fixture.generate()
        self.assertRefused(result)

    def test_payload_zip_version_mismatch_is_rejected(self):
        self.fixture.make_payload(inner_version="1.2.3")
        result = self.fixture.generate()
        self.assertRefused(result)

    def test_payload_zip_with_a_foreign_root_is_rejected(self):
        self.fixture.make_payload(
            mutation=lambda entries: entries.pop("%s/addon.xml" % PAYLOAD))
        result = self.fixture.generate()
        self.assertRefused(result)

    def test_corrupt_payload_zip_is_rejected(self):
        path = self.fixture.payload_path()
        with open(path, "wb") as stream:
            stream.write(b"not a zip")
        result = self.fixture.generate()
        self.assertRefused(result)

    def test_requested_version_must_match_the_payload_manifest(self):
        self.fixture.make_payload()
        result = self.fixture.generate("--version", "9.9.9")
        self.assertRefused(result)

    def test_malformed_version_is_a_usage_error(self):
        result = self.fixture.generate("--version", "banana")
        self.assertEqual(result.returncode, 2)

    def test_missing_repository_manifest_is_rejected(self):
        self.fixture.make_payload()
        os.remove(self.fixture.source(os.path.join(REPOSITORY, "addon.xml")))
        result = self.fixture.generate()
        self.assertRefused(result)

    def test_malformed_source_manifest_is_rejected(self):
        self.fixture.make_payload()
        path = self.fixture.source(os.path.join(REPOSITORY, "addon.xml"))
        with open(path, "w") as stream:
            stream.write("<addon id='%s' version='1.0.0'>" % REPOSITORY)
        result = self.fixture.generate()
        self.assertRefused(result)

    def test_output_may_not_be_the_repo_root(self):
        result = self.fixture.generate(out=self.fixture.repo)
        self.assertRefused(result)

    def test_output_may_not_be_a_foreign_non_empty_directory(self):
        self.fixture.make_payload()
        foreign = os.path.join(self.tmp, "foreign")
        os.makedirs(foreign)
        with open(os.path.join(foreign, "other.txt"), "w") as stream:
            stream.write("not generated")
        result = self.fixture.generate(out=foreign)
        self.assertRefused(result)

    def test_output_may_not_be_a_file(self):
        self.fixture.make_payload()
        path = os.path.join(self.tmp, "file-out")
        with open(path, "w") as stream:
            stream.write("x")
        result = self.fixture.generate(out=path)
        self.assertRefused(result)

    def test_empty_output_directory_is_accepted(self):
        self.fixture.make_payload()
        empty = os.path.join(self.tmp, "empty")
        os.makedirs(empty)
        result = self.fixture.generate(out=empty)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
