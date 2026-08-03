# -*- coding: utf-8 -*-
"""Profile storage: a JSON list of proxy profiles added via links.

Stored in the addon profile dir as profiles.json. Kodi-free so it can be unit
tested; the UI layer (default.py) and the service (main.py) both use it.
"""
import json
import os

import parsers


class ProfileStore(object):
    def __init__(self, path):
        self.path = path
        self.profiles = []
        self.active_tag = None
        self.load()

    def load(self):
        try:
            with open(self.path) as f:
                data = json.load(f)
            self.profiles = data.get("profiles", [])
            self.active_tag = data.get("active_tag")
        except (OSError, ValueError):
            self.profiles = []
            self.active_tag = None

    def save(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"profiles": self.profiles, "active_tag": self.active_tag},
                      f, indent=2, ensure_ascii=False)
        os.replace(tmp, self.path)

    # ----- queries ---------------------------------------------------
    def enabled(self):
        return [p for p in self.profiles if p.get("enabled", True)]

    def tags(self):
        return [p["tag"] for p in self.profiles]

    def get(self, tag):
        for p in self.profiles:
            if p["tag"] == tag:
                return p
        return None

    def active(self):
        if self.active_tag:
            p = self.get(self.active_tag)
            if p and p.get("enabled", True):
                return p
        en = self.enabled()
        return en[0] if en else None

    # ----- mutations -------------------------------------------------
    def add_uri(self, uri, subscription=None):
        """Parse and append a profile link. Returns (profile, error)."""
        p = parsers.parse_uri(uri.strip())
        if p is None:
            return None, "unsupported or invalid link"
        if p["tag"] in self.tags():
            # de-dup by tag: keep existing, report
            return self.get(p["tag"]), None
        p["enabled"] = True
        p["uri"] = uri.strip()
        if subscription is not None:
            p["subscription"] = subscription
        self.profiles.append(p)
        if not self.active_tag:
            self.active_tag = p["tag"]
        self.save()
        return p, None

    def add_subscription_profiles(self, parsed, group_id):
        """Append PARSED profiles tagged with GROUP_ID, de-duping by URI.

        A manual profile (no subscription) with the same URI wins: its copy
        from the subscription is skipped. Profiles without a URI are always
        added. Returns the number added.
        """
        existing = {p.get("uri") for p in self.profiles
                    if p.get("uri") is not None
                    and p.get("subscription") is None}
        added = 0
        for p in parsed:
            uri = p.get("uri")
            if uri is not None and uri in existing:
                continue
            p["enabled"] = True
            p["subscription"] = group_id
            self.profiles.append(p)
            if uri is not None:
                existing.add(uri)
            added += 1
        if added and not self.active_tag:
            self.active_tag = self.profiles[0]["tag"]
        self.save()
        return added

    def remove_by_subscription(self, group_id):
        """Remove every profile carrying GROUP_ID and re-pick the active."""
        removed = [p["tag"] for p in self.profiles
                   if p.get("subscription") == group_id]
        self.profiles = [p for p in self.profiles
                         if p.get("subscription") != group_id]
        if self.active_tag in removed:
            en = self.enabled()
            self.active_tag = en[0]["tag"] if en else None
        self.save()
        return removed

    def remove(self, tag):
        self.profiles = [p for p in self.profiles if p["tag"] != tag]
        if self.active_tag == tag:
            en = self.enabled()
            self.active_tag = en[0]["tag"] if en else None
        self.save()

    def toggle(self, tag):
        p = self.get(tag)
        if p:
            p["enabled"] = not p.get("enabled", True)
            if not p["enabled"] and self.active_tag == tag:
                en = self.enabled()
                self.active_tag = en[0]["tag"] if en else None
            self.save()

    def set_active(self, tag):
        p = self.get(tag)
        if p and p.get("enabled", True):
            self.active_tag = tag
            self.save()
            return True
        return False
