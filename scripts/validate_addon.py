#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Validate Advanced Proxy addon metadata and release-readiness.

Usage:
    python3 scripts/validate_addon.py [repo-root]

Exit code 0 when every check passes, 1 otherwise. Checks:
  * addon.xml / settings.xml are well-formed XML
  * addon root carries id, name, provider-name and an X.Y.Z version
  * the required extension points are present (xbmc.service,
    xbmc.python.pluginsource, xbmc.python.module, xbmc.addon.metadata)
  * xbmc.addon.metadata declares the GPL-3.0-or-later license
  * the required license / third-party notice files exist and are non-empty
  * every numeric setting id referenced by settings.xml (label/help/heading)
    has a matching msgctxt entry in strings.po

Importable: validate_addon.validate_addon(repo_root) -> list[str] problems.
"""
import os
import re
import sys
import xml.etree.ElementTree as ET

EXPECTED_LICENSE = "GPL-3.0-or-later"
VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")

REQUIRED_EXTENSIONS = (
    "xbmc.service",
    "xbmc.python.pluginsource",
    "xbmc.python.module",
    "xbmc.addon.metadata",
)

REQUIRED_LICENSE_FILES = (
    "LICENSE",
    "THIRD_PARTY_NOTICES.md",
    "service.advancedproxy/resources/licenses/sing-box/LICENSE",
    "service.advancedproxy/resources/licenses/sing-box/NOTICE",
    "service.advancedproxy/resources/licenses/xray/LICENSE",
)

ADDON_XML_REL = os.path.join("service.advancedproxy", "addon.xml")
SETTINGS_XML_REL = os.path.join("service.advancedproxy", "resources", "settings.xml")
STRINGS_PO_REL = os.path.join(
    "service.advancedproxy", "resources", "language",
    "resource.language.en_gb", "strings.po")


def _referenced_setting_ids(settings_path):
    """Return the set of numeric string ids referenced by settings.xml."""
    ids = set()
    tree = ET.parse(settings_path)
    for el in tree.iter():
        for attr in ("label", "help"):
            value = el.get(attr)
            if value and value.isdigit():
                ids.add(value)
        if el.tag == "heading" and el.text and el.text.strip().isdigit():
            ids.add(el.text.strip())
    return ids


def _translated_ids(strings_path):
    """Return the set of msgctxt ids (#NNNNN) present in strings.po."""
    ids = set()
    with open(strings_path, encoding="utf-8") as fh:
        text = fh.read()
    for match in re.finditer(r'^msgctxt\s+"#(\d+)"', text, re.MULTILINE):
        ids.add(match.group(1))
    return ids


def validate_addon(repo_root):
    """Return a list of human-readable problems; empty list means valid."""
    problems = []
    addon_path = os.path.join(repo_root, ADDON_XML_REL)

    # --- addon.xml --------------------------------------------------------
    if not os.path.isfile(addon_path):
        problems.append(f"missing addon.xml: {ADDON_XML_REL}")
        xml_root = None
    else:
        try:
            xml_root = ET.parse(addon_path).getroot()
        except ET.ParseError as exc:
            problems.append(f"addon.xml is not well-formed XML: {exc}")
            xml_root = None

    if xml_root is not None:
        version = (xml_root.get("version") or "").strip()
        if not VERSION_RE.match(version):
            problems.append(f"addon version {version!r} is not X.Y.Z")
        for attr in ("id", "name", "provider-name"):
            if not (xml_root.get(attr) or "").strip():
                problems.append(f"addon root missing {attr!r} attribute")
        points = {e.get("point") for e in xml_root.iter("extension")}
        for point in REQUIRED_EXTENSIONS:
            if point not in points:
                problems.append(f"missing extension point {point!r}")
        metadata = next((e for e in xml_root.iter("extension")
                         if e.get("point") == "xbmc.addon.metadata"), None)
        if metadata is None:
            problems.append("missing xbmc.addon.metadata extension")
        else:
            license_el = metadata.find("license")
            license = (license_el.text or "").strip() if license_el is not None else ""
            if license.lower() != EXPECTED_LICENSE.lower():
                problems.append(
                    f"addon license is {license!r}, expected {EXPECTED_LICENSE}")

    # --- settings.xml + localized setting ids -----------------------------
    settings_path = os.path.join(repo_root, SETTINGS_XML_REL)
    strings_path = os.path.join(repo_root, STRINGS_PO_REL)

    if not os.path.isfile(settings_path):
        problems.append(f"missing settings.xml: {SETTINGS_XML_REL}")
        referenced = set()
    else:
        try:
            referenced = _referenced_setting_ids(settings_path)
        except ET.ParseError as exc:
            problems.append(f"settings.xml is not well-formed XML: {exc}")
            referenced = set()

    if not os.path.isfile(strings_path):
        problems.append(f"missing strings.po: {STRINGS_PO_REL}")
        translated = set()
    else:
        translated = _translated_ids(strings_path)

    for sid in sorted(referenced - translated):
        problems.append(
            f"setting id #{sid} referenced by settings.xml has no "
            f"msgctxt in strings.po")

    # --- license / notice files -------------------------------------------
    for rel in REQUIRED_LICENSE_FILES:
        path = os.path.join(repo_root, rel)
        if not os.path.isfile(path):
            problems.append(f"missing required license/notice file: {rel}")
        elif os.path.getsize(path) == 0:
            problems.append(f"required license/notice file is empty: {rel}")

    return problems


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    repo_root = args[0] if args else os.path.normpath(
        os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
    problems = validate_addon(repo_root)
    for problem in problems:
        print(f"FAIL: {problem}")
    if problems:
        print(f"validate_addon: {len(problems)} problem(s) found")
        return 1
    print("validate_addon: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
