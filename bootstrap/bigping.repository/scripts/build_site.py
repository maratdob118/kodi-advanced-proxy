#!/usr/bin/env python3
"""Build the GitHub Pages site this repository serves to Kodi.

This script lives in maratdob118/kodi-addons. It is bootstrapped there
by hand, together with the workflow that runs it, and is NOT part of the tree
maratdob118/kodi-advanced-proxy pushes: the release token that writes this
repository holds
Contents:write only and can never touch `.github/`.

The Git tree here is text. The add-on payload is not: the universal Advanced
Proxy ZIP is about 235 MB, far past GitHub's 100 MB blob limit, so it lives as
a Release asset in the source repository and reaches users through a Pages
deployment instead of a Git object.

`manifest.json` is the whole contract between the two repositories. It names
the release asset, the digest and size measured on it at build time, where each
ZIP is published, and which art the payload carries. This script turns that
plan plus one already-downloaded payload into the served tree:

    addons.xml, addons.xml.md5
    service.advancedproxy/service.advancedproxy-<version>.zip[.sha256]
    service.advancedproxy/resources/<art declared by addons.xml>
    repository.bigping/repository.bigping-<version>.zip[.sha256]

Three modes, in the order the workflow uses them:

    --plan      validate the manifest and print url/sha256/size for the
                downloader, one `key=value` line each
    --verify    hash the downloaded payload and compare it against the
                manifest, before anything is built
    (default)   build the site into --out

The payload is a download, so nothing about it is trusted: it is published only
when its bytes hash to the recorded digest, only from the one release URL the
manifest's own `release` fields reconstruct, and every path the manifest names
is checked to stay inside the site. Kodi resolves a ZIP's digest from a
`content-sha256` response header first and falls back to a `<zip>.sha256`
sidecar; Pages cannot set headers, so every published ZIP gets a sidecar.

Only the current version is deployed. A Pages deployment replaces the whole
site, so old versions fall away with it and the artifact stays far below the
1 GB ceiling; the source repository's releases keep every historic asset.

The output is byte-reproducible, and the site is moved into place only once it
is complete, so a refused payload can never leave a half-built tree behind.

Exit 0 on success, 1 when building is refused, 2 on usage errors.
"""

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import zipfile
import zlib

PAYLOAD = "service.advancedproxy"
SOURCE_REPO = "maratdob118/kodi-advanced-proxy"
RELEASE_ASSET = "https://github.com/%(repo)s/releases/download/%(tag)s/%(asset)s"
SCHEMA = 1
ADDONS_XML = "addons.xml"
ADDONS_XML_MD5 = "addons.xml.md5"
RELEASE_ORIGIN = "release-asset"
BUILD_ORIGIN = "build"
VERSION_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
ADDON_ID_RE = re.compile(r"^[a-z0-9]+(?:\.[a-z0-9]+)+$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
MD5_RE = re.compile(r"^[0-9a-f]{32}$")
SEGMENT_RE = re.compile(r"^[A-Za-z0-9._-]+$")
ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)
COMPRESS_LEVEL = 9
FILE_MODE = 0o644
DIR_MODE = 0o755
CHUNK = 1 << 20


class SiteError(Exception):
    """A refusal to build: the manifest or the payload cannot be trusted."""


class UsageError(Exception):
    """A misconfiguration: bad arguments or a missing manifest."""


# -- reading the manifest ----------------------------------------------------

def safe_relative(value, what):
    """A manifest-supplied path must stay inside the site, always."""
    if not isinstance(value, str) or not value:
        raise SiteError("%s is not a path: %r" % (what, value))
    if value != value.strip() or os.path.isabs(value) or "\\" in value:
        raise SiteError("%s must be a plain relative path: %r" % (what, value))
    for segment in value.split("/"):
        if segment in (".", "..") or not SEGMENT_RE.match(segment):
            raise SiteError("%s has an unsafe path segment: %r" % (what, value))
    return value


def require(mapping, key, what):
    if not isinstance(mapping, dict) or key not in mapping:
        raise SiteError("%s declares no %s" % (what, key))
    return mapping[key]


