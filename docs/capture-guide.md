# BLE/HCI Capture Guide — Starmax GTX2 (Runmefit)

## Environment (verified 2026-07-11)
- Phone: <phone-model> (`<device-serial>`, codename `<codename>`), Android 15 / SDK 35, **non-rooted**.
- Runmefit installed: `com.starmax.runmefit`.
- Snoop logging: **FULL** (unfiltered) — confirmed via `dumpsys bluetooth_manager` → `sSnoopLogSettingAtEnable = FULL`.
  - Enabled via Developer Options → "Enable Bluetooth HCI snoop log" → **Enabled** (root-only property; set through the privileged Settings app by UI automation, then BT cycled so the stack re-read it at enable time).
- Watch state at start: **never paired, no account** — so we capture the full first-pair + auth handshake.

## Transport note (IMPORTANT)
The watch may use **classic RFCOMM/SPP**, **BLE GATT**, or both (dual-mode BT 5.3). FULL snoop logs *both* classic and LE HCI, so nothing is missed. Custom SPP UUID seen pre-capture: `5e8945b0-9525-11e3-a5e2-0800200c9a66`. During decode, do NOT filter blindly on `btatt` — also inspect RFCOMM/SPP.

## Retrieval is non-root
`/data/misc/bluetooth/logs/` is root-only. The log is pulled via `adb bugreport` (runs as privileged `dumpstate`), which bundles `FS/data/misc/bluetooth/logs/btsnoop_hci.log`. Helper: `scripts/pull-btsnoop.sh`.

## Capture sequence
Actions done in the Runmefit app by the user; the operator drives adb (launch, notifications, log pull) and timestamps.

### Phase 1 — Pairing + auth handshake (most critical)
1. Create account / log in (network only; not BLE-relevant).
2. Add device → pair the GTX2. Grant notification access + BT permissions when prompted.
3. Wait until connected and initial sync finishes (time sync happens here automatically).
4. **Checkpoint: pull btsnoop now** to protect the handshake from buffer rotation.

### Phase 2 — Features
5. Notification: post a test notification (the operator can `cmd notification post`) → confirm it appears on watch.
6. Trigger a manual **HR** measurement in-app.
7. Trigger **SpO2** measurement.
8. Start a **GPS workout**, run ~30 s, stop it.
9. Open **weather** (usually pushed on connect / on the weather screen).
10. Set an **alarm**.
11. Force a **health sync** (pull-to-refresh steps/sleep).
12. **Pull final btsnoop.**

## Correlation
Note wall-clock time of each action; btsnoop timestamps are absolute. Cross-reference against the protocol spec (`docs/protocol-spec.md`) to name each command.
