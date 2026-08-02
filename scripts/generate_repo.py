#!/usr/bin/env python3
"""Generate the text-only kodi-addons tree.

The target repository must stay text-only: the universal payload ZIP is about
235 MB and GitHub refuses any Git blob over 100 MB.  So this script reads the
universal ZIP but never copies it.  It emits the repository index plus a
manifest that tells the target Pages workflow which public Release asset to
download, how to verify it, and where to publish it:

    <out>/addons.xml                    index of every offered addon version
    <out>/addons.xml.md5                md5 Kodi polls for index changes
    <out>/manifest.json                 download/publish plan for Pages
    <out>/README.md                     what the tree is and how to install
    <out>/repository.bigping/addon.xml  metadata Pages packs into the repo ZIP

Both canonical datadir paths compose as <addon.id>/<addon.id>-<version>.zip,
which is the only shape Kodi resolves from an `addons.xml` entry.

The recorded SHA256 and size are measured on the LOCAL universal ZIP, so this
must run against the exact artifact that is uploaded to the release, in the same
CI step as that upload.  Nothing here is fetched from the network and nothing
here proves the release asset exists: the manifest states the expected digest,
and the Pages workflow is instructed to download the asset, recompute, compare,
and abort the deployment on any mismatch.

The output is byte-reproducible: identical inputs give identical bytes, no
timestamps or filesystem order leak in, and a run replaces the previous tree
wholesale so stale files cannot survive a regeneration.

Exit 0 on success, 1 when generation is refused, 2 on usage errors.
"""

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import xml.etree.ElementTree as ET
import zipfile
import zlib

PAYLOAD = "service.advancedproxy"
REPOSITORY = "repository.bigping"
PAGES = "https://maratdob118.github.io/kodi-addons/"
SOURCE_REPO = "maratdob118/kodi-advanced-proxy"
RELEASE_ASSET = "https://github.com/%(repo)s/releases/download/%(tag)s/%(asset)s"
DIR_MODE = 0o755
FILE_MODE = 0o644
ADDONS_XML = "addons.xml"
ADDONS_XML_MD5 = ADDONS_XML + ".md5"
MANIFEST = "manifest.json"
XML_DECLARATION = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
SCHEMA = 1
VERSION_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
CHUNK = 1 << 20


class GenerationError(Exception):
    """A refusal to generate: inputs are missing, inconsistent or unsafe."""


# -- inputs ------------------------------------------------------------------

def read_manifest(path, addon_id):
    """Parse the addon.xml at PATH, checking its identity and version."""
    try:
        with open(path, "rb") as stream:
            raw = stream.read()
    except OSError as error:
        raise GenerationError("cannot read %s: %s" % (path, error))
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as error:
        raise GenerationError("%s is not well-formed XML: %s" % (path, error))
    if root.tag != "addon":
        raise GenerationError("%s has root <%s>, expected <addon>"
                              % (path, root.tag))
    if root.get("id") != addon_id:
        raise GenerationError("%s declares id %r, expected %r"
                              % (path, root.get("id"), addon_id))
    version = root.get("version") or ""
    if not VERSION_RE.match(version):
        raise GenerationError("%s declares a non X.Y.Z version: %r"
                              % (path, version))
    return root, version, raw


def parse_assets(root, path):
    """(kind, reference) for every asset the manifest declares."""
    metadata = next((element for element in root.iter("extension")
                     if element.get("point") == "xbmc.addon.metadata"), None)
    assets = None if metadata is None else metadata.find("assets")
    declared = []
    for child in assets if assets is not None else ():
        reference = (child.text or "").strip()
        if not reference:
            continue
        if reference.startswith("/") or ".." in reference.split("/"):
            raise GenerationError("%s declares an unsafe <%s> asset: %r"
                                  % (path, child.tag, reference))
        declared.append((child.tag, reference))
    return declared


