# -*- coding: utf-8 -*-
"""Tests for the target repository's Pages site builder.

`bootstrap/bigping.repository/scripts/build_site.py` is bootstrapped by hand
into maratdob118/kodi-addons. There it turns the text tree the publisher
pushed plus one downloaded universal ZIP into the site GitHub Pages serves:

    addons.xml, addons.xml.md5
    service.advancedproxy/service.advancedproxy-<version>.zip[.sha256]
    service.advancedproxy/resources/<art declared by addons.xml>
    repository.bigping/repository.bigping-<version>.zip[.sha256]

The builder is the last gate before ~235 MB reaches users, so the properties
under test are about not trusting anything: the payload is only published when
its bytes hash to what the manifest measured at build time, every path the
manifest names has to stay inside the site, and two runs over one input have to
produce identical bytes.

The fixtures run the real `scripts/generate_repo.py` to produce the manifest, so
a change to the generator that the builder does not follow fails here rather
than in production. No network and no real release asset is involved.

Run:  python3 tests/test_site_builder.py
      python3 -m unittest tests.test_site_builder
"""
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, ".."))
TEMPLATE = os.path.join(REPO, "bootstrap", "bigping.repository")
BUILD_SITE = os.path.join(TEMPLATE, "scripts", "build_site.py")
GENERATE_REPO = os.path.join(REPO, "scripts", "generate_repo.py")

PAYLOAD = "service.advancedproxy"
REPOSITORY = "repository.bigping"
XML_DECLARATION = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
MANIFEST = "manifest.json"
ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)


def read_bytes(path):
    with open(path, "rb") as stream:
        return stream.read()


def sha256(payload):
    return hashlib.sha256(payload).hexdigest()


def source_root(addon_id):
    return ET.parse(os.path.join(REPO, addon_id, "addon.xml")).getroot()


def declared_assets(addon_id):
    metadata = next(element for element in source_root(addon_id).iter("extension")
                    if element.get("point") == "xbmc.addon.metadata")
    assets = metadata.find("assets")
    if assets is None:
        return []
    return [(child.tag, child.text.strip()) for child in assets
            if (child.text or "").strip()]


# Derived from the real manifests: a version bump must not break these tests.
VERSION = source_root(PAYLOAD).get("version")
REPOSITORY_VERSION = source_root(REPOSITORY).get("version")
PAYLOAD_ASSETS = declared_assets(PAYLOAD)
PAYLOAD_ZIP = "%s/%s-%s.zip" % (PAYLOAD, PAYLOAD, VERSION)
REPOSITORY_ZIP = "%s/%s-%s.zip" % (REPOSITORY, REPOSITORY, REPOSITORY_VERSION)


class SiteFixture:
    """A generated tree plus the universal ZIP it describes, in a temp dir."""

    def __init__(self, root):
        self.root = root
        self.repo = os.path.join(root, "repo")
        self.dist = os.path.join(self.repo, "dist")
        self.generated = os.path.join(root, "generated")
        os.makedirs(self.dist)
        for addon_id in (PAYLOAD, REPOSITORY):
            target = os.path.join(self.repo, addon_id, "addon.xml")
            os.makedirs(os.path.dirname(target), exist_ok=True)
            shutil.copy2(os.path.join(REPO, addon_id, "addon.xml"), target)
        self.payload = self.make_universal()
        self.generate()

    # -- inputs ------------------------------------------------------------

    def make_universal(self, path=None):
        """A stand-in universal ZIP: the payload manifest, code and its art."""
        path = path or os.path.join(self.dist, "%s-%s.zip" % (PAYLOAD, VERSION))
        entries = {
            "%s/addon.xml" % PAYLOAD:
                XML_DECLARATION + "\n"
                + ET.tostring(source_root(PAYLOAD), encoding="unicode"),
            "%s/main.py" % PAYLOAD: "# payload\n",
        }
        for _, reference in PAYLOAD_ASSETS:
            entries["%s/%s" % (PAYLOAD, reference)] = "art:%s\n" % reference
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
            for name in sorted(entries):
                archive.writestr(name, entries[name])
        return path

    def generate(self):
        result = subprocess.run(
            [sys.executable, GENERATE_REPO, "--repo", self.repo,
             "--out", self.generated, "--universal", self.payload],
            capture_output=True, text=True)
        if result.returncode != 0:
            raise AssertionError("generate_repo failed: %s%s"
                                 % (result.stdout, result.stderr))

    def manifest_path(self):
        return os.path.join(self.generated, MANIFEST)

    def manifest(self):
        with open(self.manifest_path(), encoding="utf-8") as stream:
            return json.load(stream)

    def mutate(self, change):
        """Rewrite manifest.json through CHANGE, as a hostile input would."""
        document = self.manifest()
        change(document)
        with open(self.manifest_path(), "w", encoding="utf-8") as stream:
            json.dump(document, stream, indent=2, sort_keys=True)
            stream.write("\n")

    def entry(self, document, addon_id):
        return next(item for item in document["addons"] if item["id"] == addon_id)

    def tamper_payload(self):
        with open(self.payload, "r+b") as stream:
            stream.seek(0)
            stream.write(b"PK\x03\x04junk")

    # -- runs --------------------------------------------------------------

    def run(self, *options, out=None):
        argv = [sys.executable, BUILD_SITE, "--manifest", self.manifest_path()]
        if out is not None:
            argv += ["--out", out]
        return subprocess.run(argv + list(options), capture_output=True, text=True)

    def build(self, *options, out=None):
        out = out or os.path.join(self.root, "_site")
        result = self.run("--payload", self.payload, *options, out=out)
        return result, out

    def built(self, *options):
        result, out = self.build(*options)
        if result.returncode != 0:
            raise AssertionError("build_site failed: %s%s"
                                 % (result.stdout, result.stderr))
        return out


