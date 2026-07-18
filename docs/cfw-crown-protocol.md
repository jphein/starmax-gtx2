# Custom-Firmware Crown-over-BLE Protocol (issue #31, option C — crown→HA light dimmer)

**Date:** 2026-07-14 · **Status:** SPEC + client consumer (firmware not yet
built). **Lane:** standalone-client (any source OK for the client).

This defines the BLE protocol a **custom** GTX2 firmware would expose to stream the **rotary crown**
(rotation deltas + the crown push-button) to a host, so the crown can drive a Home Assistant light
(rotate = dim, press = on/off). It ships a complete, test-green client consumer
(`starmax_client/crown.py` + the `crown` CLI verb).

**The firmware half does NOT exist.** Stock GTX2 firmware handles the crown *entirely locally* — the
rotary encoder scrolls the on-watch UI and never leaves the SoC over BLE (there is no crown
characteristic, no crown opcode, nothing on the wire). Streaming the crown is therefore a
custom-firmware feature (SDK rebuild), exactly like raw-accel. This document is the contract between
that future firmware and the client; the client is validated against **synthetic** frames built to
this spec (PII-clean — the crown emits only rotation counts and button edges, no biometric data).

It is the **twin** of [`cfw-rawaccel-protocol.md`](cfw-rawaccel-protocol.md): same 0x0FF0 notify
plane, same framing, a **new collision-free opcode `0xA1`** (raw-accel owns `0xA0`), so the existing
`framing.parse_frame` CRC-decodes crown frames **unchanged**. Read the raw-accel doc first — this one
only states the deltas.

Framing primitives (envelope, LEN/CRC rules, 0xC3 fragmentation, CRC-16/CCITT-FALSE) are defined in
[`protocol-spec.md`](protocol-spec.md) §1 and implemented in `starmax_client/framing.py`.

---

## 1. Where the crown lives in the SDK (the firmware hook point)

Confirmed against the Actions SDK source (the Actions SDK source tree, Zephyr 2.7.0). Mirrors how
`cfw-board-port.md` §3 mapped the accel path.

**Rotary encoder (rotation):**
- **Driver:** `zephyr/drivers/input/knobencoder_acts.c` — a 2-GPIO quadrature decoder. Reads pins
  `CONFIG_KNOBGPIO_INIA` / `CONFIG_KNOBGPIO_INIB` (GPIO_62 / GPIO_63 on the `ats3085s_dev_watch_ext_nor`
  reference board — see its `board.h`), configured `GPIO_PULL_UP | GPIO_INPUT | GPIO_INT_DEBOUNCE`.
  A per-edge IRQ state machine (`KNOB_IRQ_callback`) resolves each detent to a direction and fires
  the registered notify with `val.keypad.value = KEY_KONB_CLOCKWISE (30)` or
  `KEY_KONB_ANTICLOCKWISE (31)` (`framework/system/include/input_manager_type.h`). Built only when
  `CONFIG_KNOB_ENCODER=1`; the input subsystem hook needs `CONFIG_INPUT_DEV_ACTS_KNOB=y`. Device name
  `KNOB_ENCODER_DEV_NAME = "knobencoder"` (`zephyr/framework/include/key_hal.h`).
- **Framework consumer (the clean tap):** `framework/system/input/input_knob.c` —
  `input_knob_device_init()` binds `"knobencoder"` and registers `knob_notify_callback(dev, val)`,
  which stores `knob_val = val->keypad.value` and `k_work_submit(&knob_work)` →
  `sys_event_report_input(knob_val)`. **This callback is where every crown detent appears, with its
  direction, in a work-queue-safe context** — the ideal place to also feed a BLE emitter.

**Crown push-button:**
- The crown press is a **separate** input from the encoder A/B lines. On the reference board it is
  either a GPIO key (`gpiokey_acts.c`, `CONFIG_GPIOKEY`, GPIO_24) or the PMU on/off key
  (`onoff_key_acts.c`). Button edges flow through `framework/system/input/input_keypad.c` and are
  reported with a key value **and** a `KEY_TYPE_*` stage (`input_manager_type.h`):
  `KEY_TYPE_SHORT_DOWN`, `KEY_TYPE_SHORT_UP`, `KEY_TYPE_LONG_DOWN`, `KEY_TYPE_LONG`,
  `KEY_TYPE_LONG_UP`, `KEY_TYPE_HOLD`, `KEY_TYPE_DOUBLE_CLICK`, …
