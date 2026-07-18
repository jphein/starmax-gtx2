# CLI command audit — opcode confidence + hardening (issue #6)

Audit of every standalone-client command: wire-opcode confidence, prerequisites, live-test
readiness, and risk. Confidence tags: **[CAP]** = wire opcode reproduced against a real capture ·
**[SCHEMA]** = payload from the APK schema but the **wire opcode is UNRESOLVED** (protocol-spec
§1.4: the SDK's REV_TYPE values are NOT the wire opcodes) · **[INFERRED]** = opcode is a
placeholder/guess, never captured.

Every command's `--dry-run` was confirmed to emit a structurally valid `0xC1` frame (36/36
catalog commands; locked by `tests/test_command_audit.py`). "Live-safe" below means the **wire
opcode** is trustworthy — not that on-watch behavior is verified.

## Risk tiers (read this first)

### ✅ FIXED (issue #9) — the camera opcode collision
| cmd | group | opcode | resolution |
|---|---|---|---|
| `camera` | notify | ~~0x04~~ → **0x1d** | `UNVERIFIED_OP_CAMERA_CONTROL` used to be `0x04`, which **collided with the real [CAP] `OP_FEATURE_BITMAP=0x04`** — a live `camera` would write a feature bitmap. Re-opcoded to a non-colliding, non-[CAP] placeholder (`0x1d`) and **--force-gated** (below), so it can never hit `0x04` and can't be fired live by accident. Still UNVERIFIED — confirm the real wire opcode via a capture. |

### 🟠 HIGH — wrong-layer opcode (watch will not understand; do NOT fire live)
These carry SDK REV_TYPE / placeholder opcodes, not wire opcodes. Safe to `--dry-run`; a live
send is a no-op at best, misinterpreted at worst.
| cmd | group | opcode | source |
|---|---|---|---|
| `device-state` | settings | 0x82 | `SDK_OP_DEVICE_STATE` (SET_Device_State) |
| `wrist-raise` | settings | 0x82 | `SDK_OP_DEVICE_STATE` |
| `sport-goals` | settings | 0x8a | `SDK_OP_SPORT_GOAL` |
| `dnd` | settings | 0xb4 | `SDK_OP_DND` |
| `sedentary` | settings | 0xb6 | `SDK_OP_SEDENTARY` |
| `drink-water` | settings | 0xb7 | `SDK_OP_DRINK` |
| `event-reminders` | settings | 0xbb | `SDK_OP_EVENT` |
| `call` | notify | 0x14 | `UNVERIFIED_OP_PHONE_CONTROL` (GUESS) |
| `music` | notify | 0x15 | `UNVERIFIED_OP_MUSIC_CONTROL` (GUESS) |
| `sport-control` | files | 0x1a | `OP_SPORT` placeholder |
| `nfc-list` | files | 0x1c | `OP_NFC` placeholder |

