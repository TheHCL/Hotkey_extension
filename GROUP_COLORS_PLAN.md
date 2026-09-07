# Per-Group Color Customization — Implementation Plan

## Context

Today every group label rendered in the Chrome extension popup is colored from a fixed 8-entry palette (`GROUP_PALETTE` in `chrome_extension/popup.js:18-30`), picked by hashing the group name (`groupColorIndex`, lines 32-38). Users cannot pick their own colors; the only mutable state is the entry list in `index.json`.

This plan adds the ability to assign any RGB hex per group via the PWmgr Tk GUI, persist it inside `index.json`, propagate it through the existing native-host protocol, and have the popup honor it (falling back to today's hash palette for unset groups and to `NEUTRAL_COLOR` for the "未分類" bucket).

Constraints recap:
- Storage writes go through `index_lock` — no lockless files for this setting (GUI and Chrome can both write).
- Extra top-level keys on existing handler responses stay forward-compatible silent.
- Tk dialog reuses existing `Card.TFrame` / ttk styling.
- Trigger lives in the menu bar (existing pattern: top-level menu in `_build_menu`).
- Group names already capped at `MAX_GROUP_CHARS = 64` (`pwmgr/config.py:59`).

---

## Schema

Add one top-level key to `index.json`:

```json
{
  "version": 1,
  "entries": [ ... ],
  "group_colors": {
    "工作": "#3b82f6",
    "個人": "#ef4444"
  }
}
```

- Keys = exact group strings as in `PasswordEntry.group` (no normalization).
- Values = `#rrggbb` (7 chars). Case-insensitive on read; lowercase on write.
- The "未分類" bucket (`__none__` in `popup.js:16`) is **never** stored — popup maps `__none__` → `NEUTRAL_COLOR` at render time.
- Missing `group_colors` in legacy files → treat as `{}`. `_read_index_unlocked` already tolerates extra/missing top-level keys.

---

## Files to modify

### `pwmgr/storage.py`
- New helpers:
  - `_validate_hex_color(value) -> str` (lowercases on return; raises `BadRequestError`).
  - `load_group_colors() -> dict[str, str]` — reads `data.setdefault("group_colors", {})` inside `index_lock`. Returns `{}` if key absent.
  - `set_group_color(group, color)` — single-set path: under `index_lock`, read index.json, set/remove the key in `data["group_colors"]`, `_atomic_write_json`. `color is None` removes the entry.
- Reuses existing `_atomic_write_json`, `index_lock`, `_read_index_unlocked`.

### `pwmgr/native_host.py`
- Add two handlers; register in `_DISPATCH` (`pwmgr/native_host.py:196-204`):
  - `_handle_get_group_colors` — returns `{ok: True, group_colors: storage.load_group_colors()}`.
  - `_handle_set_group_color` — `req = {type, group, color}`. Validates `group` via existing `_normalize_group`; validates `color` via `_validate_hex_color` (None → remove; else require `#rrggbb`).
- Modify existing responses to carry the override map alongside (forward-compat silent):
  - `_handle_query` (lines 97-115): add `"group_colors": storage.load_group_colors()`.
  - `_handle_list` (lines 130-134): same.

### `pwmgr/app.py`
- New top-level menu `群組` in `_build_menu` (lines 210-228), inserted between `檔案` and `說明`:
  - `編輯群組顏色…` → `self._open_group_colors_dialog`.
- New method `_open_group_colors_dialog(self)` builds a modal `Toplevel` (`transient(self.root)`, `grab_set()`) with `Card.TFrame` style. Body:
  - Header + description (`CardMuted.TLabel`).
  - Scrollable list of rows — one per unique group, same first-seen order as `_refresh_listbox` (lines 446-451). No group rows if `_entries` empty (show "目前沒有任何群組" placeholder).
  - Per-row widgets: group name label (left), 32×18 color `Canvas` swatch (current or `#ffffff`), hex label (`#rrggbb` or `自動`), `TButton("選色…")`, `Danger.TButton("重設")`.
- On pick: `colorchooser.askcolor(parent=dialog, initialcolor=current or "#888888", title=f"選擇「{group}」的顏色")`. On cancel (`hex_str is None`) no-op; else `storage.set_group_color(group, hex_str.lower())`, update swatch+label + status bar.
- On reset: `storage.set_group_color(group, None)`, revert swatch to neutral.
- No batch commit — per-edit write matches `_save_entry`'s pattern.

### `chrome_extension/popup.js`
- New helper `resolveGroupColor(key, groupColorsMap) -> {bg, accent}`:
  - `key === NO_GROUP` → `NEUTRAL_COLOR`.
  - `key in groupColorsMap` → derive `{bg, accent}` from the stored hex.
  - else `GROUP_PALETTE[groupColorIndex(key)]`.
- `renderGroupMenu(entries, keyword, groupColors)` — signature change; both call sites (lines 159 and 169) thread the new map.
- `getAllEntries` and `queryFresh` responses now carry `group_colors`; thread it into `renderLaunchList` → `renderGroupMenu`. Autofill rows don't currently render group metadata, so `renderAutofillList` is unchanged for v1.

### `chrome_extension/popup.css`
- No change needed. `--group-bg` and `--group-accent` CSS variables already defined at lines 96 and 122.

---

## Algorithms

### Bg/accent derivation (popup side, single stored hex → `{bg, accent}`)

```
hex "#rrggbb" → (r, g, b) ∈ 0..255
accent = hex                                            # used for .entry-list border-left: 3px
bg     = component-wise mix(hex, "#ffffff", t = 0.85)   # used for .group-label background
```

Worst-case check: dark stored color `#1a1a1a` → bg `#d8d8d8`. Text color `#222` (`popup.css:98`) → contrast 9.8:1, well above WCAG AA. No additional clamping needed for v1.

### Hex validation (Python)

```python
_HEX_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
def _validate_hex_color(value):
    if not isinstance(value, str) or not _HEX_RE.match(value):
        raise BadRequestError("color 必須是 #rrggbb 形式")
    return value.lower()
```

Defense in depth: same regex in both `native_host._handle_set_group_color` and `storage.set_group_color`.

---

## Native protocol (final shapes)

### Additive response changes (silent forward-compat — older Chrome ignores the new key)

`_handle_query`:
```json
{
  "ok": true,
  "matches": [...],
  "group_colors": {"工作": "#3b82f6"}
}
```

`_handle_list`:
```json
{
  "ok": true,
  "entries": [...],
  "group_colors": {"工作": "#3b82f6"}
}
```

### New messages

`{"type": "get_group_colors"}` → `{"ok": true, "group_colors": {…}}`.

`{"type": "set_group_color", "group": "<name>", "color": "#rrggbb" | null}` → `{"ok": true}`. `color: null` removes the entry. Bad hex or empty group → `{"ok": false, "code": "BAD_REQUEST", ...}`.

---

## Tests

### `tests/test_storage.py`
- `test_group_colors_default_empty_when_missing`
- `test_group_colors_round_trip`
- `test_set_group_color_rejects_invalid_hex`
- `test_set_group_color_replaces_not_merges`
- `test_set_group_color_none_clears_entry`
- `test_legacy_index_loads_without_group_colors`

Use existing `tmp_index` + `null_locks` fixtures (`tests/test_storage.py:53-68`). Patch `storage.index_lock`, not `ipc.index_lock`.

### `tests/test_native_host.py`
- `test_query_response_carries_group_colors`
- `test_set_group_color_persists_then_query_carries`
- `test_set_group_color_rejects_bad_hex`
- `test_set_group_color_null_clears`
- `test_get_group_colors_empty_when_unset`
- `test_set_group_color_oversized_group_rejected`

### `tests/test_app_smoke.py`
- `test_open_group_colors_dialog_constructs`
- `test_dialog_lists_groups_in_first_seen_order_deduped`
- `test_dialog_pick_persists_via_storage` (monkeypatch `colorchooser.askcolor`)
- `test_dialog_pick_cancel_is_noop`
- `test_dialog_reset_clears_override`
- `test_dialog_empty_vault_shows_placeholder`

### `tests/test_extension_manifest.py`
- `test_popup_js_uses_group_colors_override` — asserts `popup.js` mentions `group_colors`, `resolveGroupColor`, and still has `GROUP_PALETTE` + `NEUTRAL_COLOR`.

---

## Critical files (final list)

- `pwmgr/storage.py` — new functions + `_validate_hex_color`.
- `pwmgr/native_host.py` — 2 new handlers + 2 response-shape tweaks.
- `pwmgr/app.py` — new menu + `GroupColorsDialog` class + colorchooser invocation.
- `chrome_extension/popup.js` — `resolveGroupColor` helper + threading `group_colors` into `renderGroupMenu`.
- `tests/test_storage.py`, `tests/test_native_host.py`, `tests/test_app_smoke.py`, `tests/test_extension_manifest.py`.

---

## Verification

### Manual end-to-end

1. `python install.py` (no-op vs current install; confirms no install-script changes required).
2. Launch `python -m pwmgr`. Add entries in at least two groups ("工作", "個人").
3. Menu → `群組` → `編輯群組顏色…`. Both groups listed with neutral swatches.
4. Click `選色…` for "工作", pick `#1d4ed8`, confirm. Status bar: `已更新「工作」的顏色:#1d4ed8`.
5. Open `%LOCALAPPDATA%\pwmgr\index.json` — `group_colors` key present, JSON valid.
6. Click `重設` on "工作". `index.json` no longer carries the key.
7. Restart GUI; reopen the dialog; previously-picked colors persist (read from disk).
8. In Chrome/Edge with the extension loaded, navigate somewhere with no autofill match. Open PWmgr popup:
   - "工作" group label = bluish pastel bg (`#1d4ed8` → `#d8dfea`) with darker border-left accent.
   - "個人" group (untouched) renders with the hash palette default.
   - "未分類" (if any) renders with neutral gray.
9. Forward-compat: temporarily revert the popup build. Open the popup — it should still work using the hash palette; the `group_colors` key in `query`/`list` responses is silently ignored.

### pytest

```bash
pytest tests/test_storage.py -k group_colors -v
pytest tests/test_native_host.py -k "group_color" -v
pytest tests/test_app_smoke.py -k "dialog or group_color" -v
pytest tests/test_extension_manifest.py -v
pytest -q   # full suite, no regressions
```

Run on Windows; existing `isolated_paths` / `null_locks` fixtures handle per-test temp dirs and lock stubbing.
