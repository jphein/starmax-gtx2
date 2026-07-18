# Custom-Firmware Raw-Accel-over-BLE Protocol (issue #31, option C)

**Date:** 2026-07-14 · **Status:** SPEC + client consumer (firmware not yet
built). **Lane:** standalone-client (any source OK for the client).

This defines the BLE protocol a **custom** GTX2 firmware would expose to stream the discrete **ST
LIS2DH12** accelerometer's raw XYZ, and it ships a complete, test-green client consumer
(`starmax_client/rawaccel.py` + the `raw-accel` CLI verb). The firmware half does **not exist** —
stock GTX2 exposes *no* raw-accel path at all (no realtime receiver, no data model, nothing to
piggyback — `docs/custom-firmware-poc.md` Part 3). The feature needs an SDK rebuild that adds a
LIS2DH12 sampler and this GATT-level protocol. This document is the contract between that future
firmware and the client; the client is validated against **synthetic** frames built to this spec
(no biometric/PII data — accelerometer counts only).

Framing primitives (envelope, LEN/CRC rules, 0xC3 fragmentation, CRC-16/CCITT-FALSE) are defined
in [`protocol-spec.md`](protocol-spec.md) §1 and implemented in `starmax_client/framing.py`. This
spec adds one opcode and two frame shapes on top of that existing plane.

---

## 1. Mechanism decision — three options, one recommendation

The question is how the firmware delivers a batched raw-accel stream to the app. Three candidate
mechanisms were considered:

| # | Mechanism | Pros | Cons | Verdict |
|---|-----------|------|------|---------|
| A | **New notify characteristic** under service 0x0FF0 (e.g. char `0x0003`) | isolates accel from the command pipe; independent CCCD | new attribute-table entry + CCCD + handle in firmware; the client must discover/subscribe a 2nd characteristic and run a 2nd reassembler; more firmware surface | rejected |
| B | **New C1 opcode + notify frame** on the existing write/notify chars | rides the plane the client already reassembles; zero new GATT objects; smallest firmware delta (one opcode in the existing send path) | shares airtime with stock C1 traffic (a non-issue at these rates — §6) | **RECOMMENDED** |
| C | **D-plane bulk stream** (D1/D2/D4, like dial/firmware push) | proven high-throughput path | bulk plane is a *file transfer* (announce→chunks→complete w/ whole-file CRC), not a live stream; no per-sample timing; wrong semantics for realtime | rejected |

**Recommendation: option B — a new C1 opcode `0xA0` with a control sub-frame (flag=0) and a data
sub-frame (flag=1).** Rationale:

- **Rides the existing plane with zero framing changes.** The data frame is a *normal watch→app
  CRC-bearing C1 frame* (see §4) — `framing.parse_frame` already decodes and CRC-verifies it, and
  `Reassembler` already interleaves it with stock traffic. No new characteristic, no new codec.
- **Smallest firmware delta.** The rebuilt SDK adds one opcode handler and reuses whatever routine
  it already uses to emit watch→app notifications (same SOF/seq/dir/CRC machinery as every other
  reply). A LIS2DH12 sampler work-item fills a batch buffer; the send path is unchanged.
- **MTU-247-friendly.** Each batch is sized to fit **one** ATT PDU (§4.2) → no fragmentation, no
  multi-PDU reassembly to get wrong, lowest latency.

---

## 2. Transport & opcode

- **Service / chars:** the existing custom command service **`0x0FF0`** — app→watch **write char
  `0x0001`** (ATT 0x0026), watch→app **notify char `0x0002`** (ATT 0x0028). No new GATT objects.
- **Opcode `0xA0` = `RAW_ACCEL`.** Chosen to be collision-free with every namespace in play:
  - stock GTX2 wire opcodes are `0x01–0x22` (`protocol-spec.md` §2),
  - `0xA0` sits clear above the stock range ("A" ≈ Accel), unmistakably a custom-firmware extension.
