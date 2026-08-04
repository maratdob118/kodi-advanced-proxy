#!/usr/bin/env python3
"""Assemble the deterministic universal Advanced Proxy ZIP.

Kodi cannot pick an OS-specific ZIP out of a release, so the datadir payload
has to be a single archive carrying every platform's engine binaries.  This
script merges the per-platform ZIPs built by build.sh into

    dist/service.advancedproxy-<X.Y.Z>.zip

and refuses to produce anything it cannot vouch for:

  * exactly one platform ZIP per platform declared in build.sh
  * every input readable, without duplicate names or nested platform ZIPs
  * resources/bin/<platform> trees taken only from the matching input
  * all other ("shared") entries byte-identical across all inputs
  * addon.xml inside the merged payload carrying the target version

The output is byte-reproducible: entries are sorted, timestamps are pinned to
the ZIP epoch, permissions are normalised (engine binaries 0755, everything
else 0644) and every entry is deflated at a fixed level.  Input order, input
timestamps, input modes and input compression cannot leak into the result.

Exit 0 on success, 1 when assembly fails, 2 on usage errors.
"""

import argparse
import hashlib
import os
import re
import sys
import zipfile
import zlib

ADDON = "service.advancedproxy"
BIN_PREFIX = "%s/resources/bin/" % ADDON
ADDON_XML = "%s/addon.xml" % ADDON
VERSION_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
ADDON_VERSION_RE = re.compile(r"<addon[^>]*version=\"([^\"]+)\"")
PLATFORMS_RE = re.compile(r"^PLATFORMS=\((.*)\)$", re.MULTILINE)
PLATFORM_RE = re.compile(r"^[a-z0-9_]+$")
PLATFORM_ZIP_RE = re.compile(
    r"^%s-[0-9]+\.[0-9]+\.[0-9]+\.[a-z0-9_]+\.zip$" % re.escape(ADDON)
)
ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)
COMPRESS_LEVEL = 9
EXECUTABLE_NAMES = frozenset(("sing-box", "sing-box.exe", "xray", "xray.exe"))


class AssemblyError(Exception):
    """A refusal to assemble: the inputs are missing, mixed or corrupt."""


def digest(payload):
    return hashlib.sha256(payload).hexdigest()[:16]


def read_text(path, what):
    try:
        with open(path, encoding="utf-8") as stream:
            return stream.read()
    except OSError as error:
        raise AssemblyError("cannot read %s: %s" % (what, error))


def read_platforms(build_sh):
    """The platform list build.sh ships, in declaration order."""
    match = PLATFORMS_RE.search(read_text(build_sh, "build.sh"))
    if not match:
        raise AssemblyError("no PLATFORMS=(...) declaration in %s" % build_sh)
    platforms = match.group(1).split()
    if not platforms:
        raise AssemblyError("empty PLATFORMS=() declaration in %s" % build_sh)
    for platform in platforms:
        if not PLATFORM_RE.match(platform):
            raise AssemblyError("invalid platform in %s: %r" % (build_sh, platform))
    if len(set(platforms)) != len(platforms):
        raise AssemblyError("duplicate platforms in %s: %s"
                            % (build_sh, " ".join(sorted(platforms))))
    return platforms


def parse_addon_version(text, what):
    match = ADDON_VERSION_RE.search(text)
    if not match:
        raise AssemblyError("no addon version in %s" % what)
    version = match.group(1)
    if not VERSION_RE.match(version):
        raise AssemblyError("%s declares a non X.Y.Z version: %r" % (what, version))
    return version


def discover_platform_zip(dist, platform, version):
    """The single platform ZIP for PLATFORM, or a refusal explaining why not."""
    pattern = re.compile(
        r"^%s-([0-9]+\.[0-9]+\.[0-9]+)\.%s\.zip$"
        % (re.escape(ADDON), re.escape(platform))
    )
    try:
        found = sorted(name for name in os.listdir(dist) if pattern.match(name))
    except OSError as error:
        raise AssemblyError("cannot list %s: %s" % (dist, error))
    if not found:
        raise AssemblyError("no %s-%s.%s.zip in %s"
                            % (ADDON, version, platform, dist))
    if len(found) > 1:
        raise AssemblyError("ambiguous %s zips in %s: %s"
                            % (platform, dist, ", ".join(found)))
    if pattern.match(found[0]).group(1) != version:
        raise AssemblyError("%s is not version %s (wanted %s-%s.%s.zip)"
                            % (found[0], version, ADDON, version, platform))
    return os.path.join(dist, found[0])


