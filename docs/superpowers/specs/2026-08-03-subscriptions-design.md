# Advanced Proxy: Subscription Groups

Date: 2026-08-03
Status: Approved (design decisions confirmed with the user)

## Scope

This design adds subscription groups to the Advanced Proxy Kodi addon: a
subscription is a remote text file (plain or base64) containing many profile
links, treated as one refreshable group. Decisions below are confirmed. This
document specifies them; it does not implement them.

## Confirmed Decisions

1. **Detection on paste**: any pasted link is first tried as a single profile
   (`parsers.parse_uri`); if it matches no profile scheme and is an
   `http(s)://` URL, it is treated as a subscription URL.
2. **Subscription format**: the fetched body is either plain text with one
   profile link per line, or base64 (standard or URL-safe, with or without
   newlines) decoding to such text. Unknown content is a per-subscription
   error and never touches existing profiles.
3. **Group model**: subscriptions live in `subscriptions.json`; each profile
   in `profiles.json` gains a `subscription` field holding its group id
   (`None` for manually added profiles).
4. **Mirror sync**: refreshing a group makes profiles exactly match the
   fetched content. New links are added, links that disappeared are removed
   (even if disabled), the active profile is kept when still present.
5. **Cascade delete**: removing a group removes every profile carrying its
   id; if the removed set held the active profile, another enabled profile
   becomes active (existing `ProfileStore.remove` semantics).
6. **Shared refresh interval**: ONE setting controls refresh for all groups —
   "never" or "every N hours". A group is due when
   `last_updated + N*3600 < now`.
7. **Protocol toggles**: new boolean settings disable whole protocols
   (vless, trojan, hysteria2, ...). Disabled-protocol links are skipped at
   parse time, both for manual pastes and for subscription refreshes.
8. **Availability check at activation**: before a profile is activated, a
   TCP probe to `server:port` runs; unreachable profiles are skipped. Manual
   activation warns and offers the next reachable enabled profile.
9. **Copy link**: every profile keeps a "Copy link" action. QR codes are
   explicitly out of scope.
10. **UI**: a new "Subscriptions" category inside the existing settings
    dialog (add URL, per-group refresh/remove, shared interval, protocol
    toggles). The main addon menu keeps profile selection unchanged.

## Architecture

### New module: `src/subscriptions.py` (Kodi-free)

```
class SubscriptionStore(path)          # subscriptions.json
    load() / save()
    groups()                           # list of group dicts
    get(group_id)
    add(url) -> (group, error)         # fetch once, parse, persist
    remove(group_id, profile_store)    # cascade delete via ProfileStore
    refresh(group_id, fetch, parse) -> (added, removed, error)
    due(now, interval_hours) -> [groups]
```

Group dict:
```json
{ "id": "sub-<short-hash>",
  "url": "https://...",
  "last_updated": 0,          # epoch seconds; 0 = never fetched
  "last_error": null }
```

### Fetch and parse (Kodi-free, injectable)

`fetch(url, timeout=10, max_bytes=1<<20)` -> body bytes.
`decode_subscription(body)` -> list of link lines:
1. try UTF-8 text: if it contains at least one profile-scheme line, use it.
2. else try base64 (standard, then URL-safe, whitespace stripped): decode,
   repeat step 1.
3. else raise/return error.

Protocol filter applies after decoding: lines whose protocol is disabled are
dropped (counted as skipped, not errors).

### `profiles.py` changes

- `add_uri(uri, subscription=None)` stores the group id on the profile.
- `add_subscription_profiles(profiles, group_id)` appends with de-dup by
  `uri` (a manual profile with the same URI wins; the subscription copy is
  skipped).
- `remove_by_subscription(group_id)` removes matching profiles and re-picks
  the active profile (existing active-selection logic).
- `copy_link(tag)` returns the stored `uri` (already present).

### `parsers.py` changes

- `parse_uri(line, disabled_protocols=())` returns None early for disabled
  protocols (or a distinct skip reason).
- `is_subscription_url(line)` -> True for `http(s)://` that did not parse as
  a profile.
- `parse_lines` gains a disabled-protocol filter already covered by
  `parse_uri`.

### `supervisor.py` changes

- `tick()` checks the shared interval: when any group is due, trigger a
  refresh (serialized; a refresh in flight is not re-entered). On success
  the supervisor re-resolves the active profile and, if the engine config
  references a removed profile, reconfigures.
- Activation path (`main.py`/`default.py`) gains the TCP availability check
  (reusing `helpers.measure_latencies` prober semantics per profile).

### `helpers.py` changes

- New settings: `subscription_interval_hours` (0 = never, default 0) and
  per-protocol toggles, e.g. `disable_proto_vless/trojan/hysteria2`.
- `disabled_protocols()` derives the tuple from the toggles.
- `copy_to_clipboard(text)` via Kodi (xbmc's clipboard API) or a fallback
  text-view dialog.

### UI: `settings.xml` + `default.py`

New settings category "subscriptions" (label ids added to `strings.po`):
- `subscription_url` (string) + "Add subscription" action button
  (`RunPlugin(service.advancedproxy?action=sub_add)`).
- `subscription_interval_hours` (integer, 0 = never).
- `disable_proto_vless`, `disable_proto_trojan`,
  `disable_proto_hysteria2` (boolean toggles).
- "Open subscriptions" action opening the addon listing filtered to
  subscription groups.

`default.py` new actions: `sub_add`, `sub_refresh&id=`, `sub_remove&id=`,
`copy&tag=`; the listing shows group rows (url, status, refresh/remove via
context menu) and profile rows keep click-to-activate + copy-link context
action.

### Files touched

- `service.advancedproxy/src/subscriptions.py` (new)
- `service.advancedproxy/src/parsers.py`
- `service.advancedproxy/src/profiles.py`
- `service.advancedproxy/src/supervisor.py`
- `service.advancedproxy/src/helpers.py`
- `service.advancedproxy/main.py`
- `service.advancedproxy/default.py`
- `service.advancedproxy/resources/settings.xml`
- `service.advancedproxy/resources/language/resource.language.en_gb/strings.po`
- `tests/test_core.py` (primary), existing repository/packaging tests untouched

## Non-Goals

- QR codes.
- Clash/Surge JSON subscription formats (plain/base64 line lists only).
- Per-group refresh intervals.
- Automatic YouTube restart or other addon integration changes.
- Any change to engine binaries or publication pipeline.

## Testing Strategy

- `subscriptions.py`: decode plain/base64/URL-safe-base64, invalid content,
  size cap, fetch failure, mirror sync add/remove/keep-active, cascade
  delete, due computation, shared interval.
- `parsers.py`: disabled-protocol skip for each protocol; subscription-URL
  detection; manual paste still works.
- `profiles.py`: subscription field persistence, de-dup, cascade active
  re-pick.
- `supervisor.py`: refresh-on-tick triggers once per due window, no
  re-entrancy, reconfigures after profile set change.
- `helpers.py`/`default.py` wiring via existing fakes in `tests/test_core.py`.
- Live QA on 192.168.31.174: add a real subscription URL, refresh, delete
  group, confirm profiles appear/disappear and activation still works.
