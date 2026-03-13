# Emdash Integration Plan

## Goal

Let ccbot discover and interact with emdash-managed tmux sessions, so users can control emdash agents from Telegram. Low-touch, loosely-coupled — leverages existing mechanisms, zero emdash modifications needed.

## How It Works (End-to-End)

```
Emdash (tmux: true)                    ccbot
─────────────────                      ─────
1. Creates tmux session
   emdash-claude-main-abc123
   └─ runs: claude --session-id uuid

2. Claude fires SessionStart hook ───► ccbot hook receives event
                                       writes session_map.json:
                                         "emdash-claude-main-abc123:@0" → {session_id, cwd, ...}
                                       writes events.jsonl

3.                                     SessionMonitor reads session_map
                                       Sees emdash-prefixed entry (NEW)
                                       Creates WindowState(external=true)
                                       Starts reading transcript

4.                                     User opens Telegram topic
                                       Window picker shows emdash sessions (NEW)
                                       User picks "emdash: project (claude)"

5. User sends "hello" in topic ──────► TmuxManager.send_keys(
                                         "emdash-claude-main-abc123:@0", "hello")
                                       Resolves foreign target (NEW)
                                       tmux send-keys -t emdash-claude-main-abc123:@0

6. Claude responds                     SessionMonitor reads new transcript content
                                       Routes to bound topic → sends to Telegram

7. User closes topic                   Unbinds thread, cleans up state
                                       Does NOT kill emdash session (NEW guard)
```

### Prerequisites

- User enables `"tmux": true` in emdash's `.emdash.json`
- ccbot's global hook is installed (`ccbot hook --install`)
- Both tools share the same tmux server (same machine, same user)

### What Already Works (Zero Changes)

1. **Hook fires** — ccbot's hook in `~/.claude/settings.json` is global; emdash's hook is per-project in `.claude/settings.local.json`. Claude Code merges both. Both fire independently.
2. **session_map.json gets populated** — hook resolves tmux pane → `emdash-claude-main-abc123:@0` key
3. **events.jsonl gets populated** — all 7 event types written for emdash sessions
4. **Transcript monitoring** — once session_map entry is accepted, monitor reads JSONL as usual
5. **Message delivery** — routed by window_id → thread_id binding (existing mechanism)

---

## Implementation

### Change 1: WindowState.external flag

**File:** `src/ccbot/session.py` (WindowState dataclass, ~5 lines)

Add `external: bool = False` field to mark windows owned by external tools.

```python
@dataclass
class WindowState:
    session_id: str = ""
    cwd: str = ""
    window_name: str = ""
    transcript_path: str = ""
    notification_mode: str = "all"
    provider_name: str = ""
    approval_mode: str = DEFAULT_APPROVAL_MODE
    external: bool = False  # NEW: owned by emdash or other tool
```

Update `to_dict()` / `from_dict()` to persist the flag.

---

### Change 2: Accept emdash entries in session_map parsing

**File:** `src/ccbot/session.py` (load_session_map, ~20 lines changed)

Currently `load_session_map()` filters by `prefix = f"{config.tmux_session_name}:"`. Emdash entries have prefix `emdash-*:`.

**Approach:** After processing native entries, make a second pass for `emdash-` prefixed entries. Use a helper to extract provider from the emdash session name.

```python
# In load_session_map(), after the native prefix loop:
for key, info in session_map.items():
    if not key.startswith("emdash-"):
        continue
    if not isinstance(info, dict):
        continue
    # key = "emdash-claude-main-abc123:@0"
    # Use the FULL key as window_id for foreign windows
    window_id = key  # qualified ID, not stripped
    if not info.get("session_id"):
        continue
    valid_wids.add(window_id)
    state = self.get_window_state(window_id)
    state.external = True
    # ... same field sync as native entries (session_id, cwd, transcript, provider)
```

**Key design decision:** For foreign windows, the `window_id` in ccbot's state is the full session_map key (e.g., `emdash-claude-main-abc123:@0`). This is a string — all existing dicts, bindings, and lookups use strings. No structural change needed.

**Provider detection from session name:**