def check_common(entry, position):
    """The fields every addon entry carries, whatever produced its ZIP."""
    what = "addons[%d]" % position
    addon_id = require(entry, "id", what)
    if not isinstance(addon_id, str) or not ADDON_ID_RE.match(addon_id):
        raise SiteError("%s has an unusable addon id: %r" % (what, addon_id))
    version = require(entry, "version", what)
    if not isinstance(version, str) or not VERSION_RE.match(version):
        raise SiteError("%s declares a non X.Y.Z version: %r" % (addon_id, version))
    path = safe_relative(require(entry, "path", what), "%s path" % addon_id)
    canonical = "%s/%s-%s.zip" % (addon_id, addon_id, version)
    if path != canonical:
        raise SiteError("%s publishes %s, but Kodi resolves only %s"
                        % (addon_id, path, canonical))
    sha256_path = safe_relative(require(entry, "sha256_path", what),
                                "%s sha256_path" % addon_id)
    if sha256_path != path + ".sha256":
        raise SiteError("%s puts its digest at %s, expected %s.sha256"
                        % (addon_id, sha256_path, path))
    zip_root = require(entry, "zip_root", what)
    if zip_root != "%s/" % addon_id:
        raise SiteError("%s declares zip_root %r; Kodi rejects any root but %s/"
                        % (addon_id, zip_root, addon_id))
    return {"id": addon_id, "version": version, "path": path,
            "sha256_path": sha256_path, "zip_root": zip_root}


def check_release_entry(entry, checked):
    """A payload that is downloaded: pin where from, and what it must hash to."""
    addon_id = checked["id"]
    version = checked["version"]
    release = require(entry, "release", addon_id)
    repo = require(release, "repo", "%s release" % addon_id)
    tag = require(release, "tag", "%s release" % addon_id)
    asset = require(release, "asset", "%s release" % addon_id)
    if repo != SOURCE_REPO:
        raise SiteError("%s wants its payload from %r; this repository serves "
                        "only assets released by %s" % (addon_id, repo, SOURCE_REPO))
    if tag != "v%s" % version:
        raise SiteError("%s is version %s but points at release %r"
                        % (addon_id, version, tag))
    if asset != "%s-%s.zip" % (addon_id, version):
        raise SiteError("%s expects asset %r, which is not %s-%s.zip"
                        % (addon_id, asset, addon_id, version))
    url = require(entry, "url", addon_id)
    expected = RELEASE_ASSET % {"repo": repo, "tag": tag, "asset": asset}
    if url != expected:
        raise SiteError("%s would be downloaded from %r, not from its own "
                        "release asset %s" % (addon_id, url, expected))
    digest = require(entry, "sha256", addon_id)
    if not isinstance(digest, str) or not SHA256_RE.match(digest):
        raise SiteError("%s records %r, which is not a sha256" % (addon_id, digest))
    size = require(entry, "size", addon_id)
    if not isinstance(size, int) or isinstance(size, bool) or size <= 0:
        raise SiteError("%s records size %r" % (addon_id, size))
    art = []
    for position, item in enumerate(entry.get("art") or []):
        what = "%s art[%d]" % (addon_id, position)
        source = safe_relative(require(item, "source", what), "%s source" % what)
        art_path = safe_relative(require(item, "path", what), "%s path" % what)
        art_digest = require(item, "sha256", what)
        if not isinstance(art_digest, str) or not SHA256_RE.match(art_digest):
            raise SiteError("%s records %r, which is not a sha256"
                            % (what, art_digest))
        art.append({"source": source, "path": art_path, "sha256": art_digest})
    checked.update({"url": url, "sha256": digest, "size": size, "art": art})
    return checked


def check_build_entry(entry, checked):
    """A ZIP this workflow packs itself, out of one metadata file."""
    checked["metadata"] = safe_relative(
        require(entry, "metadata", checked["id"]),
        "%s metadata" % checked["id"])
    return checked


def read_manifest(path):
    """Parse and structurally validate manifest.json; touch no other file."""
    if not os.path.isfile(path):
        raise UsageError("manifest not found: %s" % path)
    try:
        with open(path, "rb") as stream:
            raw = stream.read()
    except OSError as error:
        raise UsageError("cannot read %s: %s" % (path, error))
    try:
        document = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as error:
        raise SiteError("%s is not valid JSON: %s" % (path, error))
    if not isinstance(document, dict):
        raise SiteError("%s must hold a JSON object" % path)
    if document.get("schema") != SCHEMA:
        raise SiteError("%s declares schema %r, this builder speaks %d"
                        % (path, document.get("schema"), SCHEMA))
    index = require(document, "index", "manifest")
    for key in ("addons_xml", "addons_xml_md5"):
        safe_relative(require(index, key, "index"), "index %s" % key)
    md5 = require(index, "md5", "index")
    if not isinstance(md5, str) or not MD5_RE.match(md5):
        raise SiteError("index records %r, which is not an md5" % md5)

    addons = document.get("addons")
    if not isinstance(addons, list) or not addons:
        raise SiteError("%s declares no addons" % path)
    release, builds = None, []
    for position, entry in enumerate(addons):
        checked = check_common(entry, position)
        origin = require(entry, "origin", checked["id"])
        if origin == RELEASE_ORIGIN:
            if release is not None:
                raise SiteError("two %s entries; exactly one payload is served"
                                % RELEASE_ORIGIN)
            release = check_release_entry(entry, checked)
        elif origin == BUILD_ORIGIN:
            builds.append(check_build_entry(entry, checked))
        else:
            raise SiteError("%s declares unknown origin %r"
                            % (checked["id"], origin))
    if release is None:
        raise SiteError("%s declares no %s entry" % (path, RELEASE_ORIGIN))
    if release["id"] != PAYLOAD:
        raise SiteError("the downloaded payload must be %s, not %s"
                        % (PAYLOAD, release["id"]))
    if not builds:
        raise SiteError("%s declares no %s entry" % (path, BUILD_ORIGIN))
    return {"root": os.path.dirname(os.path.abspath(path)),
            "index": {"addons_xml": index["addons_xml"],
                      "addons_xml_md5": index["addons_xml_md5"], "md5": md5},
            "release": release, "builds": builds}


