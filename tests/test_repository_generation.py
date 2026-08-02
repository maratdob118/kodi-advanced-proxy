"""Tests for the text-only bigping.repository tree generator.

The generator turns two source manifests plus one universal ZIP into the
text tree the target repository commits and GitHub Pages serves:

    addons.xml, addons.xml.md5, manifest.json, README.md,
    repository.bigping/addon.xml

Nothing binary is ever produced: the ~235 MB universal ZIP is only read, and
its identity travels to the Pages workflow as a URL + SHA256 in manifest.json.
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
REPOSITORY = "repository.bigping"
SOURCE_REPO = "maratdob118/bigping"
PAGES = "https://maratdob118.github.io/bigping.repository/"
RELEASES = "https://github.com/%s/releases/download/" % SOURCE_REPO
XML_DECLARATION = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
GENERATED = ("addons.xml", "addons.xml.md5", "manifest.json", "README.md",
             os.path.join(REPOSITORY, "addon.xml"))


def read_bytes(path):
    with open(path, "rb") as stream:
        return stream.read()


def read_text(path):
    return read_bytes(path).decode("utf-8")


def source_root(addon_id):
    return ET.parse(os.path.join(REPO, addon_id, "addon.xml")).getroot()


def addon_version(path):
    return ET.parse(path).getroot().get("version")


def declared_assets(addon_id):
    """(kind, reference) for every asset an addon manifest declares."""
    metadata = next(element for element in source_root(addon_id).iter("extension")
                    if element.get("point") == "xbmc.addon.metadata")
    assets = metadata.find("assets")
    if assets is None:
        return []
    return [(child.tag, child.text.strip()) for child in assets
            if (child.text or "").strip()]


# Derived, never hardcoded: a version bump must not break these tests.
VERSION = source_root(PAYLOAD).get("version")
REPOSITORY_VERSION = source_root(REPOSITORY).get("version")
PAYLOAD_ASSETS = declared_assets(PAYLOAD)
MODE_DIR = 0o755
MODE_FILE = 0o644


class RepositoryFixture:
    """A throwaway copy of the source repo plus a fake universal ZIP."""

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
        """The source payload addon.xml, restamped through its version attribute."""
        root = source_root(PAYLOAD)
        if version:
            root.set("version", version)
        return XML_DECLARATION + "\n" + ET.tostring(root, encoding="unicode")

    def universal_path(self, version=None):
        return os.path.join(self.dist,
                            "%s-%s.zip" % (PAYLOAD, version or VERSION))

    def make_universal(self, version=None, inner_version=None, path=None,
                       mutation=None):
        """Write a stand-in universal ZIP: payload manifest plus declared art."""
        version = version or VERSION
        path = path or self.universal_path(version)
        entries = {
            "%s/addon.xml" % PAYLOAD: self.payload_xml(inner_version or version),
            "%s/main.py" % PAYLOAD: "# payload\n",
        }
        for _, reference in PAYLOAD_ASSETS:
            entries["%s/%s" % (PAYLOAD, reference)] = "art:%s\n" % reference
        if mutation:
            mutation(entries)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
            for name in sorted(entries):
                archive.writestr(name, entries[name])
        return path

    def universal_entry(self, name, version=None):
        with zipfile.ZipFile(self.universal_path(version)) as archive:
            return archive.read(name)

    def generate(self, *options, out=None, umask=-1):
        out = self.out if out is None else out
        argv = [sys.executable, self.source(os.path.join("scripts",
                                                         "generate_repo.py")),
                "--repo", self.repo, "--out", out, *options]
        return subprocess.run(argv, cwd=self.repo, capture_output=True,
                              text=True, umask=umask)

    def generated(self, *parts, out=None):
        return os.path.join(self.out if out is None else out, *parts)

    def manifest(self, out=None):
        return json.loads(read_text(self.generated("manifest.json", out=out)))

    def tree(self, out=None):
        """Every generated file, repo-relative, sorted."""
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
        self.fixture.make_universal()
        result = self.fixture.generate(*options, **kwargs)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def assertRefused(self, result, out=None):
        """A refusal diagnoses itself and leaves no half-written tree behind."""
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
    """The hand-written repository.bigping/addon.xml is the Kodi 20+ contract."""

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

    def test_repository_extension_uses_the_kodi20_dir_form(self):
        extension = self.extension("xbmc.addon.repository")
        self.assertTrue((extension.get("name") or "").strip())
        dirs = extension.findall("dir")
        self.assertEqual(len(dirs), 1, "expected exactly one <dir>")
        self.assertEqual(dirs[0].get("minversion"), "20.0.0")
        for child in ("info", "checksum", "datadir", "hashes"):
            self.assertIsNotNone(dirs[0].find(child), child)
        self.assertIsNone(extension.find("info"),
                          "<info> must live inside <dir>, not under the point")

    def test_dir_children_carry_the_conventional_schema_attributes(self):
        """Kodi 20+ ignores info@compressed and datadir@zip; repository.xsd
        still declares them, so the shape is pinned by convention only."""
        node = self.extension("xbmc.addon.repository").find("dir")
        self.assertEqual(node.find("info").get("compressed"), "false")
        self.assertEqual(node.find("checksum").get("verify"), "md5")
        self.assertEqual(node.find("datadir").get("zip"), "true")
        self.assertEqual((node.find("hashes").text or "").strip(), "sha256")

    def test_four_https_pages_urls_with_datadir_slash(self):
        node = self.extension("xbmc.addon.repository").find("dir")
        metadata = self.extension("xbmc.addon.metadata")
        urls = [node.find("info").text.strip(),
                node.find("checksum").text.strip(),
                node.find("datadir").text.strip(),
                metadata.find("website").text.strip()]
        self.assertEqual(len(urls), 4)
        for url in urls:
            self.assertTrue(url.startswith("https://"), url)
            self.assertTrue(url.startswith(PAGES), url)
            self.assertNotIn("raw.githubusercontent", url)
        self.assertEqual(urls[0], PAGES + "addons.xml")
        self.assertEqual(urls[1], PAGES + "addons.xml.md5")
        self.assertEqual(urls[2], PAGES)
        self.assertTrue(urls[2].endswith("/"), "datadir must end with a slash")

    def test_metadata_declares_the_project_license(self):
        metadata = self.extension("xbmc.addon.metadata")
        self.assertEqual((metadata.find("license").text or "").strip(),
                         "GPL-3.0-or-later")

    def test_repository_addon_declares_no_binary_assets(self):
        """No invented artwork: the addon ships metadata only."""
        assets = self.extension("xbmc.addon.metadata").find("assets")
        if assets is not None:
            for element in assets.iter():
                if element is not assets:
                    self.fail("unexpected asset reference: %s" % element.tag)
        directory = os.path.join(REPO, REPOSITORY)
        self.assertEqual(sorted(os.listdir(directory)), ["addon.xml"])


class TestGeneratedTree(RepositoryTestCase):
    def test_generates_exactly_the_text_files_pages_needs(self):
        self.generate_ok()
        self.assertEqual(self.fixture.tree(), sorted(GENERATED))

    def test_tree_is_text_only_and_carries_no_payload_bytes(self):
        self.generate_ok()
        universal = os.path.getsize(self.fixture.universal_path())
        for relative in self.fixture.tree():
            path = self.fixture.generated(relative)
            self.assertNotEqual(os.path.getsize(path), universal)
            self.assertLess(os.path.getsize(path), 64 * 1024, relative)
            self.assertFalse(relative.endswith(".zip"), relative)
            read_text(path)  # decodes as UTF-8 or raises

    def test_repository_manifest_is_copied_verbatim(self):
        self.generate_ok()
        self.assertEqual(
            read_bytes(self.fixture.generated(REPOSITORY, "addon.xml")),
            read_bytes(os.path.join(REPO, REPOSITORY, "addon.xml")))

    def test_readme_documents_the_pages_endpoints(self):
        self.generate_ok()
        readme = read_text(self.fixture.generated("README.md"))
        for needle in (PAGES + "addons.xml", REPOSITORY, PAYLOAD,
                       "generate_repo.py"):
            self.assertIn(needle, readme)

    def test_readme_states_the_publish_obligations(self):
        self.generate_ok()
        readme = read_text(self.fixture.generated("README.md")).lower()
        for needle in ("manifest.json", "sha256", ".sha256", "content-sha256",
                       "zip_root", "art"):
            self.assertIn(needle.lower(), readme)


class TestFixtureIntegrity(RepositoryTestCase):
    """The harness must track the source manifests, not a pinned literal."""

    def test_versions_are_read_from_the_source_manifests(self):
        self.assertEqual(VERSION, addon_version(
            os.path.join(REPO, PAYLOAD, "addon.xml")))
        self.assertEqual(REPOSITORY_VERSION, addon_version(
            os.path.join(REPO, REPOSITORY, "addon.xml")))

    def test_restamping_survives_a_version_bump(self):
        for version in ("9.9.9", "0.0.1"):
            stamped = ET.fromstring(self.fixture.payload_xml(version))
            self.assertEqual(stamped.get("version"), version)
        self.assertEqual(
            ET.fromstring(self.fixture.payload_xml()).get("version"), VERSION)

    def test_stand_in_universal_carries_the_declared_art(self):
        self.fixture.make_universal()
        with zipfile.ZipFile(self.fixture.universal_path()) as archive:
            names = archive.namelist()
        for _, reference in PAYLOAD_ASSETS:
            self.assertIn("%s/%s" % (PAYLOAD, reference), names)


class TestPermissions(RepositoryTestCase):
    def assertTreeModes(self):
        self.assertEqual(stat.S_IMODE(os.stat(self.fixture.out).st_mode),
                         MODE_DIR, "output root")
        self.assertEqual(
            stat.S_IMODE(os.stat(self.fixture.generated(REPOSITORY)).st_mode),
            MODE_DIR, REPOSITORY)
        for relative in self.fixture.tree():
            self.assertEqual(
                stat.S_IMODE(os.stat(self.fixture.generated(relative)).st_mode),
                MODE_FILE, relative)

    def test_published_tree_is_world_readable(self):
        self.generate_ok()
        self.assertTreeModes()

    def test_modes_are_explicit_and_ignore_a_hostile_umask(self):
        self.fixture.make_universal()
        result = self.fixture.generate(umask=0o077)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTreeModes()


class TestAddonsXml(RepositoryTestCase):
    def setUp(self):
        super().setUp()
        self.generate_ok()
        self.path = self.fixture.generated("addons.xml")
        self.raw = read_bytes(self.path)
        self.root = ET.fromstring(self.raw)

    def test_wraps_exactly_the_two_manifests_in_deterministic_order(self):
        self.assertEqual(self.root.tag, "addons")
        addons = list(self.root)
        self.assertEqual([element.tag for element in addons], ["addon", "addon"])
        ids = [element.get("id") for element in addons]
        self.assertEqual(ids, [REPOSITORY, PAYLOAD])
        self.assertEqual(ids, sorted(ids))

    def test_versions_come_from_the_source_manifests(self):
        versions = {element.get("id"): element.get("version")
                    for element in self.root}
        self.assertEqual(versions[PAYLOAD],
                         addon_version(os.path.join(REPO, PAYLOAD, "addon.xml")))
        self.assertEqual(versions[REPOSITORY],
                         addon_version(os.path.join(REPO, REPOSITORY, "addon.xml")))

    def test_each_addon_element_matches_its_source_manifest(self):
        for addon_id, relative in ((PAYLOAD, os.path.join(PAYLOAD, "addon.xml")),
                                   (REPOSITORY,
                                    os.path.join(REPOSITORY, "addon.xml"))):
            element = next(e for e in self.root if e.get("id") == addon_id)
            source = ET.parse(os.path.join(REPO, relative)).getroot()
            self.assertEqual(ET.canonicalize(ET.tostring(element)),
                             ET.canonicalize(ET.tostring(source)), addon_id)

    def test_encoding_is_pinned_utf8_with_lf_endings(self):
        head = self.raw.split(b"\n", 1)[0]
        self.assertEqual(
            head, b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>')
        self.assertNotIn(b"\r", self.raw)
        self.assertFalse(self.raw.startswith(b"\xef\xbb\xbf"), "unexpected BOM")
        self.assertTrue(self.raw.endswith(b"</addons>\n"))
        self.raw.decode("utf-8")

    def test_service_extension_points_survive_the_round_trip(self):
        payload = next(e for e in self.root if e.get("id") == PAYLOAD)
        points = {e.get("point") for e in payload.iter("extension")}
        self.assertIn("xbmc.service", points)
        self.assertIn("xbmc.addon.metadata", points)


class TestChecksum(RepositoryTestCase):
    def setUp(self):
        super().setUp()
        self.generate_ok()

    def test_md5_file_is_lowercase_digest_of_exact_addons_xml_bytes(self):
        digest = hashlib.md5(
            read_bytes(self.fixture.generated("addons.xml"))).hexdigest()
        recorded = read_text(self.fixture.generated("addons.xml.md5"))
        self.assertEqual(recorded, digest + "\n")
        self.assertEqual(recorded.strip(), recorded.strip().lower())
        self.assertRegex(recorded.strip(), r"^[0-9a-f]{32}$")

    def test_md5_tracks_a_changed_addons_xml(self):
        first = read_text(self.fixture.generated("addons.xml.md5"))
        shutil.rmtree(self.fixture.out)
        payload = self.fixture.source(os.path.join(PAYLOAD, "addon.xml"))
        with open(payload, "w", encoding="utf-8") as stream:
            stream.write(self.fixture.payload_xml("9.9.9"))
        self.fixture.make_universal("9.9.9")
        result = self.fixture.generate()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        second = read_text(self.fixture.generated("addons.xml.md5"))
        self.assertNotEqual(first, second)
        self.assertEqual(
            second,
            hashlib.md5(
                read_bytes(self.fixture.generated("addons.xml"))).hexdigest() + "\n")

    def test_manifest_records_the_same_md5(self):
        recorded = read_text(self.fixture.generated("addons.xml.md5")).strip()
        self.assertEqual(self.fixture.manifest()["index"]["md5"], recorded)


class TestManifest(RepositoryTestCase):
    def setUp(self):
        super().setUp()
        self.generate_ok()
        self.data = self.fixture.manifest()
        self.entries = {entry["id"]: entry for entry in self.data["addons"]}

    def test_records_the_universal_release_asset_url(self):
        self.assertEqual(
            self.entries[PAYLOAD]["url"],
            RELEASES + "v%s/%s-%s.zip" % (VERSION, PAYLOAD, VERSION))
        self.assertTrue(self.entries[PAYLOAD]["url"].startswith("https://"))
        self.assertNotIn("raw.githubusercontent", json.dumps(self.data))

    def test_binds_the_asset_to_an_immutable_release_tag(self):
        release = self.entries[PAYLOAD]["release"]
        self.assertEqual(release["repo"], SOURCE_REPO)
        self.assertEqual(release["tag"], "v" + VERSION)
        self.assertEqual(release["asset"], "%s-%s.zip" % (PAYLOAD, VERSION))
        self.assertTrue(self.entries[PAYLOAD]["url"].endswith(
            "/%s/%s" % (release["tag"], release["asset"])))

    def test_orders_pages_to_download_and_compare_before_deploying(self):
        verification = self.data["verification"]
        self.assertEqual(verification["algorithm"], "sha256")
        self.assertEqual(verification["policy"], "download-then-compare")
        instruction = verification["instruction"].lower()
        for needle in ("download", "recompute", "abort"):
            self.assertIn(needle, instruction)

    def test_digests_are_declared_as_expectations_not_verified_facts(self):
        """Nothing here was fetched: the digest describes the local artifact."""
        verification = self.data["verification"]
        self.assertEqual(verification["measured_on"], "local-build-artifact")
        self.assertFalse(verification["remote_verified"])

    def test_records_the_universal_sha256_and_size(self):
        universal = self.fixture.universal_path()
        self.assertEqual(self.entries[PAYLOAD]["sha256"],
                         hashlib.sha256(read_bytes(universal)).hexdigest())
        self.assertEqual(self.entries[PAYLOAD]["size"],
                         os.path.getsize(universal))
        self.assertRegex(self.entries[PAYLOAD]["sha256"], r"^[0-9a-f]{64}$")

    def test_paths_compose_canonically_for_both_addons(self):
        self.assertEqual(self.entries[PAYLOAD]["path"],
                         "%s/%s-%s.zip" % (PAYLOAD, PAYLOAD, VERSION))
        repository_version = addon_version(
            os.path.join(REPO, REPOSITORY, "addon.xml"))
        self.assertEqual(
            self.entries[REPOSITORY]["path"],
            "%s/%s-%s.zip" % (REPOSITORY, REPOSITORY, repository_version))
        for entry in self.data["addons"]:
            self.assertEqual(
                entry["path"],
                "%s/%s-%s.zip" % (entry["id"], entry["id"], entry["version"]))
            self.assertFalse(entry["path"].startswith("/"))
            self.assertNotIn("..", entry["path"].split("/"))

    def test_datadir_composes_with_paths_into_the_pages_urls(self):
        self.assertEqual(self.data["datadir"], PAGES)
        self.assertEqual(self.data["datadir"] + self.entries[PAYLOAD]["path"],
                         PAGES + "service.advancedproxy/"
                                 "service.advancedproxy-%s.zip" % VERSION)
        self.assertEqual(
            self.data["datadir"] + self.entries[REPOSITORY]["path"],
            PAGES + "repository.bigping/repository.bigping-%s.zip"
            % self.entries[REPOSITORY]["version"])

    def test_repository_addon_is_marked_as_built_by_pages(self):
        entry = self.entries[REPOSITORY]
        self.assertEqual(entry["origin"], "build")
        self.assertEqual(entry["metadata"], "%s/addon.xml" % REPOSITORY)
        self.assertNotIn("url", entry)
        self.assertNotIn("sha256", entry)
        self.assertEqual(self.entries[PAYLOAD]["origin"], "release-asset")

    def test_build_instruction_pins_the_bootstrap_zip_root(self):
        """Kodi rejects a repo ZIP whose single root is not the addon id."""
        entry = self.entries[REPOSITORY]
        self.assertEqual(entry["zip_root"], REPOSITORY + "/")
        self.assertEqual(entry["metadata"], entry["zip_root"] + "addon.xml")

    def test_payload_zip_root_is_recorded_for_verification(self):
        self.assertEqual(self.entries[PAYLOAD]["zip_root"], PAYLOAD + "/")

    def test_every_datadir_zip_declares_its_hash_sidecar(self):
        """Kodi reads a content-sha256 header first and falls back to a
        <zip>.sha256 sidecar; Pages cannot set headers, so the sidecar is the
        only mechanism available here and is mandatory."""
        for entry in self.data["addons"]:
            self.assertEqual(entry["sha256_path"], entry["path"] + ".sha256")

    def test_publishes_every_asset_the_payload_manifest_references(self):
        art = self.entries[PAYLOAD]["art"]
        self.assertEqual([(item["kind"], item["path"]) for item in art],
                         [(kind, "%s/%s" % (PAYLOAD, reference))
                          for kind, reference in PAYLOAD_ASSETS])
        self.assertTrue(art, "payload declares assets; manifest lists none")

    def test_art_paths_match_the_urls_kodi_resolves_from_the_index(self):
        for (kind, reference), item in zip(PAYLOAD_ASSETS,
                                           self.entries[PAYLOAD]["art"]):
            self.assertEqual(self.data["datadir"] + item["path"],
                             "%s%s/%s" % (PAGES, PAYLOAD, reference))
            self.assertEqual(item["kind"], kind)

    def test_art_is_extracted_from_the_payload_zip_with_a_digest(self):
        for item in self.entries[PAYLOAD]["art"]:
            self.assertEqual(item["origin"], "payload-zip")
            self.assertEqual(
                item["sha256"],
                hashlib.sha256(
                    self.fixture.universal_entry(item["source"])).hexdigest())

    def test_json_is_deterministic_and_readable(self):
        raw = read_text(self.fixture.generated("manifest.json"))
        self.assertTrue(raw.endswith("\n"))
        self.assertNotIn("\r", raw)
        self.assertEqual(raw, json.dumps(self.data, indent=2,
                                         sort_keys=True, ensure_ascii=False) + "\n")
        self.assertEqual(self.data["schema"], 1)
        self.assertEqual([entry["id"] for entry in self.data["addons"]],
                         sorted(entry["id"] for entry in self.data["addons"]))


class TestDeterminism(RepositoryTestCase):
    def snapshot(self, out=None):
        return {relative: read_bytes(self.fixture.generated(relative, out=out))
                for relative in self.fixture.tree(out=out)}

    def test_regeneration_over_an_existing_tree_is_byte_identical(self):
        self.generate_ok()
        first = self.snapshot()
        result = self.fixture.generate()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.snapshot(), first)

    def test_generation_into_a_fresh_directory_is_byte_identical(self):
        self.generate_ok()
        first = self.snapshot()
        other = os.path.join(self.tmp, "other")
        result = self.fixture.generate(out=other)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.snapshot(out=other), first)

    def test_output_does_not_depend_on_universal_zip_timestamps(self):
        self.generate_ok()
        first = self.snapshot()
        os.utime(self.fixture.universal_path(), (0, 0))
        result = self.fixture.generate()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.snapshot(), first)


class TestStaleOutput(RepositoryTestCase):
    def test_stale_generated_files_are_removed(self):
        self.generate_ok()
        stale = self.fixture.generated("addons.xml.sha1")
        with open(stale, "w", encoding="utf-8") as stream:
            stream.write("stale\n")
        stale_dir = self.fixture.generated("service.advancedproxy")
        os.makedirs(stale_dir)
        with open(os.path.join(stale_dir, "old.zip"), "w",
                  encoding="utf-8") as stream:
            stream.write("stale\n")
        result = self.fixture.generate()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.fixture.tree(), sorted(GENERATED))
        self.assertFalse(os.path.exists(stale))
        self.assertFalse(os.path.exists(stale_dir))

    def test_a_refusal_leaves_the_previous_tree_intact(self):
        self.generate_ok()
        before = {relative: read_bytes(self.fixture.generated(relative))
                  for relative in self.fixture.tree()}
        os.remove(self.fixture.universal_path())
        self.assertRefused(self.fixture.generate())
        self.assertEqual(
            {relative: read_bytes(self.fixture.generated(relative))
             for relative in self.fixture.tree()}, before)


class TestRefusals(RepositoryTestCase):
    def test_missing_universal_zip_is_rejected(self):
        result = self.fixture.generate()
        self.assertRefused(result)
        self.assertFalse(os.path.exists(self.fixture.out))

    def test_universal_zip_version_mismatch_is_rejected(self):
        self.fixture.make_universal(VERSION, inner_version="9.9.9")
        self.assertRefused(self.fixture.generate())

    def test_universal_filename_must_be_the_canonical_universal_name(self):
        wrong = os.path.join(self.fixture.dist,
                             "%s-%s.linux_x64.zip" % (PAYLOAD, VERSION))
        self.fixture.make_universal(path=wrong)
        self.assertRefused(self.fixture.generate("--universal", wrong))

    def test_universal_zip_missing_a_declared_asset_is_rejected(self):
        """An asset Pages cannot extract would 404 in the repository browser."""
        _, reference = PAYLOAD_ASSETS[0]
        self.fixture.make_universal(
            mutation=lambda entries: entries.pop("%s/%s" % (PAYLOAD, reference)))
        self.assertRefused(self.fixture.generate())

    def test_universal_zip_with_a_foreign_root_is_rejected(self):
        self.fixture.make_universal(
            mutation=lambda entries: entries.update({"elsewhere/file.txt": "x\n"}))
        self.assertRefused(self.fixture.generate())

    def test_universal_zip_without_payload_manifest_is_rejected(self):
        path = self.fixture.universal_path()
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("%s/main.py" % PAYLOAD, "# payload\n")
        self.assertRefused(self.fixture.generate())

    def test_corrupt_universal_zip_is_rejected(self):
        with open(self.fixture.universal_path(), "wb") as stream:
            stream.write(b"not a zip")
        self.assertRefused(self.fixture.generate())

    def test_requested_version_must_match_the_payload_manifest(self):
        self.fixture.make_universal()
        self.assertRefused(self.fixture.generate("--version", "9.9.9"))

    def test_malformed_version_is_a_usage_error(self):
        self.fixture.make_universal()
        result = self.fixture.generate("--version", "0.2")
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertFalse(os.path.exists(self.fixture.out))

    def test_missing_repository_manifest_is_rejected(self):
        self.fixture.make_universal()
        os.remove(self.fixture.source(os.path.join(REPOSITORY, "addon.xml")))
        self.assertRefused(self.fixture.generate())

    def test_malformed_source_manifest_is_rejected(self):
        self.fixture.make_universal()
        with open(self.fixture.source(os.path.join(REPOSITORY, "addon.xml")),
                  "w", encoding="utf-8") as stream:
            stream.write("<addon id=\"repository.bigping\"")
        self.assertRefused(self.fixture.generate())

    def test_output_may_not_be_the_repo_root(self):
        self.fixture.make_universal()
        self.assertRefused(self.fixture.generate(out=self.fixture.repo),
                           out=self.fixture.repo)
        self.assertTrue(os.path.isfile(
            self.fixture.source(os.path.join(PAYLOAD, "addon.xml"))))

    def test_output_may_not_be_a_foreign_non_empty_directory(self):
        self.fixture.make_universal()
        foreign = os.path.join(self.tmp, "foreign")
        os.makedirs(foreign)
        keep = os.path.join(foreign, "keep.txt")
        with open(keep, "w", encoding="utf-8") as stream:
            stream.write("precious\n")
        self.assertRefused(self.fixture.generate(out=foreign), out=foreign)
        self.assertEqual(read_text(keep), "precious\n")

    def test_output_may_not_be_a_file(self):
        self.fixture.make_universal()
        path = os.path.join(self.tmp, "file")
        with open(path, "w", encoding="utf-8") as stream:
            stream.write("data\n")
        self.assertRefused(self.fixture.generate(out=path), out=path)
        self.assertEqual(read_text(path), "data\n")

    def test_output_may_not_sit_inside_the_git_directory(self):
        self.fixture.make_universal()
        inside = os.path.join(self.fixture.repo, ".git", "pages")
        self.assertRefused(self.fixture.generate(out=inside), out=inside)
        self.assertFalse(os.path.exists(inside))

    def test_empty_output_directory_is_accepted(self):
        self.fixture.make_universal()
        empty = os.path.join(self.tmp, "empty")
        os.makedirs(empty)
        result = self.fixture.generate(out=empty)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.fixture.tree(out=empty), sorted(GENERATED))


if __name__ == "__main__":
    unittest.main()