def read_universal(path, version, assets):
    """Check the universal ZIP, hash it, and locate the art Pages must publish."""
    expected = "%s-%s.zip" % (PAYLOAD, version)
    if os.path.basename(path) != expected:
        raise GenerationError("universal zip must be named %s, got %s"
                              % (expected, os.path.basename(path)))
    if not os.path.isfile(path):
        raise GenerationError("universal zip not found: %s" % path)
    inner = "%s/addon.xml" % PAYLOAD
    art = []
    try:
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
            roots = sorted({name.split("/")[0] for name in names})
            if roots != [PAYLOAD]:
                raise GenerationError(
                    "universal zip must hold one %s/ root, found: %s"
                    % (PAYLOAD, ", ".join(roots)))
            if inner not in names:
                raise GenerationError("universal zip has no %s: %s"
                                      % (inner, path))
            embedded = archive.read(inner)
            for kind, reference in assets:
                entry = "%s/%s" % (PAYLOAD, reference)
                if entry not in names:
                    raise GenerationError(
                        "%s declares <%s>%s</%s>, but the universal zip has no %s"
                        % (inner, kind, reference, kind, entry))
                art.append({
                    "kind": kind,
                    "origin": "payload-zip",
                    "source": entry,
                    "path": entry,
                    "sha256": hashlib.sha256(archive.read(entry)).hexdigest(),
                })
    except (zipfile.BadZipFile, zlib.error, EOFError, ValueError) as error:
        raise GenerationError("unreadable universal zip %s: %s" % (path, error))
    except OSError as error:
        raise GenerationError("cannot open %s: %s" % (path, error))
    try:
        embedded_version = ET.fromstring(embedded).get("version")
    except ET.ParseError as error:
        raise GenerationError("%s inside %s is not well-formed XML: %s"
                              % (inner, path, error))
    if embedded_version != version:
        raise GenerationError("%s inside %s says version %s, expected %s"
                              % (inner, os.path.basename(path),
                                 embedded_version, version))
    digest = hashlib.sha256()
    size = 0
    try:
        with open(path, "rb") as stream:
            for block in iter(lambda: stream.read(CHUNK), b""):
                digest.update(block)
                size += len(block)
    except OSError as error:
        raise GenerationError("cannot hash %s: %s" % (path, error))
    return {"sha256": digest.hexdigest(), "size": size, "art": art}


# -- generated text ----------------------------------------------------------

def canonical_path(addon_id, version):
    """The one datadir path Kodi resolves for an addons.xml entry."""
    return "%s/%s-%s.zip" % (addon_id, addon_id, version)


def build_addons_xml(roots):
    """Wrap the addon manifests in one <addons> index, id-sorted."""
    blocks = []
    for root in sorted(roots, key=lambda element: element.get("id")):
        root.tail = None
        blocks.append(ET.tostring(root, encoding="unicode").rstrip())
    text = "%s\n<addons>\n%s\n</addons>\n" % (XML_DECLARATION, "\n".join(blocks))
    return text.encode("utf-8")


def build_manifest(payload_version, repository_version, universal, md5):
    """The download/publish plan the target Pages workflow consumes.

    Kodi resolves a ZIP's digest from a content-sha256 response header first and
    falls back to a <zip>.sha256 sidecar served next to it.  Pages cannot set
    response headers, so the sidecar is the only mechanism available here and
    every published ZIP needs one.
    """
    tag = "v%s" % payload_version
    asset = "%s-%s.zip" % (PAYLOAD, payload_version)
    addons = [
        {
            "id": PAYLOAD,
            "version": payload_version,
            "origin": "release-asset",
            "release": {"repo": SOURCE_REPO, "tag": tag, "asset": asset},
            "url": RELEASE_ASSET % {"repo": SOURCE_REPO, "tag": tag,
                                    "asset": asset},
            "sha256": universal["sha256"],
            "sha256_path": canonical_path(PAYLOAD, payload_version) + ".sha256",
            "size": universal["size"],
            "path": canonical_path(PAYLOAD, payload_version),
            "zip_root": "%s/" % PAYLOAD,
            "art": universal["art"],
        },
        {
            "id": REPOSITORY,
            "version": repository_version,
            "origin": "build",
            "metadata": "%s/addon.xml" % REPOSITORY,
            "path": canonical_path(REPOSITORY, repository_version),
            "sha256_path": canonical_path(REPOSITORY,
                                          repository_version) + ".sha256",
            "zip_root": "%s/" % REPOSITORY,
        },
    ]
    document = {
        "schema": SCHEMA,
        "generator": "scripts/generate_repo.py",
        "datadir": PAGES,
        "verification": {
            "algorithm": "sha256",
            "policy": "download-then-compare",
            "measured_on": "local-build-artifact",
            "remote_verified": False,
            "instruction":
                "For every addon with origin release-asset: download url, "
                "recompute sha256 and size, compare them against the recorded "
                "values, and abort the deployment on any mismatch. The recorded "
                "digest is an expectation measured on the local build artifact "
                "staged for the release upload; nothing here was fetched from "
                "the network. Publish each zip at path, write its lowercase hex "
                "digest to sha256_path, pack build entries so their single root "
                "directory is zip_root, and extract every art entry from the "
                "payload zip source to its path.",
        },
        "index": {
            "addons_xml": ADDONS_XML,
            "addons_xml_md5": ADDONS_XML_MD5,
            "md5": md5,
        },
        "addons": sorted(addons, key=lambda entry: entry["id"]),
    }
    dumped = json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False)
    return (dumped + "\n").encode("utf-8")