def site_tree(out):
    """Every file in the built site, relative and slash-separated."""
    found = []
    for directory, _, names in os.walk(out):
        for name in names:
            path = os.path.join(directory, name)
            found.append(os.path.relpath(path, out).replace(os.sep, "/"))
    return sorted(found)


class SiteBuilderTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="site-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.fixture = SiteFixture(self.tmp)

    def out(self, *parts):
        return os.path.join(self.tmp, "_site", *parts)


# ---------------------------------------------------------------------------
# The served tree
# ---------------------------------------------------------------------------

class TestServedTree(SiteBuilderTest):
    def test_builds_the_whole_served_tree(self):
        out = self.fixture.built()
        expected = {"addons.xml", "addons.xml.md5",
                    PAYLOAD_ZIP, PAYLOAD_ZIP + ".sha256",
                    REPOSITORY_ZIP, REPOSITORY_ZIP + ".sha256"}
        expected |= {"%s/%s" % (PAYLOAD, reference)
                     for _, reference in PAYLOAD_ASSETS}
        self.assertEqual(set(site_tree(out)), expected)

    def test_index_is_copied_verbatim_and_agrees_with_its_md5(self):
        out = self.fixture.built()
        addons_xml = read_bytes(os.path.join(out, "addons.xml"))
        self.assertEqual(addons_xml,
                         read_bytes(os.path.join(self.fixture.generated,
                                                 "addons.xml")))
        recorded = read_bytes(os.path.join(out, "addons.xml.md5")).decode().strip()
        self.assertEqual(recorded, hashlib.md5(addons_xml).hexdigest())

    def test_payload_is_published_byte_for_byte(self):
        out = self.fixture.built()
        self.assertEqual(read_bytes(os.path.join(out, PAYLOAD_ZIP)),
                         read_bytes(self.fixture.payload))

    def test_manifest_and_output_agree_on_the_canonical_path(self):
        document = self.fixture.manifest()
        self.assertEqual(self.fixture.entry(document, PAYLOAD)["path"], PAYLOAD_ZIP)
        self.assertEqual(self.fixture.entry(document, REPOSITORY)["path"],
                         REPOSITORY_ZIP)

    def test_art_is_published_where_addons_xml_resolves_it(self):
        out = self.fixture.built()
        with zipfile.ZipFile(self.fixture.payload) as archive:
            for _, reference in PAYLOAD_ASSETS:
                entry = "%s/%s" % (PAYLOAD, reference)
                self.assertEqual(read_bytes(os.path.join(out, entry)),
                                 archive.read(entry))


# ---------------------------------------------------------------------------
# Digest sidecars: Kodi's only verification channel on Pages
# ---------------------------------------------------------------------------