### 🟡 MEDIUM — real opcode, unverified payload/subop (opcode safe, behavior unconfirmed)
| cmd | group | opcode | note |
|---|---|---|---|
| `dial-switch` | files | 0x16 [INFERRED-payload] | reuses the real dial opcode 0x16 with a set-shaped `Notify.DialInfo` payload — uncaptured; install auto-activates (issue #8), so rarely needed |
| `dial-activate` | dials | 0x16 [INFERRED-payload] | delegates to `dial-switch` |
| `realtime-open` / `realtime-measure` | health | 0x0e **[SCHEMA / UNRESOLVED opcode]** | ⚠️ NOT live-safe: `0x0e` is the health-**switch** opcode, NOT a realtime opcode — sending realtime on it just **ACKs, does not stream** (confirmed live). The real realtime-enable opcode is unresolved (under investigation). The old Java/legacy `0x70/0x0d` are gone, but `0x0e` is not the answer either. **--force-gated.** |
| `hr-config`, `female-health-set`, `health-interval` | health | 0x0e [SCHEMA-payload] | real 0x0e opcode, schema payload |
| `aod`, `world-clock`, `date-format` | settings | 0x22 [CAP-opcode] | ride the real generic setting-KV 0x22; the key/value layout is schema-derived |

### 🟢 LOW — [CAP], wire-trustworthy (live-safe frames)
`bind` (0x01) · `set-time` (0x02) · `user-profile` (0x03) · `feature-bitmap` (0x04 — the real
notification/feature enable; note the `camera` 0x04 collision flagged above) · `alarm-get`/`alarm-set` (0x07) ·
`health-switch-read`/`-write`, `history-status`/`-sync`, `hr-`/`spo2-`/`sleep-history` (0x0e) ·
`notify-detailed` (0x11) · `weather` (0x12) · `notify-summary` (0x13) · `find` (0x18) ·
`dial-list` (0x16) · `setting-query` (0x22) · core verbs `scan`/`pair`/`activate`/`set-time`/
`find`/`sync-health`. **`dial-push`** (D-plane D1/D2/D3/D4) — [CAP] and **live-validated on
hardware** (issue #8). `dial-build` — offline transcode, no BLE.

## Prerequisites
- **`notify`** — needs the `0x04`+`0x03` notification-enable exchange first (wired in PR #4);
  even then, *display* is gated on classic-BT companion presence (issue #5 / `docs/notifications.md`)
  — out of scope on LE-only.
- **`dial-push`** — the blob must be a native container (or a ZIP, auto-transcoded); auto-activates.
- No other command has a missing enable-prereq to wire — the [CAP] commands work after `bind`.
  (`activate` already sends the full vendor setup handshake 0x01/0x22/0x05/0x16→time→0x0e→0x04→0x03.)

## Ready-to-live-test (for team-lead's on-watch pass), in priority order
1. 🟢 **[CAP] settings on real opcodes:** `setting-query`, `aod`, `world-clock`, `date-format`
   (0x22) — confirm the KV key/value mapping renders on-watch.
2. 🟡 **health schema-payloads on 0x0e:** `hr-config`, `health-interval`, `female-health-set`,
   `realtime-open`/`measure` — real opcode; verify the payload is honored.
3. 🟡 **`dial-switch`/`dial-activate` (0x16 set):** confirm switching among installed faces.
4. 🟠 **DO NOT live-test** the wrong-layer set (0x82/0x8a/0xb4–0xbb, 0x14/0x15/0x1a/0x1c) or
   `camera` (0x04) until their wire opcodes are captured — they need a fresh app-capture of each
   feature, not a live poke.

## Hardening — DONE (issue #9)
1. ✅ **Camera 0x04 collision fixed** — re-opcoded to a non-colliding placeholder (`0x1d`),
   `--force`-gated (below). Locked by `test_camera_no_longer_collides_with_feature_bitmap`.
2. ✅ **Live-send `--force` guard** — `commands.GATED_COMMANDS` + `commands.requires_force()`; every
   group subcommand gained `--force`; `cli._run` **refuses** to send a gated (non-verified)
   command live without `--force` (`--dry-run` always open). So a wrong-layer opcode can't reach
   the watch by accident. All 🔴/🟠/🟡 commands above are gated; the 🟢 [CAP] set is not.
   *(Note: the files-group commands `nfc-list`/`sport-control`/`dial-switch` use the older
   register-time-closure client, so they are already **print-only** in the CLI — never sent live —
   which is an even stronger guarantee than the `--force` gate.)*
3. ✅ **Regression lock** — `tests/test_command_audit.py` pins every command→opcode, asserts
   `--dry-run` validity, locks the non-[CAP] set + the camera fix, and tests the guard.

## The [CAP] wire-opcode set (ground truth)
`0x01 0x02 0x03 0x04 0x05 0x07 0x0e 0x10 0x11 0x12 0x13 0x16 0x18 0x22` (capture-confirmed).
Anything else a builder emits is [SCHEMA]/[INFERRED] and must not be trusted on the wire.