- **Unified tap:** `input_manager_init(event_trigger event_cb)` registers a callback invoked for
  **every** key event (rotation keys *and* button stages) as `event_cb(uint32_t key_value,
  uint16_t type)` *before* UI dispatch — one hook covers both halves.

**GTX2 delta [needs-teardown, #31]:** the EVB build target `ats3085s4_dev_watch_ext_nor` ships with
`CONFIG_KNOB_ENCODER=n` (no crown), and the GTX2's crown-encoder GPIOs + crown-button GPIO are
unknown until the teardown/NOR-dump. `cfw-board-port.md` §1.5 already tags this
(`enable INPUT_DEV_ACTS_KNOB`, wire encoder GPIOs). This protocol is independent of *which* GPIOs —
it only rides the `input_knob`/keypad events above.

---

## 2. Transport & opcode

- **Service / chars:** the existing custom command service **`0x0FF0`** — app→watch **write char
  `0x0001`**, watch→app **notify char `0x0002`**. No new GATT objects (same rationale as raw-accel
  option B).
- **Opcode `0xA1` = `CROWN`.** Collision-free with every namespace in play: stock GTX2 wire opcodes
  `0x01–0x22`, the Java-SDK REV block `0x31–0x3C` and realtime toggle `0x70`, and the raw-accel
  extension `0xA0`. `0xA1` sits one above raw-accel, unmistakably a custom-firmware extension.
- **`flag` byte selects the sub-frame:**
  - `flag = 0x00` → **CONTROL** (protobuf; app→watch enable/disable, watch→app ack).
  - `flag = 0x01` → **DATA** (binary event batch; watch→app only).
- **MTU:** 247 negotiated on-device → **244-byte** ATT payload per PDU. Crown frames are tiny and
  always single-PDU (§4.2).

---

## 3. Control plane — `0xA1` flag=0 (protobuf)

Standard C1 command/reply, identical framing to every other protobuf opcode: app→watch requests
carry no CRC (`LEN = total`); the watch→app ack is a CRC-bearing protobuf reply (`LEN = total − 2`).

### 3.1 Enable / disable (app→watch)

| field | type | meaning |
|-------|------|---------|
| f1 | varint | **command**: `1` = START (enable), `0` = STOP (disable) |
| f2 | varint | **report_rotation** — `1`/`0`, stream rotation-delta events (default 1) |
| f3 | varint | **report_button** — `1`/`0`, stream button events (default 1) |
| f4 | varint | **coalesce_ms** — rotation coalescing window in ms; `0` = emit each detent immediately, `>0` = accumulate detents for that window then emit the **net** delta (HA rate-limit knob). Default 0. |

STOP carries `f1=0` only. The watch **clamps** unsupported values and reports the **actual**
settings in the ack — the client trusts the ack, not the request.

Byte-exact enable (START, rotation+button on, coalesce 0, seq 0x05):
`c1 05 01 01 00 a1 13 00 00 00 00  08 01 10 01 18 01 20 00`
(payload `08 01 10 01 18 01 20 00` = `f1=1, f2=1, f3=1, f4=0`). Locked by
`tests/test_crown.py::test_enable_frame_byte_exact`.

### 3.2 Ack (watch→app, CRC-bearing protobuf)

| field | type | meaning |
|-------|------|---------|
| f1 | varint | **status**: `0`=ok/streaming, `1`=busy, `2`=no_crown (hardware/board not wired) |
| f2 | varint | **report_rotation** — actual |
| f3 | varint | **report_button** — actual |
| f4 | varint | **detents_per_rev** — hardware detents per full revolution (0 = unknown); lets HA scale a full turn to a brightness sweep |
| f5 | varint | **base_ts_ms** — device-monotonic ms at stream start (anchor; optional) |

---

## 4. Data plane — `0xA1` flag=1 (binary event batch, watch→app)

**The key framing choice (same as raw-accel §4):** the data frame is a **normal watch→app
CRC-bearing C1 frame** whose payload happens to be binary. `0xA1 flag=1` is **not** the `0x0e flag=1`
binary-record special case, so `framing.parse_frame` treats it as the ordinary watch→app protobuf
*class* (`LEN = total − 2`, CRC-16/CCITT-FALSE trailer over `frame[0:LEN]`, `payload = frame[11:LEN]`).
The framing layer never assumes protobuf — it slices bytes and verifies the CRC — so this rides the
existing plane with **zero** changes to `framing.py`.

### 4.1 Payload layout (little-endian)

12-byte header + N × 4-byte event — deliberately the **same header shape** as raw-accel so drop
detection and timing reuse.

```
off 0     u8    version    = 0x01           payload-format version
off 1     u8    cfg                          bit0 = rotation convention (0 = clockwise is +); [7:1]=0
off 2     u8    reserved   = 0x00
off 3     u8    count (N)                     events in this frame (1..MAX, see §4.2)
off 4-5   u16   frame_seq                     per-stream frame counter, wraps 0x10000 (drop detect)
off 6-9   u32   base_ts_ms                    device-monotonic ms of event[0]
off 10-11 u16   reserved   = 0x0000
off 12..  N×4   events                        each: [ev_type:u8][ev_detail:u8][value:i16 LE]
```

**Event record (4 bytes, `<BBh`):**

| ev_type | meaning | ev_detail | value (i16) |
|---------|---------|-----------|-------------|
| `1` ROTATE | crown rotated | `0` (reserved) | **signed step delta**: `+N` clockwise, `−N` counter-clockwise (net detents when coalesced) |
| `2` BUTTON | crown pressed | button action code (below) | `0` (reserved) |

**Button action codes (ev_detail when ev_type=2):** `1` DOWN, `2` UP, `3` CLICK (short press
released), `4` LONG (long-press reached), `5` DOUBLE (double-click), `6` LONG_UP (release after a
long-press). These map 1:1 from the SDK `KEY_TYPE_*` stages (§1). HA typically only needs CLICK
(toggle) and LONG (e.g. full-off / scene) — the rest are there for richer automations.

- **Drop detection:** the consumer tracks `frame_seq`; a gap = dropped frames
  (`crown.detect_drops`). Independent of the C1 envelope `seq`.
- **Timing:** crown events are **event-driven, not fixed-rate** — each frame carries its own
  `base_ts_ms` (the ms of `event[0]`). The protocol does not interpolate per-event times (unlike
  raw-accel's ODR); a coalesced rotation frame reports one net delta at one timestamp.
- **Rotation semantics:** net-delta accumulation is idempotent under drops — a lost frame loses that
  delta but the crown is a *relative* control, so HA simply applies fewer brightness steps; there is
  no absolute state to desync.

### 4.2 Batch sizing (MTU-247, single-PDU)

Per-PDU budget for events: `mtu_payload − C1_header(11) − data_header(12) − CRC(2)`.

| MTU | mtu_payload | budget | **max events (÷4)** |
|-----|-------------|--------|---------------------|
| 247 | 244 | 219 | **54** |
| 185 | 182 | 157 | 39 |
| 23 (floor) | 20 | −5 | 0 → must fragment/shrink |

Crown traffic is human-paced (a fast spin is tens of detents/s; a press is a handful of edges), so a
frame **never** approaches 54 events — every crown frame is single-PDU, no `0xC3` fragmentation. The
firmware emits a frame per event (or per coalesce window). `crown.max_events_per_frame(mtu_payload)`
computes the ceiling; the generic `Reassembler` still rejoins a fragmented batch if one ever exceeds
a PDU.

---

## 5. Coexistence with stock C1 traffic

Identical to raw-accel §6: routed by opcode (a `0xA1 flag=1` listener gets crown batches, everything
else untouched), self-contained single-PDU frames never land mid-fragment, and the ~human event rate
is negligible airtime. Data frames use the normal watch→app `seq | 0x80`; the app never acks them.

Crown (`0xA1`) and raw-accel (`0xA0`) can stream **simultaneously** on the same pipe — different
opcodes, independent `frame_seq` — so a future "dim the light *and* log wrist motion" mode is free.

---

## 6. Firmware implementation notes

Mirrors raw-accel §7; see the build-ready draft in the Actions SDK source tree (`crown_ble.c`/`.h`).

1. **Rotation source:** register a notify on `KNOB_ENCODER_DEV_NAME` (exactly as `input_knob.c`
   does) — the cb receives `KEY_KONB_CLOCKWISE`/`KEY_KONB_ANTICLOCKWISE`; accumulate `+1`/`−1` into a
   pending delta.
2. **Button source:** feed the crown-button `KEY_TYPE_*` stage into `crown_ble_report_button(type)`
   from the keypad path / the `input_manager_init` `event_cb` (one line).
3. **Emit:** on a button edge, or when the coalesce window fires with a non-zero delta, build a `0xA1
   flag=1` frame = header + event(s), compute CRC-16/CCITT-FALSE over `frame[0:LEN]`, notify on char
   `0x0002` via `bt_manager_ble_send_data()`. Reuse the SDK's existing watch→app notify path.
4. **Control:** on `0xA1 flag=0` START, latch report_rotation/report_button/coalesce_ms, set
   `base_ts_ms`, reply with the ack (§3.2). On STOP, stop emitting and reply `status=0`.
5. **iBeacon / fast-advert bonus (same firmware):** to make the watch a *snappy* room beacon for HA
   presence (§ `cfw-crown-ha.md`), the same build can (a) drop the advertising interval from stock
   ~4 s to ~250–500 ms and/or (b) add an iBeacon manufacturer-data AD via the `bt_manager_ble`
   advertising path (`bt_manager_ble_adv_start`, `struct bt_le_adv_param`). This is orthogonal to the
   crown opcode but ships together because both are custom-firmware-only.
6. **Open hardware unknown:** which GPIOs the GTX2 crown encoder + button use, and enabling
   `CONFIG_KNOB_ENCODER` / `CONFIG_INPUT_DEV_ACTS_KNOB` for the ported board — resolved by the
   teardown/NOR-dump (#17). Does **not** affect this wire protocol.

---

## 7. Client consumer (this deliverable)

`starmax_client/crown.py` — transport-independent, offline-testable, mirrors `rawaccel.py`:

- **Builders:** `build_crown_enable(report_rotation, report_button, coalesce_ms, seq)`,
  `build_crown_disable(seq)` → app→watch control frames.
- **Parsers:** `parse_crown_ack(payload)` → dict; `parse_crown_frame(payload)` → `CrownBatch`
  (header fields + `CrownEvent`s). Raises `CrownError` on bad version / truncation / bad cfg.
- **Semantics helpers:** `CrownEvent.is_rotation` / `.is_button` / `.button_action` (name),
  `CrownBatch.net_rotation()` (sum of rotation deltas), `detect_drops`, `max_events_per_frame`.
- **Firmware-emulation encoders** (tests + golden vectors): `build_crown_data_frame(events, …)`,
  `build_crown_ack_frame(…)` produce byte-exact watch→app wire frames. `rotate(delta)` /
  `button(action)` helpers construct event tuples.
- **CLI verb `crown`:** `--decode <hex>` (offline decode), `--dry-run` (print the enable frame),
  live streaming behind `--force` (refused otherwise — opcode 0xA1 is custom-firmware-only and
  unverified).

Tests: `tests/test_crown.py` — all synthetic (PII-clean), covers control byte-layout + validation,
data-frame decode round-trip through the stock codec, event decode (rotation + all button actions),
MTU sizing, drop detection, coexistence with stock **and** raw-accel traffic, reassembly, and the CLI
verb. `crown` is **not** an auto-discovered command-group module, so the `command-audit` opcode lock
is unaffected.

---

## 8. Home Assistant integration

The wire protocol ends at the notify char; how the crown reaches HA to dim a light — a spare
ESP32-C3 running ESPHome as a `ble_client`, room-tying via the existing iTag presence receivers, and
the concrete ESPHome yaml + HA automation — is specified in [`cfw-crown-ha.md`](cfw-crown-ha.md).

---

## 9. Open items (resolve on hardware, once custom FW exists)

1. **Everything is unverified on the wire** — no firmware emits `0xA1` yet. Opcode, header layout,
   button-action mapping, and coalescing behaviour are the *proposed* contract; confirm against the
   first real capture and reconcile this doc.
2. **Crown-button ownership** (GPIO key vs PMU on/off key) and the encoder GPIO pair — a
   teardown/NOR-dump question (#31/#17), not a protocol question.
3. **Detents-per-rev** and whether the encoder emits 1 or more notifies per physical click — firmware
   choice; the protocol just reports the net delta + the ack's `detents_per_rev`.

## Sources
- Framing / envelope / CRC: `docs/protocol-spec.md` §1, `starmax_client/framing.py`, `crc.py`.
- Twin protocol + framing rationale (option B, CRC-bearing binary batch): `docs/cfw-rawaccel-protocol.md`.
- Crown hook point: Actions SDK `zephyr/drivers/input/knobencoder_acts.c`,
  `framework/system/input/input_knob.c`, `framework/system/include/input_manager_type.h`,
  `zephyr/include/drivers/input/input_dev.h`, board `ats3085s_dev_watch_ext_nor/board.h`.
- BLE notify / advertising API: `framework/bluetooth/bt_manager/bt_manager_ble.c`
  (`bt_manager_ble_send_data`, `bt_manager_ble_adv_start`), `bt_manager_super_service.c` (GATT template).
- Board deltas / crown gate: `docs/cfw-board-port.md` §1.5, §3.