def load_entries(path):
    """Every file entry of PATH as name -> bytes, or a refusal."""
    entries = {}
    try:
        with zipfile.ZipFile(path) as archive:
            infos = archive.infolist()
            names = [info.filename for info in infos]
            duplicates = sorted({name for name in names if names.count(name) > 1})
            if duplicates:
                raise AssemblyError("duplicate entries in %s: %s"
                                    % (path, ", ".join(duplicates)))
            for info in infos:
                name = info.filename
                if info.is_dir():
                    continue
                if name.startswith("/") or ".." in name.split("/"):
                    raise AssemblyError("unsafe entry name in %s: %s" % (path, name))
                if not name.startswith(ADDON + "/"):
                    raise AssemblyError("entry outside %s/ in %s: %s"
                                        % (ADDON, path, name))
                if PLATFORM_ZIP_RE.match(os.path.basename(name)):
                    raise AssemblyError("nested platform zip in %s: %s" % (path, name))
                entries[name] = archive.read(info)
    except (zipfile.BadZipFile, zlib.error, EOFError, ValueError) as error:
        raise AssemblyError("unreadable zip %s: %s" % (path, error))
    except OSError as error:
        raise AssemblyError("cannot open %s: %s" % (path, error))
    if not entries:
        raise AssemblyError("no file entries in %s" % path)
    return entries


def split_entries(platform, path, entries):
    """Partition ENTRIES into (own platform tree, shared payload)."""
    own, shared = {}, {}
    for name, payload in entries.items():
        if name.startswith(BIN_PREFIX):
            owner = name[len(BIN_PREFIX):].split("/")[0]
            if owner != platform:
                raise AssemblyError("%s carries a foreign %s tree: %s"
                                    % (os.path.basename(path), owner, name))
            own[name] = payload
        else:
            shared[name] = payload
    if not own:
        raise AssemblyError("%s has no %s%s/ tree"
                            % (os.path.basename(path), BIN_PREFIX, platform))
    return own, shared


def compare_shared(reference_platform, reference, platform, shared, path):
    missing = sorted(set(reference) - set(shared))
    if missing:
        raise AssemblyError(
            "%s is missing shared entries present in the %s zip: %s"
            % (os.path.basename(path), reference_platform, ", ".join(missing))
        )
    extra = sorted(set(shared) - set(reference))
    if extra:
        raise AssemblyError(
            "%s adds shared entries absent from the %s zip: %s"
            % (os.path.basename(path), reference_platform, ", ".join(extra))
        )
    for name in sorted(reference):
        if reference[name] != shared[name]:
            raise AssemblyError(
                "shared entry %s diverges: %s zip sha256:%s, %s zip sha256:%s"
                % (name, reference_platform, digest(reference[name]),
                   platform, digest(shared[name]))
            )


def merge(platforms, sources):
    """Merge the per-platform payloads into one entry map."""
    merged = {}
    reference_platform, reference = None, None
    for platform in platforms:
        path = sources[platform]
        own, shared = split_entries(platform, path, load_entries(path))
        if reference is None:
            reference_platform, reference = platform, shared
        else:
            compare_shared(reference_platform, reference, platform, shared, path)
        merged.update(own)
    merged.update(reference)
    return merged


def is_executable(name):
    return (name.startswith(BIN_PREFIX)
            and os.path.basename(name) in EXECUTABLE_NAMES)