def build_readme(payload_version, repository_version):
    return ("""# BigPing Kodi repository

Generated tree. Do not edit by hand: every file here is produced by
`scripts/generate_repo.py` in [maratdob118/kodi-advanced-proxy](https://github.com/maratdob118/kodi-advanced-proxy)
and overwritten on the next run.

This repository stays text-only in Git. Add-on ZIPs are never committed; they
are published as a GitHub Pages deployment, because the universal Advanced
Proxy ZIP is far larger than GitHub's 100 MB blob limit.

## Contents

| File | Role |
| --- | --- |
| `%(index)s` | index of every add-on version this repository offers |
| `%(md5)s` | md5 of `%(index)s`; Kodi polls it to detect changes |
| `%(manifest)s` | which Release asset Pages downloads, its SHA256, and where it is published |
| `%(repository)s/addon.xml` | metadata Pages packs into the repository add-on ZIP |

## Publishing (what the Pages workflow must do)

`%(manifest)s` is the whole contract. For each entry under `addons`:

- `origin: release-asset` — download `url`, recompute its SHA256 and size,
  compare them against the recorded `sha256`/`size`, and abort the deployment on
  any mismatch. Those values are expectations measured on the build artifact
  staged for the release upload, not properties verified against the remote
  asset; `release.tag` pins which release the bytes must come from.
- `origin: build` — pack `metadata` into a ZIP whose single top-level directory
  is `zip_root`. Kodi rejects an add-on ZIP with any other root.
- Publish every ZIP at `path` and write its lowercase hex digest to
  `sha256_path`. Kodi reads a `content-sha256` response header first and falls
  back to that `<zip>.sha256` sidecar; Pages cannot set response headers, so the
  sidecar is the only mechanism available and is mandatory.
- Extract every `art` entry from the payload ZIP at `source` and publish it at
  `path`, so the icon and fanart that `%(index)s` resolves actually exist.

## Offered add-ons

| Add-on | Version | Published path |
| --- | --- | --- |
| `%(repository)s` | %(repository_version)s | `%(repository_path)s` |
| `%(payload)s` | %(payload_version)s | `%(payload_path)s` |

## Installing

1. Download `%(repository_path)s` from %(pages)s
2. In Kodi: **Add-ons -> Install from zip file**, pick that ZIP.
3. **Add-ons -> Install from repository -> BigPing -> Services -> Advanced Proxy**.

Kodi 20 (Nexus) or newer is required. Updates arrive automatically once the
repository add-on is installed; Kodi re-reads %(pages)s%(index)s.
""" % {
        "index": ADDONS_XML,
        "md5": ADDONS_XML_MD5,
        "manifest": MANIFEST,
        "pages": PAGES,
        "payload": PAYLOAD,
        "payload_version": payload_version,
        "payload_path": canonical_path(PAYLOAD, payload_version),
        "repository": REPOSITORY,
        "repository_version": repository_version,
        "repository_path": canonical_path(REPOSITORY, repository_version),
    }).encode("utf-8")


# -- output ------------------------------------------------------------------

def check_output(out, repo):
    """Refuse output paths that are not ours to replace."""
    out = os.path.abspath(out)
    parent = os.path.dirname(out)
    if parent == out:
        raise GenerationError("refusing to write to the filesystem root: %s" % out)
    if ".git" in out.split(os.sep):
        raise GenerationError("refusing to write inside a git directory: %s" % out)
    if os.path.realpath(out) == os.path.realpath(repo):
        raise GenerationError("refusing to overwrite the source repo: %s" % out)
    if not os.path.isdir(parent):
        raise GenerationError("output parent directory not found: %s" % parent)
    if os.path.islink(out):
        raise GenerationError("refusing to replace a symlink: %s" % out)
    if os.path.exists(out):
        if not os.path.isdir(out):
            raise GenerationError("output path is not a directory: %s" % out)
        if os.listdir(out) and not os.path.isfile(os.path.join(out, ADDONS_XML)):
            raise GenerationError(
                "refusing to replace %s: not a generated tree (no %s)"
                % (out, ADDONS_XML))
    return out