# -- verifying the download --------------------------------------------------

def hash_file(path, what):
    digest = hashlib.sha256()
    size = 0
    try:
        with open(path, "rb") as stream:
            for block in iter(lambda: stream.read(CHUNK), b""):
                digest.update(block)
                size += len(block)
    except OSError as error:
        raise SiteError("cannot read %s: %s" % (what, error))
    return digest.hexdigest(), size


def verify_payload(plan, payload):
    """The downloaded bytes must be exactly the ones the source repo measured."""
    release = plan["release"]
    if not os.path.isfile(payload):
        raise SiteError("payload not found: %s" % payload)
    digest, size = hash_file(payload, payload)
    if size != release["size"]:
        raise SiteError("%s is %d bytes, the manifest recorded %d; refusing to "
                        "publish it" % (payload, size, release["size"]))
    if digest != release["sha256"]:
        raise SiteError("%s hashes to sha256:%s, the manifest recorded sha256:%s; "
                        "refusing to publish it" % (payload, digest,
                                                    release["sha256"]))
    return digest


# -- writing the site --------------------------------------------------------

def read_file(path, what):
    try:
        with open(path, "rb") as stream:
            return stream.read()
    except OSError as error:
        raise SiteError("cannot read %s: %s" % (what, error))


def check_out(out):
    """Refuse an output path that is not ours to create."""
    out = os.path.abspath(out)
    parent = os.path.dirname(out)
    if parent == out:
        raise SiteError("refusing to write to the filesystem root: %s" % out)
    if not os.path.isdir(parent):
        raise SiteError("output parent directory not found: %s" % parent)
    if os.path.islink(out):
        raise SiteError("refusing to replace a symlink: %s" % out)
    if os.path.exists(out):
        if not os.path.isdir(out):
            raise SiteError("output path is not a directory: %s" % out)
        if os.listdir(out):
            raise SiteError("refusing to build into a non-empty directory: %s"
                            % out)
    return out


def write_site_file(scratch, relative, content):
    path = os.path.join(scratch, relative.replace("/", os.sep))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as stream:
        stream.write(content)
    return path


def write_sidecar(scratch, entry, digest):
    """Kodi's only digest channel here: Pages cannot set a response header."""
    write_site_file(scratch, entry["sha256_path"],
                    (digest + "\n").encode("utf-8"))


def publish_payload(scratch, plan, payload, digest):
    release = plan["release"]
    path = os.path.join(scratch, release["path"].replace("/", os.sep))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    try:
        shutil.copyfile(payload, path)
    except OSError as error:
        raise SiteError("cannot publish %s: %s" % (release["path"], error))
    write_sidecar(scratch, release, digest)


def publish_art(scratch, plan, payload):
    """Publish the icon and fanart addons.xml points at, from the payload."""
    release = plan["release"]
    if not release["art"]:
        return
    try:
        with zipfile.ZipFile(payload) as archive:
            names = set(archive.namelist())
            for item in release["art"]:
                if item["source"] not in names:
                    raise SiteError("the payload carries no %s, which the "
                                    "manifest publishes as %s"
                                    % (item["source"], item["path"]))
                content = archive.read(item["source"])
                digest = hashlib.sha256(content).hexdigest()
                if digest != item["sha256"]:
                    raise SiteError("%s hashes to sha256:%s, the manifest "
                                    "recorded sha256:%s"
                                    % (item["source"], digest, item["sha256"]))
                write_site_file(scratch, item["path"], content)
    except (zipfile.BadZipFile, zlib.error, EOFError, ValueError) as error:
        raise SiteError("unreadable payload %s: %s" % (payload, error))
    except OSError as error:
        raise SiteError("cannot open %s: %s" % (payload, error))