def write_universal(path, entries):
    """Write ENTRIES to PATH deterministically, atomically."""
    temporary = path + ".tmp"
    try:
        with zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED) as archive:
            for name in sorted(entries):
                info = zipfile.ZipInfo(name, ZIP_TIMESTAMP)
                info.compress_type = zipfile.ZIP_DEFLATED
                info.create_system = 3  # unix, so external_attr carries the mode
                info.external_attr = (0o755 if is_executable(name) else 0o644) << 16
                archive.writestr(info, entries[name], compresslevel=COMPRESS_LEVEL)
        os.replace(temporary, path)
    except BaseException:
        if os.path.exists(temporary):
            os.remove(temporary)
        raise


def assemble(repo, dist, version, output, platforms_override=None):
    platforms = read_platforms(os.path.join(repo, "build.sh"))
    if platforms_override:
        unknown = sorted(set(platforms_override) - set(platforms))
        if unknown:
            raise AssemblyError("unknown platforms: %s (build.sh ships: %s)"
                                % (", ".join(unknown), ", ".join(platforms)))
        platforms = [p for p in platforms if p in platforms_override]
    sources = {platform: discover_platform_zip(dist, platform, version)
               for platform in platforms}
    entries = merge(platforms, sources)
    if ADDON_XML not in entries:
        raise AssemblyError("merged payload has no %s" % ADDON_XML)
    merged_version = parse_addon_version(
        entries[ADDON_XML].decode("utf-8", "replace"), "the merged " + ADDON_XML
    )
    if merged_version != version:
        raise AssemblyError("%s says version %s, expected %s"
                            % (ADDON_XML, merged_version, version))
    try:
        os.makedirs(os.path.dirname(output) or ".", exist_ok=True)
        write_universal(output, entries)
    except OSError as error:
        raise AssemblyError("cannot write %s: %s" % (output, error))
    return platforms, entries


def main(argv=None):
    here = os.path.dirname(os.path.abspath(__file__))
    parser = argparse.ArgumentParser(
        prog="make_universal.py",
        description="Merge the per-platform Advanced Proxy ZIPs into one "
                    "deterministic universal ZIP.",
    )
    parser.add_argument("--repo", default=os.path.dirname(here),
                        help="repo root holding build.sh and the addon source")
    parser.add_argument("--dist", help="directory holding the platform ZIPs "
                                       "(default: <repo>/dist)")
    parser.add_argument("--version", help="addon version X.Y.Z "
                                          "(default: from addon.xml)")
    parser.add_argument("--output", help="output path (default: "
                                         "<dist>/%s-<version>.zip)" % ADDON)
    parser.add_argument("--platforms", help="comma-separated subset of "
                        "build.sh platforms to merge (default: all)")
    args = parser.parse_args(argv)

    repo = os.path.abspath(args.repo)
    if not os.path.isdir(repo):
        parser.error("repo dir not found: %s" % repo)
    dist = os.path.abspath(args.dist) if args.dist else os.path.join(repo, "dist")
    if not os.path.isdir(dist):
        parser.error("dist dir not found: %s" % dist)
    if args.version is not None and not VERSION_RE.match(args.version):
        parser.error("version must be X.Y.Z: %s" % args.version)
    platforms_override = None
    if args.platforms:
        platforms_override = [p for p in args.platforms.split(",") if p]
        for platform in platforms_override:
            if not PLATFORM_RE.match(platform):
                parser.error("invalid platform in --platforms: %r" % platform)

    try:
        if args.version:
            version = args.version
        else:
            addon_xml = os.path.join(repo, ADDON_XML)
            version = parse_addon_version(read_text(addon_xml, addon_xml), addon_xml)
        output = args.output or os.path.join(dist, "%s-%s.zip" % (ADDON, version))
        platforms, entries = assemble(repo, dist, version, output,
                                      platforms_override)
    except AssemblyError as error:
        print("make_universal: %s" % error, file=sys.stderr)
        return 1

    with open(output, "rb") as stream:
        sha256 = hashlib.sha256(stream.read()).hexdigest()
    print("make_universal: OK %s (version=%s platforms=%d entries=%d sha256=%s)"
          % (output, version, len(platforms), len(entries), sha256))
    return 0


if __name__ == "__main__":
    sys.exit(main())
