#!/usr/bin/env python3
"""Generate the kodi-addons repository tree (classic Kodi layout).

Turns the two source manifests and one payload ZIP into the tree that
maratdob118/kodi-addons commits and serves from raw.githubusercontent.com:

    zips/addons.xml
    zips/addons.xml.md5
    zips/service.advancedproxy/addon.xml
    zips/service.advancedproxy/service.advancedproxy-<version>.zip
    zips/repository.maratdob118/addon.xml
    zips/repository.maratdob118/repository.maratdob118-<version>.zip
    README.md

Kodi resolves every addon's ZIP as <datadir>/<id>/<id>-<version>.zip, where
<datadir> is the repository addon's datadir URL. The payload ZIP is copied
verbatim; the repository addon ZIP is packed from its addon.xml. Nothing is
committed unless it is safe: identities and versions are checked against the
source manifests, the payload ZIP must be a single-root archive carrying the
matching addon.xml, and the output tree is replaced atomically.

Exit 0 on success, 1 when generation is refused, 2 on usage errors.
"""
import argparse
import hashlib
import os
import shutil
import stat
import sys
import tempfile
import zipfile
import xml.etree.ElementTree as ET

PAYLOAD = "service.advancedproxy"
REPOSITORY = "repository.maratdob118"
ADDONS_XML = "zips/addons.xml"
ADDONS_XML_MD5 = "zips/addons.xml.md5"
README = "README.md"
XML_DECLARATION = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
VERSION_RE = r"^[0-9]+\.[0-9]+\.[0-9]+$"
DIR_MODE = stat.S_IRWXU | stat.S_IRGRP | stat.S_IXGRP | stat.S_IROTH | stat.S_IXOTH
FILE_MODE = stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP | stat.S_IROTH
CHUNK = 1 << 20


class GenerationError(Exception):
    """A refusal to generate: inputs are missing, inconsistent or unsafe."""


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
    if not __import__("re").match(VERSION_RE, version):
        raise GenerationError("%s declares a non X.Y.Z version: %r"
                              % (path, version))
    return root, version, raw


def check_payload(payload_zip, payload_version):
    """Verify the payload ZIP: single root, matching addon.xml, readable."""
    if not os.path.isfile(payload_zip):
        raise GenerationError("payload zip not found: %s" % payload_zip)
    try:
        with zipfile.ZipFile(payload_zip) as archive:
            names = archive.namelist()
            roots = sorted({name.split("/")[0] for name in names})
            if roots != [PAYLOAD]:
                raise GenerationError(
                    "payload zip must hold one %s/ root, found: %s"
                    % (PAYLOAD, ", ".join(roots)))
            inner = "%s/addon.xml" % PAYLOAD
            if inner not in names:
                raise GenerationError("payload zip has no %s: %s"
                                      % (inner, payload_zip))
            embedded = archive.read(inner)
    except (zipfile.BadZipFile, OSError, ValueError) as error:
        raise GenerationError("unreadable payload zip %s: %s"
                              % (payload_zip, error))
    try:
        embedded_version = ET.fromstring(embedded).get("version")
    except ET.ParseError as error:
        raise GenerationError("%s inside %s is not well-formed XML: %s"
                              % (inner, payload_zip, error))
    if embedded_version != payload_version:
        raise GenerationError("%s inside %s says version %s, expected %s"
                              % (inner, os.path.basename(payload_zip),
                                 embedded_version, payload_version))
    digest = hashlib.sha256()
    size = 0
    try:
        with open(payload_zip, "rb") as stream:
            for block in iter(lambda: stream.read(CHUNK), b""):
                digest.update(block)
                size += len(block)
    except OSError as error:
        raise GenerationError("cannot hash %s: %s" % (payload_zip, error))
    return {"sha256": digest.hexdigest(), "size": size}


def build_addons_xml(roots):
    """Wrap the addon manifests in one <addons> index, id-sorted."""
    blocks = []
    for root in sorted(roots, key=lambda element: element.get("id")):
        root.tail = None
        blocks.append(ET.tostring(root, encoding="unicode").rstrip())
    text = "%s\n<addons>\n%s\n</addons>\n" % (XML_DECLARATION, "\n".join(blocks))
    return text.encode("utf-8")


def pack_repository_zip(addon_xml_bytes, version):
    """Pack the repository addon into a deterministic single-root ZIP."""
    buffer = tempfile.SpooledTemporaryFile(max_size=1 << 20)
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        info = zipfile.ZipInfo("%s/addon.xml" % REPOSITORY,
                               (1980, 1, 1, 0, 0, 0))
        info.create_system = 3
        info.external_attr = FILE_MODE << 16
        archive.writestr(info, addon_xml_bytes)
    buffer.seek(0)
    payload = buffer.read()
    buffer.close()
    return payload