class TestSidecars(SiteBuilderTest):
    def sidecar(self, out, zip_path):
        return read_bytes(os.path.join(out, zip_path + ".sha256")).decode("utf-8")

    def test_payload_sidecar_is_lowercase_hex_and_one_newline(self):
        out = self.fixture.built()
        text = self.sidecar(out, PAYLOAD_ZIP)
        self.assertTrue(text.endswith("\n"), repr(text))
        digest = text.strip()
        self.assertRegex(digest, r"^[0-9a-f]{64}$")
        self.assertEqual(digest, sha256(read_bytes(os.path.join(out, PAYLOAD_ZIP))))

    def test_payload_sidecar_matches_what_the_manifest_measured(self):
        out = self.fixture.built()
        recorded = self.fixture.entry(self.fixture.manifest(), PAYLOAD)["sha256"]
        self.assertEqual(self.sidecar(out, PAYLOAD_ZIP).strip(), recorded)

    def test_repository_sidecar_describes_the_repository_zip(self):
        out = self.fixture.built()
        text = self.sidecar(out, REPOSITORY_ZIP)
        self.assertTrue(text.endswith("\n"))
        self.assertEqual(text.strip(),
                         sha256(read_bytes(os.path.join(out, REPOSITORY_ZIP))))

    def test_every_published_zip_has_a_sidecar(self):
        out = self.fixture.built()
        zips = [name for name in site_tree(out) if name.endswith(".zip")]
        self.assertEqual(len(zips), 2)
        for name in zips:
            self.assertTrue(os.path.isfile(os.path.join(out, name + ".sha256")),
                            "%s has no .sha256 sidecar" % name)


# ---------------------------------------------------------------------------
# The bootstrap repository ZIP
# ---------------------------------------------------------------------------

class TestRepositoryZip(SiteBuilderTest):
    def archive(self, out):
        return zipfile.ZipFile(os.path.join(out, REPOSITORY_ZIP))

    def test_has_exactly_one_root_directory(self):
        out = self.fixture.built()
        with self.archive(out) as archive:
            roots = {name.split("/")[0] for name in archive.namelist()}
        self.assertEqual(roots, {REPOSITORY}, "Kodi rejects any other root")

    def test_carries_the_repository_metadata_unchanged(self):
        out = self.fixture.built()
        with self.archive(out) as archive:
            packed = archive.read("%s/addon.xml" % REPOSITORY)
        self.assertEqual(packed,
                         read_bytes(os.path.join(self.fixture.generated,
                                                 REPOSITORY, "addon.xml")))

    def test_is_byte_reproducible(self):
        first = read_bytes(os.path.join(self.fixture.built(), REPOSITORY_ZIP))
        second_out = os.path.join(self.tmp, "again")
        result, _ = self.fixture.build(out=second_out)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(first, read_bytes(os.path.join(second_out, REPOSITORY_ZIP)))

    def test_pins_timestamps_and_modes(self):
        out = self.fixture.built()
        with self.archive(out) as archive:
            for info in archive.infolist():
                self.assertEqual(info.date_time, ZIP_TIMESTAMP, info.filename)
                self.assertEqual(info.external_attr >> 16, 0o644, info.filename)


class TestDeterminism(SiteBuilderTest):
    def test_the_whole_site_is_byte_reproducible(self):
        first = self.fixture.built()
        second = os.path.join(self.tmp, "again")
        result, _ = self.fixture.build(out=second)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(site_tree(first), site_tree(second))
        for relative in site_tree(first):
            self.assertEqual(read_bytes(os.path.join(first, relative)),
                             read_bytes(os.path.join(second, relative)),
                             relative)


# ---------------------------------------------------------------------------
# Verification: the payload is a download, so it is never trusted
# ---------------------------------------------------------------------------