def normalize_modes(root):
    """Pin the served tree's modes; mkdtemp makes 0700 and umask varies."""
    os.chmod(root, DIR_MODE)
    for directory, subdirectories, names in os.walk(root):
        for name in subdirectories:
            os.chmod(os.path.join(directory, name), DIR_MODE)
        for name in names:
            os.chmod(os.path.join(directory, name), FILE_MODE)


def replace_tree(out, files):
    """Swap FILES in as OUT, dropping any stale tree."""
    parent = os.path.dirname(out)
    scratch = tempfile.mkdtemp(prefix=os.path.basename(out) + ".new.", dir=parent)
    previous = os.path.join(
        parent, "%s.old.%d" % (os.path.basename(out), os.getpid()))
    try:
        for relative in sorted(files):
            target = os.path.join(scratch, relative)
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with open(target, "wb") as stream:
                stream.write(files[relative])
        normalize_modes(scratch)
        if os.path.exists(previous):
            shutil.rmtree(previous)
        staged = os.path.exists(out)
        if staged:
            os.rename(out, previous)
        try:
            os.rename(scratch, out)
        except OSError:
            if staged:
                os.rename(previous, out)
            raise
        if staged:
            shutil.rmtree(previous)
    except OSError as error:
        raise GenerationError("cannot write %s: %s" % (out, error))
    finally:
        if os.path.isdir(scratch):
            shutil.rmtree(scratch, ignore_errors=True)


def generate(repo, out, universal, version=None):
    """Build the whole tree in memory, then publish it atomically."""
    payload_xml = os.path.join(repo, PAYLOAD, "addon.xml")
    repository_xml = os.path.join(repo, REPOSITORY, "addon.xml")
    payload_root, payload_version, _ = read_manifest(payload_xml, PAYLOAD)
    repository_root, repository_version, repository_raw = read_manifest(
        repository_xml, REPOSITORY)
    if version and version != payload_version:
        raise GenerationError("%s says version %s, expected %s"
                              % (payload_xml, payload_version, version))

    out = check_output(out, repo)
    universal = universal or os.path.join(
        repo, "dist", "%s-%s.zip" % (PAYLOAD, payload_version))
    assets = parse_assets(payload_root, payload_xml)
    measured = read_universal(universal, payload_version, assets)

    addons_xml = build_addons_xml([payload_root, repository_root])
    md5 = hashlib.md5(addons_xml).hexdigest()
    files = {
        ADDONS_XML: addons_xml,
        ADDONS_XML_MD5: (md5 + "\n").encode("utf-8"),
        MANIFEST: build_manifest(payload_version, repository_version,
                                 measured, md5),
        "README.md": build_readme(payload_version, repository_version),
        os.path.join(REPOSITORY, "addon.xml"): repository_raw,
    }
    replace_tree(out, files)
    return {"out": out, "md5": md5, "sha256": measured["sha256"],
            "size": measured["size"], "art": len(measured["art"]),
            "payload_version": payload_version,
            "repository_version": repository_version}


def main(argv=None):
    here = os.path.dirname(os.path.abspath(__file__))
    parser = argparse.ArgumentParser(
        prog="generate_repo.py",
        description="Generate the text-only kodi-addons tree.")
    parser.add_argument("--repo", default=os.path.dirname(here),
                        help="repo root holding the addon sources")
    parser.add_argument("--out", required=True,
                        help="directory to (re)generate; replaced wholesale")
    parser.add_argument("--universal", help="universal payload ZIP (default: "
                                            "<repo>/dist/%s-<version>.zip)"
                                            % PAYLOAD)
    parser.add_argument("--version", help="expected addon version X.Y.Z "
                                          "(default: from addon.xml)")
    args = parser.parse_args(argv)

    repo = os.path.abspath(args.repo)
    if not os.path.isdir(repo):
        parser.error("repo dir not found: %s" % repo)
    if args.version is not None and not VERSION_RE.match(args.version):
        parser.error("version must be X.Y.Z: %s" % args.version)

    try:
        result = generate(repo, args.out,
                          os.path.abspath(args.universal) if args.universal
                          else None,
                          args.version)
    except GenerationError as error:
        print("generate_repo: %s" % error, file=sys.stderr)
        return 1

    print("generate_repo: OK %s (%s=%s %s=%s md5=%s sha256=%s size=%d)"
          % (result["out"], PAYLOAD, result["payload_version"],
             REPOSITORY, result["repository_version"], result["md5"],
             result["sha256"], result["size"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