def build_readme(payload_version, repository_version):
    return ("""# maratdob118 Kodi repository

Generated tree. Do not edit by hand: every file here is produced by
`scripts/generate_repo.py` in
[maratdob118/kodi-advanced-proxy](https://github.com/maratdob118/kodi-advanced-proxy)
and pushed by `scripts/publish_repo.py` on every release.

## Installing

1. Download `%(repository_path)s` from this repository
   (`https://raw.githubusercontent.com/maratdob118/kodi-addons/main/%(repository_path)s`).
2. In Kodi: **Add-ons -> Install from zip file**, pick that ZIP.
3. **Add-ons -> Install from repository -> maratdob118 Repository ->
   Services -> Advanced Proxy**.

Kodi 19 (Matrix) or newer is required. Updates arrive automatically once the
repository add-on is installed; Kodi re-reads
`https://raw.githubusercontent.com/maratdob118/kodi-addons/main/%(index)s`.

## Contents

| File | Role |
| --- | --- |
| `%(index)s` | index of every add-on version this repository offers |
| `%(md5)s` | md5 of `%(index)s`; Kodi polls it to detect changes |
| `%(payload_path)s` | Advanced Proxy payload (all platforms the repo ships) |
| `%(repository_path)s` | the repository add-on users install first |
""" % {
        "index": ADDONS_XML,
        "md5": ADDONS_XML_MD5,
        "payload": PAYLOAD,
        "payload_path": "zips/%s/%s-%s.zip" % (PAYLOAD, PAYLOAD, payload_version),
        "repository_path": "zips/%s/%s-%s.zip"
                           % (REPOSITORY, REPOSITORY, repository_version),
    }).encode("utf-8")


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
    """Pin the generated tree's modes; mkdtemp makes 0700 and umask varies."""
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


def generate(repo, out, payload_zip, version=None):
    """Build the whole tree in memory, then write it atomically."""
    payload_xml = os.path.join(repo, PAYLOAD, "addon.xml")
    repository_xml = os.path.join(repo, REPOSITORY, "addon.xml")
    payload_root, payload_version, _ = read_manifest(payload_xml, PAYLOAD)
    repository_root, repository_version, repository_raw = read_manifest(
        repository_xml, REPOSITORY)
    if version and version != payload_version:
        raise GenerationError("%s says version %s, expected %s"
                              % (payload_xml, payload_version, version))

    out = check_output(out, repo)
    payload_zip = payload_zip or os.path.join(
        repo, "dist", "%s-%s.zip" % (PAYLOAD, payload_version))
    measured = check_payload(payload_zip, payload_version)

    addons_xml = build_addons_xml([payload_root, repository_root])
    md5 = hashlib.md5(addons_xml).hexdigest()
    repository_zip = pack_repository_zip(repository_raw, repository_version)
    files = {
        ADDONS_XML: addons_xml,
        ADDONS_XML_MD5: (md5 + "\n").encode("utf-8"),
        README: build_readme(payload_version, repository_version),
        os.path.join("zips", PAYLOAD, "addon.xml"):
            open(payload_xml, "rb").read(),
        os.path.join("zips", PAYLOAD, "%s-%s.zip" % (PAYLOAD, payload_version)):
            open(payload_zip, "rb").read(),
        os.path.join("zips", REPOSITORY, "addon.xml"): repository_raw,
        os.path.join("zips", REPOSITORY,
                     "%s-%s.zip" % (REPOSITORY, repository_version)):
            repository_zip,
    }
    replace_tree(out, files)
    return {"out": out, "md5": md5, "sha256": measured["sha256"],
            "size": measured["size"], "payload_version": payload_version,
            "repository_version": repository_version}


def main(argv=None):
    here = os.path.dirname(os.path.abspath(__file__))
    parser = argparse.ArgumentParser(
        prog="generate_repo.py",
        description="Generate the kodi-addons repository tree.")
    parser.add_argument("--repo", default=os.path.dirname(here),
                        help="repo root holding the addon sources")
    parser.add_argument("--out", required=True,
                        help="directory to (re)generate; replaced wholesale")
    parser.add_argument("--payload", help="payload ZIP (default: "
                                          "<repo>/dist/%s-<version>.zip)"
                                          % PAYLOAD)
    parser.add_argument("--version", help="expected addon version X.Y.Z "
                                          "(default: from addon.xml)")
    args = parser.parse_args(argv)

    repo = os.path.abspath(args.repo)
    if not os.path.isdir(repo):
        parser.error("repo dir not found: %s" % repo)
    if args.version is not None and not __import__("re").match(VERSION_RE,
                                                               args.version):
        parser.error("version must be X.Y.Z: %s" % args.version)

    try:
        result = generate(repo, args.out,
                          os.path.abspath(args.payload) if args.payload else None,
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