def build_addon_zip(scratch, plan, entry):
    """Pack one metadata file into a deterministic, correctly rooted ZIP."""
    source = os.path.join(plan["root"], entry["metadata"].replace("/", os.sep))
    content = read_file(source, entry["metadata"])
    name = "%s%s" % (entry["zip_root"], os.path.basename(entry["metadata"]))
    path = os.path.join(scratch, entry["path"].replace("/", os.sep))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    info = zipfile.ZipInfo(name, ZIP_TIMESTAMP)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.create_system = 3  # unix, so external_attr carries the mode
    info.external_attr = FILE_MODE << 16
    try:
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr(info, content, compresslevel=COMPRESS_LEVEL)
    except OSError as error:
        raise SiteError("cannot write %s: %s" % (entry["path"], error))
    write_sidecar(scratch, entry, hash_file(path, entry["path"])[0])


def publish_index(scratch, plan):
    """Copy the index, refusing a tree whose md5 no longer describes it."""
    index = plan["index"]
    addons_xml = read_file(
        os.path.join(plan["root"], index["addons_xml"].replace("/", os.sep)),
        index["addons_xml"])
    digest = hashlib.md5(addons_xml).hexdigest()
    if digest != index["md5"]:
        raise SiteError("%s hashes to md5:%s, the manifest records md5:%s; the "
                        "tree is stale" % (index["addons_xml"], digest,
                                           index["md5"]))
    recorded = read_file(
        os.path.join(plan["root"], index["addons_xml_md5"].replace("/", os.sep)),
        index["addons_xml_md5"]).decode("utf-8", "replace").strip()
    if recorded != index["md5"]:
        raise SiteError("%s says md5:%s, the manifest records md5:%s"
                        % (index["addons_xml_md5"], recorded, index["md5"]))
    write_site_file(scratch, ADDONS_XML, addons_xml)
    write_site_file(scratch, ADDONS_XML_MD5, (digest + "\n").encode("utf-8"))


def normalize_modes(root):
    """Pin the served tree's modes; mkdtemp makes 0700 and umask varies."""
    os.chmod(root, DIR_MODE)
    for directory, subdirectories, names in os.walk(root):
        for name in subdirectories:
            os.chmod(os.path.join(directory, name), DIR_MODE)
        for name in names:
            os.chmod(os.path.join(directory, name), FILE_MODE)


def build(plan, payload, out):
    """Build the whole site in a scratch dir, then move it into place."""
    digest = verify_payload(plan, payload)
    scratch = tempfile.mkdtemp(prefix=os.path.basename(out) + ".new.",
                               dir=os.path.dirname(out))
    try:
        publish_index(scratch, plan)
        publish_payload(scratch, plan, payload, digest)
        publish_art(scratch, plan, payload)
        for entry in plan["builds"]:
            build_addon_zip(scratch, plan, entry)
        normalize_modes(scratch)
        if os.path.isdir(out):
            os.rmdir(out)  # check_out proved it is empty
        os.rename(scratch, out)
    except OSError as error:
        raise SiteError("cannot write %s: %s" % (out, error))
    finally:
        if os.path.isdir(scratch):
            shutil.rmtree(scratch, ignore_errors=True)
    return digest


# -- entry point -------------------------------------------------------------

def print_plan(plan):
    """One `key=value` line per field, safe to append to $GITHUB_OUTPUT."""
    release = plan["release"]
    print("url=%s" % release["url"])
    print("sha256=%s" % release["sha256"])
    print("size=%d" % release["size"])


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="build_site.py",
        description="Build the Pages site this Kodi repository serves.")
    parser.add_argument("--manifest", default="manifest.json",
                        help="publisher-generated plan (default: %(default)s)")
    parser.add_argument("--payload", help="the downloaded universal ZIP")
    parser.add_argument("--out", help="directory to build the site into")
    parser.add_argument("--plan", action="store_true",
                        help="print url/sha256/size for the downloader and exit")
    parser.add_argument("--verify", action="store_true",
                        help="check the downloaded payload against the manifest "
                             "and exit")
    args = parser.parse_args(argv)

    try:
        if args.plan and args.verify:
            raise UsageError("--plan and --verify are separate steps")
        plan = read_manifest(args.manifest)
        if args.plan:
            print_plan(plan)
            return 0
        if not args.payload:
            raise UsageError("--payload is required")
        if args.verify:
            verify_payload(plan, args.payload)
            print("build_site: OK %s matches the manifest (sha256:%s size=%d)"
                  % (args.payload, plan["release"]["sha256"],
                     plan["release"]["size"]))
            return 0
        if not args.out:
            raise UsageError("--out is required")
        out = check_out(args.out)
        digest = build(plan, args.payload, out)
    except UsageError as error:
        print("build_site: %s" % error, file=sys.stderr)
        return 2
    except SiteError as error:
        print("build_site: %s" % error, file=sys.stderr)
        return 1
    print("build_site: OK %s (%s=%s sha256=%s art=%d addons=%d)"
          % (out, plan["release"]["id"], plan["release"]["version"], digest,
             len(plan["release"]["art"]), 1 + len(plan["builds"])))
    return 0


if __name__ == "__main__":
    sys.exit(main())