class TestVerification(SiteBuilderTest):
    def verify(self):
        return self.fixture.run("--verify", "--payload", self.fixture.payload)

    def test_accepts_the_payload_the_manifest_describes(self):
        result = self.verify()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_rejects_a_payload_whose_bytes_changed(self):
        self.fixture.tamper_payload()
        result = self.verify()
        self.assertEqual(result.returncode, 1)
        self.assertIn("sha256", (result.stdout + result.stderr).lower())

    def test_rejects_a_payload_whose_size_changed(self):
        with open(self.fixture.payload, "ab") as stream:
            stream.write(b"\x00")
        result = self.verify()
        self.assertEqual(result.returncode, 1)

    def test_size_is_checked_on_its_own(self):
        """Not only via the digest: a wrong recorded size is a stale manifest."""
        self.fixture.mutate(lambda document: self.fixture
                            .entry(document, PAYLOAD)
                            .__setitem__("size", os.path.getsize(
                                self.fixture.payload) + 1))
        result = self.verify()
        self.assertEqual(result.returncode, 1)
        self.assertIn("bytes", result.stdout + result.stderr)

    def test_build_refuses_a_payload_that_fails_verification(self):
        self.fixture.tamper_payload()
        result, out = self.fixture.build()
        self.assertEqual(result.returncode, 1)
        self.assertFalse(os.path.isdir(out) and site_tree(out),
                         "a rejected payload must leave no site behind")

    def test_rejects_a_manifest_digest_that_is_not_a_sha256(self):
        self.fixture.mutate(lambda document: self.fixture
                            .entry(document, PAYLOAD)
                            .__setitem__("sha256", "NOTAHASH"))
        self.assertEqual(self.verify().returncode, 1)

    def test_refuses_art_whose_digest_no_longer_matches(self):
        reference = PAYLOAD_ASSETS[0][1]

        def corrupt(document):
            self.fixture.entry(document, PAYLOAD)["art"][0]["sha256"] = "0" * 64

        self.fixture.mutate(corrupt)
        result, _ = self.fixture.build()
        self.assertEqual(result.returncode, 1)
        self.assertIn(reference, result.stdout + result.stderr)

    def test_refuses_art_the_payload_does_not_carry(self):
        def add_missing(document):
            art = self.fixture.entry(document, PAYLOAD)["art"]
            art.append({"kind": "icon", "origin": "payload-zip",
                        "source": "%s/resources/absent.png" % PAYLOAD,
                        "path": "%s/resources/absent.png" % PAYLOAD,
                        "sha256": "0" * 64})
        self.fixture.mutate(add_missing)
        result, _ = self.fixture.build()
        self.assertEqual(result.returncode, 1)
        self.assertIn("absent.png", result.stdout + result.stderr)


# ---------------------------------------------------------------------------
# The download plan the workflow consumes
# ---------------------------------------------------------------------------

class TestPlan(SiteBuilderTest):
    def plan(self):
        result = self.fixture.run("--plan")
        pairs = {}
        for line in result.stdout.splitlines():
            key, _, value = line.partition("=")
            pairs[key] = value
        return result, pairs

    def test_prints_url_digest_and_size(self):
        result, pairs = self.plan()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        entry = self.fixture.entry(self.fixture.manifest(), PAYLOAD)
        self.assertEqual(pairs["url"], entry["url"])
        self.assertEqual(pairs["sha256"], entry["sha256"])
        self.assertEqual(pairs["size"], str(entry["size"]))

    def test_output_is_one_safe_line_per_key(self):
        """These lines are appended to $GITHUB_OUTPUT; a newline would inject."""
        result, pairs = self.plan()
        self.assertEqual(len(result.stdout.strip().splitlines()), len(pairs))
        for value in pairs.values():
            self.assertNotIn(" ", value)

    def test_refuses_a_url_on_another_host(self):
        self.fixture.mutate(lambda document: self.fixture
                            .entry(document, PAYLOAD)
                            .__setitem__("url", "https://evil.example/a.zip"))
        result, _ = self.plan()
        self.assertEqual(result.returncode, 1)

    def test_refuses_a_plaintext_url(self):
        entry = self.fixture.entry(self.fixture.manifest(), PAYLOAD)
        plain = entry["url"].replace("https://", "http://")
        self.fixture.mutate(lambda document: self.fixture
                            .entry(document, PAYLOAD).__setitem__("url", plain))
        result, _ = self.plan()
        self.assertEqual(result.returncode, 1)

    def test_refuses_raw_githubusercontent(self):
        """It serves Git blobs; the payload is far too large to be one."""
        raw = ("https://raw.githubusercontent.com/maratdob118/kodi-advanced-proxy/main/%s-%s.zip"
               % (PAYLOAD, VERSION))
        self.fixture.mutate(lambda document: self.fixture
                            .entry(document, PAYLOAD).__setitem__("url", raw))
        result, _ = self.plan()
        self.assertEqual(result.returncode, 1)

    def test_refuses_a_url_that_disagrees_with_the_release_fields(self):
        def retarget(document):
            entry = self.fixture.entry(document, PAYLOAD)
            entry["url"] = entry["url"].replace(
                "/download/v%s/" % VERSION, "/download/v9.9.9/")
        self.fixture.mutate(retarget)
        result, _ = self.plan()
        self.assertEqual(result.returncode, 1)


# ---------------------------------------------------------------------------
# Hostile manifests
# ---------------------------------------------------------------------------

