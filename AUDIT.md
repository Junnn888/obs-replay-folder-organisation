# RecORDER audit — does it do the job?

Repo: https://github.com/oxypatic/RecORDER (main, `RecORDER.py` v3.1.1, 1238 lines).
Method: five independent agents (2× Opus, 3× Sonnet) audited correctness, OBS API usage,
file-move/threading, improvement options, and docs/upstream context. Their claims were
cross-checked against each other, against the obs-studio source, and where possible
empirically against the real `obspython` binding shipped with the OBS 32.2.2 install on
this machine. One agent claim was refuted by that check and is recorded below.

## Verdict

**It does the job in the happy path only.** Windows, one Game Capture at the top level of
the active scene, a game with a Latin-alphabet title that is still running when you press
the replay hotkey, fast local disk: the clip lands in `<dir>/<Game>/replay/`. The two
design decisions that matter most are right and should not be touched:

- the game name is resolved **live at save time** by asking the source's `get_hooked`
  procedure, not from a cached value, and
- the file move happens **off the OBS thread**.

Outside that path it fails quietly rather than loudly, and one bug actively harms the user.

## Findings, ranked

| # | Severity | Finding | Where (orig. lines) |
|---|---|---|---|
| 1 | **Blocker** | Changing any script setting (or reloading/unloading the script, or OBS exit) **stops an active recording and force-saves + stops the replay buffer**. `script_update` → `shutdown()` → the scene-collection cleanup path. | 803-824, 863-866, 1134-1140, 1188 |
| 2 | **Bug (verified vs. C source)** | The `hooked` signal is **never connected**: `signal_handler_connect` is given a bound method, which obspython's C wrapper silently rejects (`PyFunction_Check`). The author already worked around this for `file_changed` with a closure but not here. Consequence: `HookState.window_title` is never set from the signal, `isWindowHooked()` is always false, and the only thing that works is the live `get_hooked` query. | 264-266, 586-592 |
| 3 | Bug | The `file_changed` disconnect passes a different callable than was connected, so it never disconnects. Every settings change leaves a dead RecordingManager attached to the recording output; after a few changes, split recordings are processed by several managers racing with stale settings. | 611-613 vs 627-631 |
| 4 | Bug | Replay saved shortly **after the game closed** (crash, alt-F4, then hotkey) always goes to the fallback folder. The remembered-title accessor that would fix it is dead code. | 344-346, 370-373 |
| 5 | Bug (empirically confirmed) | Sanitizer keeps only `[A-Za-z0-9 ]`. Non-Latin titles (`原神`, `Смута`) become `""`, so the game-folder layer is silently dropped and all such games pile into one `replay/` folder. Hyphens are deleted too (`Counter-Strike 2` → `CounterStrike 2`). No length cap, no reserved-name guard (`NUL`). | 291-305, 403-411 |
| 6 | Bug (empirically confirmed) | The block commented `# Retry logic` is a single 100 ms sleep and one attempt. A file OBS or antivirus still holds is abandoned in the root folder with only a log line. | 541-551 |
| 7 | Bug (empirically confirmed) | `shutil.move` onto an existing same-named file **silently overwrites it** (copy2 + unlink fallback on Windows). | 551 |
| 8 | Bug | Source picker enumerates only top-level, currently-visible items of the current scene. A Game Capture inside a group or nested scene, or hidden behind an overlay, cannot be selected at all. A stored uuid not in the list makes the combo silently rebind to entry 0 and persist it. | 983-1015, 1142-1145 |
| 9 | Bug | No recovery when the capture source is deleted and recreated (new uuid); everything falls back until script reload. No type validation on the configured source. | 112-114, 232-247, 748-752 |
| 10 | Bug | Raw `signal_handler_t*` is kept after the source reference is released; disconnecting on `SCENE_COLLECTION_CHANGED` (after sources are freed) is a use-after-free risk. Should detach on `..._CHANGING`. | 268, 249-257, 819 |
| 11 | Bug | "Check for updates" is dead: the button callback takes 0 args but OBS calls it with 2 (TypeError every click), and the real logic sits in a modified-callback that OBS never invokes for buttons. It also compares `"3.1.1"` to tag `"v3.1.1"`, calls GitHub twice, and blocks the UI thread. | 909-937, 1109-1112 |
| 12 | Bug | Linux is claimed but the X11 source id is `xcomposite_input`, which the picker never matches. macOS window capture has no hook concept at all. | 44-47, 947-952 |
| 13 | Fragile | Three `finally` blocks reference a local that is unbound if the acquiring call raised (masks the real error). | 246, 971, 1009 |
| 14 | Fragile | Unknown organization mode → `UnboundLocalError`; `ReplayState.last_file_path` is annotated but never assigned; `getctime` computed even when unused. | 528-539, 149-150, 525 |
| 15 | Fragile | Per-file daemon threads are never joined; OBS exit can kill a cross-drive copy midway. `asyncio.run` per file is pure overhead. | 474-551 |
| 16 | Fragile | Moving the file at `RECORDING_STOPPED` fights OBS's auto-remux (upstream issue #35, open). | 575-584 |
| 17 | Nit | `onFileChange` ignores the `next_file` calldata and re-queries the frontend from the muxer thread; title resolved twice per split. | 634-652 |
| 18 | Docs | README swaps the two checkbox descriptions, omits the replay/screenshot folder-name settings, claims Python 3.11 (true floor 3.10) and OBS 29.0 (needs 29.1 for `obs_source_get_uuid`), and links `padiix/` (same repo, renamed). | README |

**Refuted claim.** One agent reported that `obs_data_get_string(settings, name=...)` would raise
`TypeError` because the SWIG build lacks keyword support. Tested against the real binding: it
works. The script does initialize.

**Correct and worth keeping:** live `get_hooked` query with `calldata_destroy` in `finally`;
`REPLAY_BUFFER_SAVED` + `obs_frontend_get_last_replay()`; `RECORDING_STOPPED` over `STOPPING`;
exception-isolated event dispatch; reference counting in the query path; consistent
`os.path.join`; no obspython calls from the worker thread.

## Upstream context

- `padiix/RecORDER` redirects to `oxypatic/RecORDER`; one repo, renamed.
- Latest release v3.1.1 (Jan 2024). The `main` branch is a class-based rewrite that post-dates it.
- Open issue #35 (auto-remuxed files not moved) is finding 16. Closed issues #14, #21, #23, #26
  are earlier versions of findings 4, 6, 9 and 17.

## What was changed (v3.2.0 in this working copy)

See the implementer's report and `tests/` for the fix list; the short version: no output is ever
stopped by the script; signals connect via stored closures and are disconnected symmetrically;
grace window for the last hooked game; Unicode-safe sanitizer with executable fallback; single
worker thread with retry, collision-safe naming and join-on-unload; recursive source discovery
with auto-detect; remux-aware recording move; working update button; README corrected.
