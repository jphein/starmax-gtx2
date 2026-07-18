# Decode notes — GTX2 BLE protocol (running findings)

Source captures in `captures/`. Transport confirmed (verified on-device + against the capture's GATT discovery): a **custom GATT command service `0x0FF0`**, **not** Nordic UART. The Runmefit SDK/APK references NUS-style UUIDs, but the GTX2 firmware does not expose Nordic UART — it exposes this custom service instead.
- Service `0x0FF0` = `00000ff0-0000-1000-8000-00805f9b34fb` (primary; ATT handles 0x0024–0x0029)
- Write (app→watch): char `0x0001` = `00000001-0000-1000-8000-00805f9b34fb` (props 0x0c Write / WriteWithoutResponse) at ATT value handle **0x0026** (ATT PDU 0x52 Write Command)
- Notify (watch→app): char `0x0002` = `00000002-0000-1000-8000-00805f9b34fb` (props 0x10 Notify) at ATT value handle **0x0028** (ATT PDU 0x1b Handle Value Notification; CCCD 0x0029)
- The `0xD2` bulk/DFU plane (firmware + dial transfers) rides the **same `0x0FF0` write char `0x0001` (ATT handle 0x0026)** as the C1 commands — verified in the capture (10k+ `0xD2` writes on 0x0026).
- A secondary custom service `0xFFD0` (handles 0x001b–0x0023, 128-bit-UUID chars) appears in GATT discovery but carried **no observed command or bulk traffic**; its purpose is **undetermined**.

## Framing — multi-channel, first byte = channel/frame-type discriminator
The first byte of each command frame is a channel/type id, NOT a fixed magic. Observed on the write char (0x0026) in one segment:
| first byte | count | nature |
|---|---|---|
| `0xC1` | 138 | **command channel — PLAINTEXT protobuf** (readable) |
| `0xD2` | 381 | **bulk-data channel — high entropy** (compressed or binary blob) |
| `0xD1` | 26 | (bulk-related; TBD) |
| `0xC3` | 5 | continuation / secondary control (TBD) |
| `0xD4` | 1 | (TBD) |

### Command channel (0xC1) — CONFIRMED envelope
```
off 0   : 0xC1  SOF / channel id   (0xC3 = continuation fragment of a multi-PDU frame)
off 1   : seq   message sequence; high bit 0x80 SET on watch→app frames
off 2   : dir   0x01 = app→watch (command), 0x00 = watch→app (response/push)
off 3   : 0x01  constant (protocol version?)
off 4   : flag  0x00/0x01 (sub-type/bank; varies)
off 5   : OPCODE  command id — response echoes the request's opcode
off 6   : LEN   frame length excluding the 2-byte CRC (frames >255B fragment via 0xC3)
off 7-10: 00 00 00 00  (reserved / 32-bit field, usually zero)
off 11+ : protobuf payload
last 2  : CRC-16, little-endian
```
**CRC = CRC-16/CCITT-FALSE** (poly 0x1021, init 0xFFFF, refin/refout false, xorout 0x0000), over the
**entire frame from the 0xC1 SOF up to the CRC**, stored **little-endian**. Verified against 5 frames
(incl. two identical-except-seq frames with differing CRCs). Exact algo the coordinator must use.

> ⚠️ **`docs/protocol-spec.md` (verified decode) supersedes this file for opcodes/framing.** Two corrections:
> **(a)** LEN is a **16-bit little-endian** field at off 6–7 (NOT 1-byte + 4 reserved; reserved is off 8–10) — needed for frames >255 B (e.g. the 377-B weather frame `LEN=79 01`).
> **(b)** CRC is present **only on watch→app** frames (LEN = total−2); **app→watch commands carry NO CRC** (LEN = total). The coordinator must NOT append a CRC to outgoing frames.

### Opcodes identified so far (byte 5) — request/response echo confirmed
| opcode | meaning (inferred) | evidence |
|---|---|---|
| 0x01 | device info / init | response carries module id ASCII `UC62228CI`, a MAC `<watch-mac>`, capability protobuf |
| 0x02 | **set time** | protobuf year=2026 month=7 day=11 (`08 ea0f 10 07 18 0b …`) |
| 0x05 | device/state query | response has MAC + small struct |
| 0x16 | **watch-face / resource file list** | response lists `.bin` names (`YHZN_1021@LC.bin`, `CW07_*.bin`) |
| 0x22 | setting get/set | short protobuf `08 01 10 f4 01` (=244) |
| 0x0e | health/settings block | sub-reads `08 00/01/02 10 00 18 00` |

| 0x11 | **notification (app/email, title+body)** | text field (protobuf field 5 = 0x2a): e.g. `<notification title>` |
| 0x12 | **weather** (verified) | city + current temp + condition + 24h hourly + 3-day daily + pressure×100. NOTE: the `<city>` string is the *weather location*, not a caller-ID (earlier call reading was wrong). |
| 0x13 | **notification (SMS / messages)** | full SMS text (e.g. `<notification body>`) and `<notification summary>` summaries |

Payload is **protobuf**. Notification text is UTF-8 in protobuf field 5 (`0x2a`). The watch shows only a
count by its own UI choice — the protocol **does** carry full title/body, so the coordinator can send content.
Full opcode map across all captures = next step (see `docs/protocol-spec.md` for the capture-decoded map).

> ⚠️ **Privacy:** captures contain real personal data (notification text, account, watch MAC). Sanitize before any public issue/PR.

### Bulk channel (0xD2/0xD1) — high entropy
- 236-byte writes in bursts. High entropy ⇒ compressed or opaque binary.
- Appeared heavily during **weather + health-sync (wave 2)**. Most likely **AGPS/EPO almanac** for the GPS receiver, and/or watch-face/resource data, and/or a compressed history blob.
- NOT the command channel — commands stay plaintext alongside it. So this is a separate bulk-transfer sub-protocol, probably NOT needed for the MVP (notifications/time/HR/steps/sleep).

## Encryption assessment
**Command channel is plaintext protobuf — no comms encryption on control traffic.** Only the bulk (0xD2) channel is opaque, and that's most likely compression/binary payload, not a cipher on the protocol. Big de-risk for the coordinator.

## Open items for decode phase
- Exact 0xC1 envelope field offsets + CRC algorithm (brute over pcap: CRC16 variants).
- What the 0xD2 bulk channel carries (AGPS vs resource vs history) — decompress-test (zlib/gzip magic) and correlate with the wave-2 weather/sync timestamps.
- Notification command: adb shell test notification produced only a **count** on the watch and its marker text is absent from the trace → likely count-only for system apps. **Re-test with a real app notification** to capture a content-bearing notification frame.
- Map opcodes: HR trigger, SpO2 trigger, set-time, weather push, alarm set, activity/sleep history read (via `0x0e` flag=1 per-category sync — see protocol-spec §6).

## Firmware / OTA (captured + image recovered)
- **OTA server (public HTTPS, no auth):** `www.runmefit.cn/storage/…`, resources `download.runmefitserver.com/dial/gtx2/gtx2/…`. URLs recovered from bugreport **logcat** (no MITM needed).
- **Flashed image:** `cb05_yhzn01_v1.0.3_20241218_02.ota`, 2,148,064 B, sha256 `5dac413b0e8e68581d5de1d6916f022727ef9a96bacc4003e1751f86c2967cc0`. Saved to `firmware/`.
- **Image format:** magic `FA EE EB DE` + TOC; contains `firmware/zephyr.bin`. Strings = **Zephyr RTOS** → GTX2 runs Zephyr+LVGL on the ATS3085. Unpackable with the community ATS3085S unpacker.
- **DFU transport = `0xD2` bulk channel.** Frame = `D2 | counter | <234 bytes raw image>`; chunk N = image bytes `[N·234 : N·234+234]`. Verified: captured payloads land at image offsets 966·234, 3966·234, 7966·234 exactly. NOT encrypted. Same channel also carries watch-face `.bin` blobs.
- Capture `fw-complete-*` has ~10,214 D2 frames (~full 2.1 MB transfer). **DFU is initiated on the BULK plane, NOT C1** (corrected): `D3` resume-query → `D1` announce (filename `res.ota` + size) → `D2` 234-B chunks (counter resets per file), acked every 15 chunks with a running **CRC-16/XMODEM** cumulative offset → `D4` end = whole-file CRC-16/XMODEM (0xAA0D). Image CRC-32 (header+0x04) = 0xE6E6BB23, verified. See `docs/firmware-dfu.md` (authoritative). **Out of MVP scope** but a complete dataset if we add OTA.

## Segments captured (cumulative ring-buffer snapshots)
- `pairing-*` — first bind + auth handshake (~2151 pkt)
- `features1-*` — + notification(count-only) + HR + SpO2 (~3730 pkt)
- `features2-*` — + weather + alarm + health sync (~4220 pkt)
- (pending) workout, real-notification test