```python
def _parse_emdash_provider(session_name: str) -> str:
    """Extract provider from emdash session name.

    Format: emdash-{provider}-main-{id} or emdash-{provider}-chat-{id}
    """
    for sep in ("-main-", "-chat-"):
        if sep in session_name:
            prefix = session_name.split(sep)[0]  # "emdash-claude"
            return prefix.removeprefix("emdash-")  # "claude"
    return ""
```

---

### Change 3: TmuxManager cross-session support

**File:** `src/ccbot/tmux_manager.py` (~40 lines added/modified)

Add a resolution layer that routes foreign window IDs to the correct tmux session.

**New helper:**

```python
def _is_foreign_window(window_id: str) -> bool:
    """Check if window_id refers to a foreign tmux session."""
    return ":" in window_id and not window_id.startswith("@")

def _parse_foreign_target(window_id: str) -> tuple[str, str]:
    """Split qualified window_id into (session_name, window_id).

    "emdash-claude-main-abc123:@0" → ("emdash-claude-main-abc123", "@0")
    """
    session_name, _, wid = window_id.rpartition(":")
    return session_name, wid
```

**Modified methods** — each gets a foreign-window fast path at the top:

1. **`capture_pane(window_id)`** — uses subprocess path (`_capture_pane_ansi`) which already accepts raw `-t` targets. The `window_id` is passed directly as the tmux target. For foreign windows, the qualified ID like `emdash-claude-main-abc123:@0` is a valid tmux target string already!

   Actually — this is the key realization: **tmux CLI accepts `session:window` as the `-t` target natively.** So `tmux capture-pane -t emdash-claude-main-abc123:@0` just works.

   Methods using subprocess (`_capture_pane_ansi`, `capture_pane_raw`) already pass `window_id` directly to `-t`. They work for foreign windows with NO changes!

   Methods using libtmux (`_capture_pane_plain`, `_pane_send`) need the foreign-window path: use subprocess instead.

2. **`send_keys(window_id, text)`** — the `_send_literal_then_enter` path calls `_pane_send` which uses libtmux's `session.windows.get()`. For foreign windows, use subprocess:

   ```python
   def _pane_send(self, window_id, chars, *, enter, literal):
       if _is_foreign_window(window_id):
           return self._pane_send_subprocess(window_id, chars, enter=enter, literal=literal)
       # ... existing libtmux path

   def _pane_send_subprocess(self, target, chars, *, enter, literal):
       """Send keys via tmux subprocess (for foreign sessions)."""
       cmd = ["tmux", "send-keys", "-t", target]
       if literal:
           cmd.append("-l")
       cmd.append(chars)
       if enter:
           # Send Enter separately
           subprocess.run(cmd, timeout=5)
           subprocess.run(["tmux", "send-keys", "-t", target, "Enter"], timeout=5)
           return True
       subprocess.run(cmd, timeout=5)
       return True
   ```

3. **`_capture_pane_plain(window_id)`** — for foreign windows, delegate to `_capture_pane_ansi` (subprocess-based, already works):

   ```python
   async def _capture_pane_plain(self, window_id):
       if _is_foreign_window(window_id):
           return await self._capture_pane_ansi(window_id)
       # ... existing libtmux path
   ```

4. **`find_window_by_id(window_id)`** — for foreign windows, query the foreign session via subprocess:

   ```python
   async def find_window_by_id(self, window_id):
       if _is_foreign_window(window_id):
           return await self._find_foreign_window(window_id)
       # ... existing path

   async def _find_foreign_window(self, qualified_id):
       """Check if a foreign tmux window exists and return TmuxWindow."""
       session_name, wid = _parse_foreign_target(qualified_id)
       # tmux list-windows -t session_name -F "#{window_id} #{pane_current_path} #{pane_current_command}"
       proc = await asyncio.create_subprocess_exec(
           "tmux", "list-windows", "-t", session_name,
           "-F", "#{window_id}\t#{pane_current_path}\t#{pane_current_command}",
           stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
       )
       stdout, _ = await proc.communicate()
       if proc.returncode != 0:
           return None
       for line in stdout.decode().strip().split("\n"):
           parts = line.split("\t", 2)
           if len(parts) >= 1 and parts[0] == wid:
               return TmuxWindow(
                   window_id=qualified_id,  # use qualified ID
                   window_name=session_name.removeprefix("emdash-"),
                   cwd=parts[1] if len(parts) > 1 else "",
                   pane_current_command=parts[2] if len(parts) > 2 else "",
               )
       return None
   ```

