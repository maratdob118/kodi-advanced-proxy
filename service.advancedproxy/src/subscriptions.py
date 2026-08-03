# -*- coding: utf-8 -*-
"""Subscription groups: fetch, decode and mirror-sync remote profile lists.

A subscription is a URL whose body is a list of profile links, either plain
text (one link per line) or base64 (standard or URL-safe, with or without
padding/newlines) decoding to such text. The store keeps the group metadata
in subscriptions.json; the profiles themselves live in ProfileStore with a
`subscription` field set to the group id. Kodi-free so it can be unit tested.
"""
import base64
import hashlib
import io
import json
import os
import time
import urllib.request

import parsers


MAX_BYTES = 1 << 20  # refuse subscription bodies larger than 1 MiB


def decode_subscription(body, max_bytes=MAX_BYTES):
    """Decode BODY into a list of profile link lines.

    Plain text wins when it contains at least one profile-scheme line;
    otherwise base64 (standard, then URL-safe, padding optional) is tried.
    Returns the list of link lines. Raises ValueError when the body is too
    large, or when it decoded as text/base64 but holds no profile links.
    """
    if body is None or len(body) > max_bytes:
        raise ValueError("subscription body missing or larger than %d bytes"
                         % max_bytes)
    text = _decode_text(body)
    if text is not None:
        return text
    decoded = _decode_base64(body)
    if decoded is not None:
        text = _decode_text(decoded)
        if text is not None:
            return text
    try:
        body.decode("utf-8")
        raise ValueError("subscription body contains no profile links")
    except UnicodeDecodeError:
        return []


def _decode_text(body):
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError:
        return None
    lines = _profile_lines(text)
    if lines:
        return lines
    return None


def _decode_base64(body):
    candidates = []
    for transform in (lambda b: b, lambda b: b.replace(b"-", b"+")
                      .replace(b"_", b"/")):
        candidate = transform(body)
        candidate = b"".join(candidate.split())
        pad = len(candidate) % 4
        if pad:
            candidate += b"=" * (4 - pad)
        candidates.append(candidate)
    for candidate in candidates:
        try:
            decoded = base64.b64decode(candidate, validate=True)
        except (ValueError, TypeError):
            continue
        return decoded
    return None


def _profile_lines(text):
    lines = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if parsers.parse_uri(line) is not None:
            lines.append(line)
    return lines


def fetch(url, timeout=10, max_bytes=MAX_BYTES):
    """Download URL with a total size cap. Returns bytes; raises on error."""
    request = urllib.request.Request(url, headers={"User-Agent": "advancedproxy"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        buf = io.BytesIO()
        while True:
            chunk = response.read(65536)
            if not chunk:
                break
            buf.write(chunk)
            if buf.tell() > max_bytes:
                raise ValueError("subscription body larger than %d bytes"
                                 % max_bytes)
        return buf.getvalue()


def _group_id(url):
    return "sub-" + hashlib.sha1(url.encode("utf-8")).hexdigest()[:8]


def parse_links(links, disabled_protocols=()):
    """Parse link lines into profiles with their original URI attached."""
    profiles, skipped = parsers.parse_lines(links, disabled_protocols)
    valid = [line for line in links
             if parsers.parse_uri(line, disabled_protocols) is not None]
    if len(profiles) != len(valid):
        raise ValueError("parse mismatch: %d profiles vs %d valid links"
                         % (len(profiles), len(valid)))
    for profile, line in zip(profiles, valid):
        profile["uri"] = line
    return profiles, skipped


class SubscriptionStore(object):
    """JSON list of subscription groups in subscriptions.json."""

    def __init__(self, path, now=None):
        self.path = path
        self.now = now or (lambda: int(time.time()))
        self.subscriptions = []
        self.load()

    def load(self):
        try:
            with open(self.path) as f:
                data = json.load(f)
            self.subscriptions = data.get("subscriptions", [])
        except (OSError, ValueError):
            self.subscriptions = []

    def save(self):
        parent = os.path.dirname(self.path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"subscriptions": self.subscriptions}, f,
                      indent=2, ensure_ascii=False)
        os.replace(tmp, self.path)

    # ----- queries ---------------------------------------------------
    def groups(self):
        return self.subscriptions

    def get(self, group_id):
        for group in self.subscriptions:
            if group["id"] == group_id:
                return group
        return None

    # ----- mutations -------------------------------------------------
    def add(self, url, fetcher=fetch, profile_store=None):
        """Fetch URL once, create a group and add its profiles.

        Returns (group, error). On failure no group is persisted and the
        profiles are untouched.
        """
        try:
            body = fetcher(url)
            links = decode_subscription(body)
        except Exception as e:
            return None, str(e)
        if not links:
            return None, "subscription contains no usable profiles"
        parsed, _ = parse_links(links)
        group = {"id": _group_id(url), "url": url,
                 "last_updated": self.now(), "last_error": None}
        if profile_store is not None:
            profile_store.add_subscription_profiles(parsed, group["id"])
        self.subscriptions = [g for g in self.subscriptions
                              if g["id"] != group["id"]]
        self.subscriptions.append(group)
        self.save()
        return group, None

    def remove(self, group_id, profile_store=None):
        """Delete the group and cascade-delete its profiles."""
        self.subscriptions = [g for g in self.subscriptions
                              if g["id"] != group_id]
        if profile_store is not None:
            profile_store.remove_by_subscription(group_id)
        self.save()

    def refresh(self, group_id, fetch=fetch, parse=parse_links,
                profile_store=None):
        """Mirror-sync one group against its URL.

        Returns (added, removed, error). On fetch/decode failure the profiles
        are left untouched and last_error is recorded.
        """
        group = self.get(group_id)
        if group is None:
            return [], [], "no such subscription"
        try:
            body = fetch(group["url"])
            links = decode_subscription(body)
        except Exception as e:
            group["last_error"] = str(e)
            self.save()
            return [], [], str(e)
        parsed, _ = parse(links)
        added, removed = [], []
        if profile_store is not None:
            if hasattr(profile_store, "sync_subscription"):
                added, removed = profile_store.sync_subscription(
                    parsed, group_id)
            else:
                removed = profile_store.remove_by_subscription(group_id)
                profile_store.add_subscription_profiles(parsed, group_id)
                added = [p["tag"] for p in parsed]
        group["last_updated"] = self.now()
        group["last_error"] = None
        self.save()
        return added, removed, None

    def due(self, now, interval_hours):
        """Groups that are due for refresh given the shared INTERVAL_HOURS."""
        if not interval_hours:
            return []
        due = []
        for group in self.subscriptions:
            last = group.get("last_updated") or 0
            if last + interval_hours * 3600 < now:
                due.append(group)
        return due