class TestManifestSafety(SiteBuilderTest):
    def test_refuses_a_path_that_escapes_the_site(self):
        self.fixture.mutate(lambda document: self.fixture
                            .entry(document, PAYLOAD)
                            .__setitem__("path", "../../escaped.zip"))
        result, _ = self.fixture.build()
        self.assertEqual(result.returncode, 1)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "escaped.zip")))

    def test_refuses_an_absolute_path(self):
        escape = os.path.join(self.tmp, "absolute.zip")
        self.fixture.mutate(lambda document: self.fixture
                            .entry(document, PAYLOAD)
                            .__setitem__("path", escape))
        result, _ = self.fixture.build()
        self.assertEqual(result.returncode, 1)
        self.assertFalse(os.path.exists(escape))

    def test_refuses_an_art_path_that_escapes_the_site(self):
        def escape(document):
            self.fixture.entry(document, PAYLOAD)["art"][0]["path"] = \
                "../../art.png"
        self.fixture.mutate(escape)
        result, _ = self.fixture.build()
        self.assertEqual(result.returncode, 1)

    def test_refuses_a_zip_root_that_is_not_the_addon_id(self):
        self.fixture.mutate(lambda document: self.fixture
                            .entry(document, REPOSITORY)
                            .__setitem__("zip_root", "anything/"))
        result, _ = self.fixture.build()
        self.assertEqual(result.returncode, 1)

    def test_refuses_an_unknown_schema(self):
        self.fixture.mutate(lambda document: document.__setitem__("schema", 99))
        result, _ = self.fixture.build()
        self.assertEqual(result.returncode, 1)
        self.assertIn("schema", (result.stdout + result.stderr).lower())

    def test_refuses_an_index_md5_that_no_longer_matches(self):
        self.fixture.mutate(lambda document: document["index"]
                            .__setitem__("md5", "0" * 32))
        result, _ = self.fixture.build()
        self.assertEqual(result.returncode, 1)

    def test_refuses_an_index_the_manifest_no_longer_describes(self):
        """Kodi polls addons.xml.md5; serving a stale pair breaks every client."""
        path = os.path.join(self.fixture.generated, "addons.xml")
        with open(path, "ab") as stream:
            stream.write(b"<!-- edited after generation -->\n")
        result, _ = self.fixture.build()
        self.assertEqual(result.returncode, 1)
        self.assertIn("md5", (result.stdout + result.stderr).lower())

    def test_refuses_a_sidecar_md5_the_publisher_did_not_write(self):
        """A target tree edited after the push is not silently repaired."""
        path = os.path.join(self.fixture.generated, "addons.xml.md5")
        with open(path, "w", encoding="utf-8") as stream:
            stream.write("0" * 32 + "\n")
        result, _ = self.fixture.build()
        self.assertEqual(result.returncode, 1)
        self.assertIn("addons.xml.md5", result.stdout + result.stderr)

    def test_refuses_a_manifest_that_is_not_json(self):
        with open(self.fixture.manifest_path(), "w", encoding="utf-8") as stream:
            stream.write("{not json")
        result, _ = self.fixture.build()
        self.assertEqual(result.returncode, 1)

    def test_missing_manifest_is_a_usage_error(self):
        os.remove(self.fixture.manifest_path())
        result, _ = self.fixture.build()
        self.assertEqual(result.returncode, 2)


# ---------------------------------------------------------------------------
# The builder itself
# ---------------------------------------------------------------------------

class TestBuilderShape(unittest.TestCase):
    def test_never_reaches_the_network(self):
        """The workflow downloads; the builder only ever verifies local bytes."""
        source = read_bytes(BUILD_SITE).decode("utf-8")
        for forbidden in ("urllib", "requests", "http.client", "socket",
                          "subprocess"):
            self.assertNotIn("import %s" % forbidden, source)

    def test_refuses_a_non_empty_output_directory(self):
        tmp = tempfile.mkdtemp(prefix="site-out-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        fixture = SiteFixture(tmp)
        out = os.path.join(tmp, "_site")
        os.makedirs(out)
        with open(os.path.join(out, "stale.txt"), "w", encoding="utf-8") as stream:
            stream.write("left over\n")
        result, _ = fixture.build(out=out)
        self.assertEqual(result.returncode, 1)
        self.assertIn("non-empty", result.stdout + result.stderr)
        self.assertTrue(os.path.isfile(os.path.join(out, "stale.txt")))


if __name__ == "__main__":
    unittest.main(verbosity=2)