5. **`kill_window(window_id)`** — guard for foreign windows:

   ```python
   async def kill_window(self, window_id):
       if _is_foreign_window(window_id):
           logger.info("Skipping kill for external window %s", window_id)
           return False
       # ... existing path
   ```

6. **`list_windows()`** — unchanged. Only lists native windows. Foreign windows are discovered separately.

**New discovery method:**

```python
async def discover_emdash_sessions(self) -> list[TmuxWindow]:
    """Discover emdash tmux sessions and return as TmuxWindow list."""
    proc = await asyncio.create_subprocess_exec(
        "tmux", "list-sessions", "-F", "#{session_name}",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    stdout, _ = await proc.communicate()
    if proc.returncode != 0:
        return []

    results = []
    for session_name in stdout.decode().strip().split("\n"):
        if not session_name.startswith("emdash-"):
            continue
        # Get window info for this session
        proc2 = await asyncio.create_subprocess_exec(
            "tmux", "list-windows", "-t", session_name,
            "-F", "#{window_id}\t#{pane_current_path}\t#{pane_current_command}",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        stdout2, _ = await proc2.communicate()
        if proc2.returncode != 0:
            continue
        for line in stdout2.decode().strip().split("\n"):
            if not line:
                continue
            parts = line.split("\t", 2)
            wid = parts[0] if parts else ""
            cwd = parts[1] if len(parts) > 1 else ""
            cmd = parts[2] if len(parts) > 2 else ""
            qualified_id = f"{session_name}:{wid}"
            results.append(TmuxWindow(
                window_id=qualified_id,
                window_name=session_name,
                cwd=cwd,
                pane_current_command=cmd,
            ))
    return results
```

---

### Change 4: Window picker shows emdash sessions

**File:** `src/ccbot/handlers/directory_browser.py` (~25 lines added)

In `_show_window_picker()`, after listing unbound ccbot windows, also list emdash sessions:

```python
async def _show_window_picker(update, context, ...):
    # ... existing: list unbound native windows

    # Discover emdash sessions
    emdash_windows = await tmux_manager.discover_emdash_sessions()
    # Filter out already-bound emdash windows
    bound_ids = set(session_manager.iter_all_bound_window_ids())
    emdash_unbound = [w for w in emdash_windows if w.window_id not in bound_ids]

    if emdash_unbound:
        for w in emdash_unbound:
            provider = _parse_emdash_provider(w.window_name)
            label = f"📎 {Path(w.cwd).name} ({provider})"
            # Add to the inline keyboard with same callback pattern
            buttons.append(InlineKeyboardButton(label, callback_data=f"{CB_WIN_BIND}{idx}"))
            # Store mapping for callback resolution
            ...
```

The callback handler (`_handle_window_bind`) already binds thread → window_id. Since emdash windows use qualified IDs, the binding just stores the qualified ID.

---

### Change 5: Lifecycle guards

**Files:** `src/ccbot/handlers/status_polling.py`, `src/ccbot/handlers/cleanup.py` (~10 lines)

1. **Auto-kill unbound windows** (`_check_unbound_window_ttl`): Skip external windows.

   ```python
   # In _check_unbound_window_ttl:
   state = session_manager.get_window_state(wid)
   if state.external:
       continue  # don't kill emdash windows
   ```

2. **Topic deletion** (in status_polling `_handle_topic_deleted`): Don't kill external windows.

   ```python
   # Where kill_window is called on topic close:
   state = session_manager.get_window_state(wid)
   if not state.external:
       await tmux_manager.kill_window(w.window_id)
   ```

3. **Dead window detection**: Works as-is. When emdash kills a session, `find_window_by_id` returns None, triggering existing dead-window recovery flow. The recovery keyboard can offer "Unbind" instead of "Fresh/Continue/Resume" for external windows.