- **`flag` byte selects the sub-frame** (the envelope's flag is per-opcode, `protocol-spec.md` §1):
  - `flag = 0x00` → **CONTROL** (protobuf; app→watch enable/disable, watch→app ack).
  - `flag = 0x01` → **DATA** (binary sample batch; watch→app only).
- **MTU:** 247 negotiated on-device (`transport.py` `_acquire_att_mtu`), giving a **244-byte** ATT
  payload per PDU (247 − 3 ATT header).

---

## 3. Control plane — `0xA0` flag=0 (protobuf)

Standard C1 command/reply, identical framing to every other protobuf opcode: app→watch requests
carry no CRC (`LEN = total`); the watch→app ack is a CRC-bearing protobuf reply (`LEN = total − 2`).

### 3.1 Enable / disable (app→watch)

| field | type | meaning |
|-------|------|---------|
| f1 | varint | **command**: `1` = START (enable), `0` = STOP (disable) |
| f2 | varint | **rate_hz** — requested sample rate; one of `{25, 50, 100, 200}` |
| f3 | varint | **range_g** — full-scale ±g; one of `{2, 4, 8, 16}` |
| f4 | varint | **res_bits** — output resolution / LIS2DH12 mode; one of `{8, 10, 12}` (default 12 = high-res) |

STOP carries `f1=0` only. The watch **clamps** an unsupported rate/range/res to the nearest it
supports and reports the **actual** values in the ack and in every data-frame header — the client
must trust the header, not the request.

Byte-exact enable (rate 50, range 8, res 12, seq 0x05):
`c1 05 01 01 00 a0 13 00 00 00 00  08 01 10 32 18 08 20 0c`
(payload `08 01 10 32 18 08 20 0c` = `f1=1, f2=50, f3=8, f4=12`). Locked by
`tests/test_rawaccel.py::test_enable_frame_byte_exact`.

### 3.2 Ack (watch→app, CRC-bearing protobuf)

| field | type | meaning |
|-------|------|---------|
| f1 | varint | **status**: `0`=ok/streaming, `1`=unsupported_rate, `2`=unsupported_range, `3`=busy, `4`=unsupported_res |
| f2 | varint | **rate_hz** — actual rate applied |
| f3 | varint | **range_g** — actual range applied |
| f4 | varint | **res_bits** — actual resolution applied |
| f5 | varint | **base_ts_ms** — device-monotonic ms at stream start (anchor; optional) |

---

## 4. Data plane — `0xA0` flag=1 (binary batch, watch→app)

**The key framing choice:** the data frame is a **normal watch→app CRC-bearing C1 frame** whose
payload happens to be binary rather than protobuf. Because `0xA0 flag=1` is **not** the `0x0e
flag=1` binary-record special case (`protocol-spec.md` §1.1), `framing.parse_frame` treats it as
the ordinary watch→app protobuf *class*: `LEN = total − 2`, a CRC-16/CCITT-FALSE trailer over
`frame[0:LEN]`, and `payload = frame[11:LEN]`. **The framing layer never assumes the payload is
protobuf** — it just slices bytes and verifies the CRC — so this rides the existing plane with
**zero** changes to `framing.py`. The CRC gives end-to-end integrity across reassembly (belt-and-
braces over BLE's per-PDU link CRC) and costs the firmware one cheap CRC loop it already has.

> Alternative (documented, not chosen): make the data frame a *no-CRC binary record* like `0x0e
> flag=1`. Saves 2 bytes/frame + the CRC, but requires extending `framing._is_binary_record` to
> recognise `(0xA0, 1)`. Rejected: the 2 bytes are negligible and the CRC-bearing form needs **no
> client core change** and adds integrity. Keep the CRC.

### 4.1 Payload layout (little-endian)

12-byte header + N × 6-byte sample. All multi-byte fields little-endian.

```
off 0     u8    version    = 0x01           payload-format version (lets the header evolve)
off 1     u8    cfg                          bits[1:0]=range_code, bits[3:2]=res_code, [7:4]=0
off 2     u8    rate_code                    0=25Hz 1=50Hz 2=100Hz 3=200Hz  (actual rate)
off 3     u8    count (N)                     samples in this frame (1..MAX, see §4.2)
off 4-5   u16   frame_seq                     per-stream frame counter, wraps 0x10000 (drop detect)
off 6-9   u32   base_ts_ms                    device-monotonic ms timestamp of sample[0]
off 10-11 u16   reserved   = 0x0000           alignment / future (temp, event flags)
off 12..  N×6   samples                       each: [x:i16][y:i16][z:i16]  (LE)
```

- `range_code`: `0=±2g 1=±4g 2=±8g 3=±16g`. `res_code`: `0=8-bit 1=10-bit 2=12-bit`.
- **Samples are raw, LEFT-justified 16-bit** LIS2DH12 register pairs (OUT_X_L/H … as read). The
  useful bits are the top 8/10/12 per the operating mode (§5). The consumer keeps both the raw
  i16 and a g-converted float.
- **Drop detection:** the consumer tracks `frame_seq`; a gap = dropped frames
  (`rawaccel.detect_drops`). This is independent of the C1 envelope `seq`.
- **Per-sample time:** `t(k) = base_ts_ms + k · (1000 / rate_hz)` ms. Each frame is self-anchoring
  (its own `base_ts_ms`), so a dropped frame does not desync timing.

### 4.2 Batch sizing (MTU-247, single-PDU)

Per-PDU budget for samples: `mtu_payload − C1_header(11) − data_header(12) − CRC(2)`.

| MTU | mtu_payload | budget | **max samples (÷6)** |
|-----|-------------|--------|----------------------|
| 247 | 244 | 219 | **36** |
| 185 | 182 | 157 | 26 |
| 23 (floor) | 20 | −5 | 0 → must fragment/shrink |

The firmware **should size each batch ≤ the single-PDU max** (36 at MTU 247) so no `0xC3`
fragmentation is needed — simplest firmware, lowest latency, and it keeps a stock push from
interleaving mid-fragment (§6). At MTU 247 / 50 Hz that is one frame every ~720 ms (≈1.4 fps);
even 200 Hz is ≈5.6 fps. `rawaccel.max_samples_per_frame(mtu_payload)` computes this; the generic
`Reassembler` still handles a fragmented batch if a firmware ever exceeds one PDU
(`test_reassembler_fragmented_large_batch`).

---

## 5. LIS2DH12 g-conversion

The wire carries the raw left-justified 16-bit word. To convert to g: **right-shift** to the
useful N-bit value (per operating mode), then multiply by the datasheet **mg/digit** for
(res, range). Sensitivity table (ST LIS2DH12 datasheet DocID026799):

| mode (res) | shift | ±2 g | ±4 g | ±8 g | ±16 g |
|------------|-------|------|------|------|-------|
| high-resolution (12-bit) | 4 | 1 | 2 | 4 | 12 | mg/digit |
| normal (10-bit) | 6 | 4 | 8 | 16 | 48 | mg/digit |
| low-power (8-bit) | 8 | 16 | 32 | 64 | 192 | mg/digit |

`g = (raw_i16 >> shift) · mg_per_digit / 1000`. Python's `>>` on a signed int is arithmetic, so
sign is preserved. Example: high-res ±2 g, `raw=16000` → `16000>>4 = 1000` digits × 1 mg = **1.000
g**; `raw=32767` (near full scale) → **2.047 g**. Implemented in `rawaccel.to_g`; locked against
the datasheet in `test_to_g_matches_datasheet`.

---

## 6. Coexistence with stock C1 traffic

The raw-accel stream shares the notify pipe with stock replies/pushes (bind `0x01`, device-state
`0x05`, status ping `0x10`, health `0x0e`, …). This is safe:

- **Routed by opcode.** The client dispatches inbound frames by `opcode`; a `0xA0 flag=1` listener
  gets data batches, everything else is untouched (`test_coexists_with_stock_c1_traffic`).
- **Self-contained frames.** Because data frames are single-PDU (§4.2), a stock push arriving
  between them never lands mid-fragment. (If a firmware *did* fragment a batch, an interleaved
  stock frame mid-`0xC3` would corrupt reassembly — another reason to keep batches single-PDU.)
- **Airtime.** ≤5.6 fps even at 200 Hz leaves ample room for stock traffic. The firmware must
  still service the C1 command queue between batches (don't starve bind/acks).
- **Envelope seq.** Data frames use the normal watch→app `seq | 0x80`; the app never needs to ack
  them (notifications). The independent `frame_seq` in the payload is what tracks drops.

---

## 7. Firmware implementation notes

1. **Sampler:** a periodic work-item reads the LIS2DH12 (OUT_X..OUT_Z, or drain its 32-level FIFO)
   at the configured ODR, appends `[x][y][z]` i16 to a batch buffer.
2. **Emit:** when the batch reaches the single-PDU max (or a flush timer fires), build a `0xA0
   flag=1` frame = header + samples, compute CRC-16/CCITT-FALSE over `frame[0:LEN]`, append it LE,
   notify on char `0x0002`. Reuse the SDK's existing watch→app notify path.
3. **Control:** on `0xA0 flag=0` START, program the LIS2DH12 ODR/FS/mode from f2/f3/f4 (clamp to
   supported), set `base_ts_ms`, reply with the ack (§3.2), begin sampling. On STOP, halt the
   work-item and reply `status=0`.
4. **Open hardware unknown (from Part 3):** whether the LIS2DH12 hangs off the ATS3085 (app-only
   mod) or the WTM2101 co-processor (needs its firmware / the ATS3085↔WTM2101 IPC). Resolved only
   by the teardown/NOR-dump (#17). Does **not** affect this wire protocol — only where the sampler
   code lives.

---

## 8. Client consumer (this deliverable)

`starmax_client/rawaccel.py` — transport-independent, offline-testable:

- **Builders:** `build_rawaccel_enable(rate_hz, range_g, res_bits, seq)`,
  `build_rawaccel_disable(seq)` → app→watch control frames (validate against the supported sets).
- **Parsers:** `parse_rawaccel_ack(payload)` → dict; `parse_rawaccel_frame(payload)` →
  `RawAccelBatch` (header fields + `AccelSample`s with raw i16 **and** g). Raises `RawAccelError`
  on bad version / truncation / bad cfg.
- **Helpers:** `to_g`, `detect_drops`, `max_samples_per_frame`, `RawAccelBatch.timestamps_ms()`.
- **Firmware-emulation encoders** (for tests + golden vectors): `build_rawaccel_data_frame(...)`,
  `build_rawaccel_ack_frame(...)` produce byte-exact watch→app wire frames.
- **CLI verb `raw-accel`** (core verb, mirrors `monitor`): `--decode <hex>` (offline decode of a
  wire frame or bare payload), `--dry-run` (print the enable frame), live streaming behind
  `--force` (refused otherwise — opcode 0xA0 is custom-firmware-only and unverified).

Tests: `tests/test_rawaccel.py` — control byte-layout + validation, data-frame decode round-trip
through the stock codec, g-conversion vs datasheet, MTU sizing, drop detection, coexistence/
reassembly, and the CLI verb. All synthetic (PII-clean). `raw-accel` is **not** an auto-discovered
command-group module, so the `command-audit` opcode lock (`test_command_audit.py`) is unaffected.

---

## 9. Gadgetbridge clean-room consumer plan (do NOT implement yet)

GB support for raw-accel is a **future** clean-room task, gated on a **real capture** that cannot
exist until the custom firmware is flashed and streaming. Do **not** implement it now — there is
nothing to clean-room against. The plan, for when a capture exists:

1. **Capture first.** With custom firmware running, capture an enable→stream→disable exchange
   (btsnoop) so any future coordinator is derived from **observed bytes**, not from this doc.
2. **Coordinator shape:**
   - Recognise `0xA0` in the C1 frame handler; route `flag=0` acks and `flag=1` data batches.
   - A `RawAccel` decoder porting §4.1 (header + i16 samples) and §5 (g-conversion) —
     re-derived from the capture, byte-faithful, with its own confidence tags.
   - Surface samples via the host's activity/sensor API (or a debug log), honouring the
     enable/disable lifecycle and battery/rate policy.
3. **Provenance wall.** The decoder is written **only** from the capture + public LIS2DH12
   datasheet — never copied from `rawaccel.py`. This spec may guide *what to look for*, but the
   values must be confirmed on the wire.

---

## 10. Open items (resolve on hardware, once custom FW exists)

1. **Everything is unverified on the wire** — no firmware emits `0xA0` yet. Opcode choice, header
   layout, and rate/range/res clamping behaviour are the *proposed* contract; confirm against the
   first real capture and reconcile this doc.
2. **LIS2DH12 ownership** (ATS3085 vs WTM2101) — §7.4; a NOR-dump / teardown (#17) question, not a
   protocol question.
3. **FIFO vs polled sampling** and the exact ODR set the sensor is configured for — firmware
   choice; the protocol just reports the actual `rate_code`.
4. **Higher rates / bigger ranges** (e.g. 400 Hz) — the `rate_code`/`range_code` tables have spare
   values; bump `DATA_VERSION` if the header layout ever changes.

## Sources
- Framing / envelope / CRC: `docs/protocol-spec.md` §1, `starmax_client/framing.py`, `crc.py`.
- Feasibility (no stock raw-accel; SDK-rebuild path): `docs/custom-firmware-poc.md` Part 3.
- Opcode namespaces: `docs/protocol-spec.md` §2.
- Sensor: ST **LIS2DH12** datasheet (DocID026799) — OUT regs 0x28–0x2D, 32-level FIFO,
  sensitivity table (§5).