---

### Change 6: Status polling for foreign windows

**File:** `src/ccbot/handlers/status_polling.py` (~5 lines)

The main poll loop calls `tmux_manager.list_windows()` to get live windows, then matches against bindings. Foreign windows aren't in `list_windows()`.

Fix: also call `discover_emdash_sessions()` and merge into the live window set:

```python
# In _poll_status_for_users():
all_windows = await tmux_manager.list_windows()
emdash_windows = await tmux_manager.discover_emdash_sessions()
all_windows.extend(emdash_windows)
window_lookup = {w.window_id: w for w in all_windows}
```

This makes the existing per-window polling logic work for emdash windows — capture_pane, status detection, emoji updates, etc.

---

### Change 7: Startup re-resolution for foreign windows

**File:** `src/ccbot/session.py` (~10 lines)

Foreign window IDs include the session name, which is stable in emdash (deterministic from task ID). So `@0` within an emdash session doesn't change on tmux restart (emdash recreates with same session name via `-As`).

However, if an emdash session doesn't exist at ccbot startup:

- `resolve_stale_ids()` should skip foreign windows (they're managed externally)
- Dead foreign bindings are cleaned up by the regular status polling dead-window detection

```python
# In resolve_stale_ids():
# Skip foreign window IDs — their session names are stable
if _is_foreign_window(old_wid):
    continue
```

---

## Files Changed Summary

| File                                      | Change                                                                             | Lines          |
| ----------------------------------------- | ---------------------------------------------------------------------------------- | -------------- |
| `src/ccbot/session.py`                    | WindowState.external, load_session_map emdash pass, resolve_stale_ids skip, helper | ~50            |
| `src/ccbot/tmux_manager.py`               | Foreign window helpers, subprocess paths, discover_emdash_sessions, kill guard     | ~80            |
| `src/ccbot/handlers/directory_browser.py` | Window picker shows emdash sessions                                                | ~25            |
| `src/ccbot/handlers/status_polling.py`    | Merge emdash windows into poll loop, lifecycle guards                              | ~15            |
| **Total**                                 |                                                                                    | **~170 lines** |

## What's NOT Changed

- `hook.py` — already writes correct entries for any tmux session
- `session_monitor.py` — reads session_map and events.jsonl as-is
- `config.py` — no new config needed (auto-discovery)
- `providers/` — existing provider detection works (from session_map `provider_name`)
- `cleanup.py` — no changes (delegates to status_polling)
- `transcript_parser.py` — transcript reading is provider-agnostic
- emdash — zero changes to emdash itself

## Testing Strategy

1. **Unit tests:** Mock tmux subprocess calls for `discover_emdash_sessions`, foreign window operations
2. **Integration test:** Create a fake emdash tmux session (`tmux new-session -d -s emdash-claude-main-test`), verify ccbot discovers it, can send/capture
3. **Manual E2E:** Run emdash with `tmux: true`, bind a topic, verify bidirectional communication

## Risks & Mitigations

| Risk                                          | Mitigation                                                                                                 |
| --------------------------------------------- | ---------------------------------------------------------------------------------------------------------- |
| emdash changes session naming                 | Parse prefix `emdash-` only; provider extraction is best-effort                                            |
| tmux subprocess overhead in poll loop         | `discover_emdash_sessions` is 1-2 subprocess calls per poll cycle (2s interval) — negligible               |
| Qualified window_id breaks string assumptions | All existing code uses string keys; no format assumptions beyond `@`-prefix for native (which we preserve) |
| ccbot and emdash both send keystrokes         | Not a problem — tmux handles concurrent input; user intent determines which interface they use             |
| Hook conflicts                                | Claude Code supports arrays of hooks per event; both coexist natively                                      |

## Future Enhancements (Not In Scope)

- Auto-create topics for new emdash sessions (notification-driven)
- Show emdash task metadata (from emdash's SQLite) in Telegram
- Sync emdash worktree diffs to Telegram
- Support emdash sessions over SSH (remote tmux)
