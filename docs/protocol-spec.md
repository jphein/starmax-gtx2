# Starmax GTX2 (Runmefit) BLE Protocol — Implementation Spec

Complete, capture-verified spec for the GTX2 command protocol. Framing basics are in
`docs/decode-notes.md`; this document supersedes/refines it where the raw packets disagree
(see **§11 Corrections to decode-notes**). Every schema below is decoded from real frames in
captured traffic (tshark 4.2.2 + a raw-protobuf parser).
All CRCs and lengths were re-derived from the bytes, not taken on faith.

Transport (CORRECTED — verified on-device + against the capture's GATT discovery): a **custom GATT
command service `0x0FF0`** (`00000ff0-0000-1000-8000-00805f9b34fb`; primary, ATT handles
0x0024–0x0029), **not** Nordic UART. **app→watch = write char `0x0001` (`00000001-…`, props 0x0c)
at ATT handle `0x0026` (Write Command)**, **watch→app = notify char `0x0002` (`00000002-…`, props
0x10) at ATT handle `0x0028` (Handle Value Notification; CCCD 0x0029)**. The `0xD2` bulk/DFU plane (firmware + dial transfers) rides the **same `0x0FF0` write char `0x0001` (ATT handle `0x0026`)** as C1 commands — verified in the capture (10k+ `0xD2` writes on 0x0026). A secondary custom service `0xFFD0` (handles 0x001b–0x0023) appears in GATT discovery but carried **no observed command or bulk traffic** — purpose undetermined. The Runmefit SDK/APK
references NUS-style UUIDs, but the GTX2 firmware exposes this custom `0x0FF0` service instead —
verified on-device.

---

## 1. Frame envelope (CORRECTED)

```
off  0    : 0xC1   start-of-frame  (0xC3 = continuation fragment of a multi-PDU frame)
off  1    : seq    sequence counter; watch→app frames OR-in 0x80
off  2    : dir    0x01 = app→watch (command), 0x00 = watch→app (reply/push)
off  3    : 0x01   constant (protocol version)
off  4    : flag   sub-type/bank byte (0x00 or 0x01) — meaning is per-opcode
off  5    : OPCODE
off  6-7  : LEN    **16-bit LITTLE-ENDIAN** length  ← (decode-notes had this as 1 byte)
off  8-10 : 00 00 00   three reserved bytes (always zero observed)
off  11.. : PAYLOAD  (protobuf; or a binary record for opcode 0x0e flag=1)
[tail]    : CRC-16   2 bytes, little-endian — **watch→app protobuf frames only**
```

### 1.1 LEN + CRC rules are DIRECTION- and CLASS-dependent (the important correction)

| Frame class | LEN means | Trailing CRC? | payload slice |
|---|---|---|---|
| **watch→app, protobuf** (all opcodes incl. 0x0e flag=0, 0x13 flag=1) | `total − 2` | **yes**, CRC over bytes `[0 .. LEN)` | `buf[11:LEN]` |
| **app→watch, command** | `total` (whole frame) | **no** | `buf[11:LEN]` |
| **0x0e flag=1 binary health records** (both directions) | `total` | **no** | `buf[11:LEN]` |
| bind `0x01` command (app→watch) | `11` (header only) | 2 trailing `00` bytes | empty |

Verified: 100% of watch→app protobuf frames across all five command captures pass CRC; the
348 `0x0e`-flag1 records carry no CRC (frame length == LEN, no room for a trailer). The
2-byte-LE LEN is proven by the 377-byte weather command (`LEN = 79 01 = 0x0179 = 377`), which
reassembles byte-exact.

### 1.2 CRC-16/CCITT-FALSE (reference, verified)

poly `0x1021`, init `0xFFFF`, no reflect, xorout `0x0000`, stored **little-endian**, computed
over `frame[0 : LEN]` (from the `0xC1` SOF up to but not including the 2 CRC bytes).

```python
def crc16(data: bytes) -> int:          # returns value; store as struct.pack("<H", crc16(...))
    crc = 0xFFFF
    for b in data:
        crc ^= b << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc
```
Check: `c182000100221000000000080110f401` → CRC `0x7c5e` (stored `5e 7c`). ✓

### 1.3 Fragmentation (0xC3)

Frames longer than the ATT MTU are split at the **C1 layer** (not L2CAP): the first PDU is a
normal `0xC1` frame; each continuation is `0xC3 | seq | <raw bytes>` with the **same seq**.
Reassemble by appending `pdu[2:]` of each `0xC3` until the accumulated length reaches the
declared total (LEN for writes / LEN+2 for reads), then decode. Example: the 0x16 dial-list
reply (`YHZN_1021@LC.bin`) spans one C1 + one C3.

### 1.4 Wire opcode namespace

The wire uses a small opcode set (`0x01`–`0x22`) and multiplexes features inside protobuf and via
the `flag` byte / the `0x0e` category index. The table below maps each wire opcode → function by
decoded behaviour from the captures, not by numeric value.

---

## 2. Master opcode table (wire opcodes actually observed)

| op | flag | dir | function (observed behaviour) | purpose | one real example |
|----|----|-----|------|---------|-------------------|
| `0x01` | 0 | W+N | bind / read device descriptor | bind + full device descriptor | W:`(empty)` → N: `08 d91f … 18"UC6228CI" …` |
| `0x02` | 0 | W+N | set date/time | set date/time (+epoch, tz) | W:`0802121808ea0f1007180b2000281730…` |
| `0x03` | 0 | W+N | profile + goals + notif-toggles bundle | profile, goals, notif toggles | see §3.9 |
| `0x04` | 0 | W+N | feature / notification boolean bitmap | boolean toggle bitmap | W:`080110 02` → N:`0801 1002 1801…` |
| `0x05` | 0 | W+N | read device state | MAC + fw/version struct | N:`080110021a12aabbccddeeffe80707001800130031002700` |
| `0x07` | 0 | W+N | alarm clock get/set | **alarm clock get/set** | see §9.3 |
| `0x0e` | 0 | W+N | `HealthSwitch`/`RealTimeSwitch` config | health-detection master switches | see §5 |
| `0x0e` | 1 | W+N | **history/health data sync** | binary health records by category | see §6 |
| `0x10` | 0 | N | (watch→app push/control) | status ping **and** watch-originated controls: **media** (`f1=1`) & **find-phone** (`f1=2`); status `f1=3`/`f1=4` | N:`0803 1009`; media `08 01 10 01`; see §9.4–9.5 |
| `0x11` | 0 | W+N | notification — **detailed** (title/body) | push rich notification | W:`08011002180620642800 32 1a "<notification title>"` |
| `0x12` | 0 | W+N | weather push | push weather (current+hourly+daily) | see §3.7 |
| `0x13` | 1 | W+N | notification — **summary/count** | push summary line | W:`08021000180220222a0e "<summary text>"` |
| `0x16` | 0 | W+N | watch-face / resource list | installed watch-face/resource list | see §3.10 |
| `0x18` | 0 | W+N | find-device / ring watch | **find-device / ring watch** | W:`08 02 10 01 18 01`=buzz on / `18 00`=off |
| `0x22` | 0 | W+N | generic setting/feature-flag query | read a setting value | W:`0801` → N:`080110 f401` (`f2=244`) |

Still not observed on the C1 channel (see §10): **workout GPS track**, per-app
**notification-switch write**, contacts, DND, sedentary/drink reminders. Now resolved and folded
in: **find-device** (0x18), **alarm** (0x07), **media control / find-phone** (0x10), **dial
install** (bulk plane) — see §9. Handled OFF the command channel by classic-BT profiles (Android
does them for free): **incoming call** = HFP (§8), **camera shutter** = HID (§9.6). The
`0xD1/0xD2/0xD4` bulk plane (dials, AGPS, firmware) is decoded in §9.1.

---

## 3. Per-feature request/response (decoded)

Notation: `fN` = protobuf field N. Values are straight from the capture.

### 3.1 Bind / device descriptor — `0x01`
- **Request** (app→watch): empty payload. Frame `c1 01 01 01 00 01 0b 00 00 00 00 00 00`
  (LEN=11 header-only + two `00` pad bytes — the one command frame that carries a trailer).
- **Response** (watch→app), 130 B protobuf, CRC ok. Decoded from
  `08d91f1001200130034a06aabbccddeeff…`:

| field | value | meaning |
|---|---|---|
| f1 | 4057 | protocol / build code |
| f2,f4,f5 | 1 | capability flags |
| f6 | 3 | (device class?) |
| f9 | `aabbccddeeff` | **MAC** — redacted placeholder (also f10, identical) |
| f11 | `{f1=3880, f3=56, f10=1}` | **firmware/version block** (fw 3880) |
| f13 | 1 | |
| f15 | 108 | |
| f16 | 1 | |
| f17 | `{f1..f11 = 1}` | **capability bitmap** (10 features enabled) |
| f18 | `"UC6228CI"` | **chipset/module id** (decode-notes' "UC62228CI" was a typo). ⚠ **NOT the application SoC** — this is the **Unicore UC6228 GNSS/GPS L1 receiver** (a peripheral); the app SoC is the Actions **ATS3085S4** (Cortex-M33). The vendor populated this field with the GNSS part, not the main controller. See `docs/hardware-teardown-guide.md §1.2`. |
| f19 | `{f1=466, f2=466, f3=233}` | **screen: width 466 × height 466**, f3=233 (radius/DPI) → round AMOLED |
| f21 | 67 | |
| f22 | 2 | |
| f23 | `{f1=…, f2=…, f3=…, f4=…}` (redacted) | serial / build fingerprint |

Note f11.f1 = 3880 in `pairing`, 3829 in `workout` (build/uptime counter that increments).

### 3.2 Set time — `0x02`  (app→watch; watch acks empty)
Payload `f1=2, f2 = { time }` where the time message is:

| field | example | meaning |
|---|---|---|
| f1 | 2026 | **year** (plain int, not BCD) |
| f2 | 7 | month |
| f3 | 11 | day |
| f4 | 0 | hour (**local**) |
| f5 | 23 | minute |
| f6 | 42 | second |
| f7 | 5 | **weekday, Monday=0** (5 = Saturday; verified 2026-07-11 = Saturday) |
| f8 | 1783754622 | **Unix epoch seconds (UTC)** — decodes to 2026-07-11 07:23:42 UTC = 00:23 local |
| f9 | 1140 | timezone/DST descriptor — *UNRESOLVED units* (Pacific −7h was in effect) |

Real: `0802121808ea0f1007180b2000281730380540fedec7d20648f408`.

### 3.3 Device state — `0x05` (W+N)
- Request `f1=1, f2=0, f3=0`.
- Response `f1=1, f2=2, f3 = 18 raw bytes` = `aabbccddeeff` (MAC) + `e8 07 07 00 18 00 13 00
  31 00 27 00` (six 16-bit LE words: 2024,7,24,19,49,39 — a firmware build stamp; exact field
  split *UNRESOLVED*). A second empty `0x05` frame (seq+3) follows as an end/ack marker.

### 3.4 Notification — detailed — `0x11` (flag=0, app→watch)
`08 01 10 02 18 06 20 64 28 00 32 1a "<notification title>" 3a 00`

| field | value | meaning |
|---|---|---|
| f1 | 1 | (present flag) |
| f2 | 2 | app/channel id |
| f3 | 6 | notification category/icon |
| f4 | 100 | id/count |
| f5 | 0 | |
| f6 | `"<notification title>"` | **UTF-8 title/text** |
| f7 | (empty) | **body** (empty in sample) |

Confirmed: the protocol carries full UTF-8 content, so a coordinator can push real
title+body. (The watch's own UI may collapse system notifications to a count.)

### 3.5 Notification — summary/count — `0x13` (flag=1, app→watch)
`08 02 10 00 18 02 20 22 2a 0e "<summary text>" 32 00`
→ `f1=2, f2=0, f3=2, f4=34, f5="<summary text>" (UTF-8 text), f6=(empty)`.
Note the text field differs from 0x11: **0x13 puts text in f5, 0x11 in f6**. Other samples in
`notif-real`: short status strings.

### 3.6 (reserved)

### 3.7 Weather push — `0x12` (app→watch; no read counterpart)
366-byte protobuf, fragmented (C1+C3). Top level `f1=2, f2=1, f3 = { forecast }`:

| f3 field | example | meaning |
|---|---|---|
| f1 | 7 | month |
| f2 | 11 | day |
| f3 | 0 | hour |
| f4 | 23 | minute (== capture minute; tracks "now") |
| f5 | 6 | **current condition code** — drives the widget's condition icon. Icon-code map being swept on-device (2026-07-15); code 6 rendered cloudy/rainy, NOT "sunny" as first assumed. Full 1..N map is the sweep deliverable (#25). |
| f6 | 31 | current temp (°C) — **metadata slot, no observed UI display role** (still populated with current, harmless) |
| f7 | 22 | **UI "current" temp — the BIG number the widget shows.** Differential-calibrated 2026-07-15 (two pushes: the big number tracked f7, the range-low tracked f9). The capture's `22` was degenerate — current==min that day — so the old "(min-ish)" label was wrong. |
| f8 | 31 | **range HIGH (temp max)** |
| f9 | 22 | **range LOW (temp min)** — the widget's small "low" in the hi/lo range |
| f10 | `"<city>"` | **location name** |
| f11 ×24 | `{f1=temp/hi, f2=temp}` | **24-hour hourly forecast** (e.g. 31/21, 31/20, 32/20 …) |
| f14 | 44 | |
| f16/f17/f18 | 5/0/5 | |
| f19 ×3 | `{f1=hi, f2=lo, f3=cond}` | **3-day daily forecast** (33/31/22, 32/33/21, 32/35/23) |
| f20 | `{5,47,20,33}` | |
| f22 | 101500 | **air pressure ×100 → 1015.00 hPa** |
| f23 | 24 zero bytes | reserved/hourly-AQI placeholder |
| f24 ×N | `{day, hi, lo, ?, 7}` | extended daily rows |
| f25 ×N | `{5, minute, 20, 33}` | |

Unit/AQI/UV subfields present but not individually pinned → treat unlabeled subfields as
*UNRESOLVED*. City, current temp, condition code, hourly[24], daily[3], and pressure are solid.

### 3.8 Setting query — `0x22`
Request `f1=1`. Response `f1=1, f2=244` (later also `f4=1, f5=1`). A generic key/value settings
read; f2=244 is the returned value. Exact setting identity *UNRESOLVED*.

### 3.9 User profile + goals + notif-toggles bundle — `0x03` (W+N)
Set example (`features2`): `f1=2, f2={profile}, f3={notif toggles}, f4={goals}`.

- **profile** f2: `f1=<height cm>` · `f2=<weight, raw ~0.01 kg units>` · `f3=0` · `f4=<birth
  year>` · `f5=<sex: 1=male/0=female>` · `f6=1`. (Real captured bio values redacted; the two
  captures differed, confirming f2=weight and f4=birth-year are the editable fields.)
- **goals** f4: `f1=30, f2=12, f3=500, f4=8000` **step goal** (→10000 after edit) · `f5=5000`
  **distance goal (m)** · `f6=7, f7=1, f8=0`.
- **notif toggles** f3: ~12 booleans (per-app notification switches, all 1).

This bundle carries the user profile, sport goals, and notification toggles (all decoded above).

### 3.10 Watch-face / resource list — `0x16` (W+N)
Request `f1=0`. Response (fragmented) `f3=6, f4=12, f5=7, f6=1, f8=1`, then **repeated f10 file
entries** `{f1, f2, f3=size-bytes, f4=filename}`:

```
YHZN_1021@LC.bin  262144    CW06G_187_03.bin 229376   CW06G_187_04.bin 262144
CW07_6208_01.bin  196608    CW07_12607_01.bin 425984  CW07_M12603.bin   98304
num061109_10.bin  983040
```
Trailer: `f11=3145728` (total flash), `f12=2457600` (used), `f14="YHZN_1021@LC.bin"` (**active
dial**), `f15=4, f17=3, f18=12`.

---

## 4. Connect / bind handshake sequence (from `pairing-*`, in order)

The coordinator must replicate this exchange right after GATT connect (enable notifications on
`0x0028` first). Each line = one C1 frame; W=app→watch, N=watch→app.

| # | dir | op/flag | payload (req) → reply | purpose |
|---|-----|---------|------|---------|
| 1 | W | 0x01 | *(empty)* | **bind / hello** |
| 2 | N | 0x01 | device descriptor (MAC, fw 3880, `UC6228CI`, 466×466, caps) | bind reply |
| 3 | W | 0x22 | `f1=1` | read setting |
| 4 | N | 0x22 | `f1=1, f2=244` | setting reply |
| 5 | W | 0x05 | `f1=1,f2=0,f3=0` | read device state |
| 6 | N | 0x05 | MAC + build stamp | + empty 0x05 (seq 0x83) end-marker |
| 7 | W | 0x01 | *(empty)* | **re-read device info** (app does it twice) |
| 8 | W | 0x22 | `f1=1` | (pipelined) |
| 9 | N | 0x01 / 0x22 | descriptor / setting | replies |
|10 | W | 0x16 | `f1=0` | **read dial/resource list** |
|11 | N | 0x16 | file list (C1+C3) | reply |
|12 | W | 0x02 | year/…/epoch/tz | **SET TIME** |
|13 | N | 0x02 | *(empty)* | time ack |
|14 | W | 0x0e f0 | `f1=1` | read health switches |
|15 | N | 0x0e f0 | `f1=1,f2=1,…` 7 booleans | switch state |
|16 | W | 0x0e f0 | `f1=2, {08 cat 10 0}×7` (cats 0,1,2,3,4,5,7) | **write health switches** |
|17 | N | 0x0e f0 | `f1=2, f2=273` | ack |
|18 | W/N | 0x0e f1 | per-category data sync — see §6 | **history/health pull** |
|19 | N | 0x10 | `f1=3, f2=9` | watch ready/status push (interleaved) |

After bind the app loops §18 for categories `0,1,2,3,4,5,7`, each as a `read-data` then a
`read-status` request. Weather (0x12), notifications (0x11/0x13), and profile (0x03) are pushed
later as the user exercises them.

---

## 5. Health switches — `0x0e` flag=0 (protobuf)
- **Read**: W `f1=1` → N `f1=1, f2=1,f3=1,f4=1,f5=1,f6=1,f7=1,f9=1` (per-category enabled bits).
- **Write**: W `f1=2, f2=[ {f1=cat, f2=on} … ]` for categories `0,1,2,3,4,5,7`
  (`0802 1204080010 00 …`) → N `f1=2, f2=273`.
The category→metric numbering is now fully resolved: **0=HR, 1=stress, 2=SpO2, 3=sleep,
4=workout, 5=activity, 7=HRV** — see [`health-metrics.md`](health-metrics.md) for the authoritative
map + per-metric decode. (Earlier drafts mislabeled cat 5 as "sleep" and cat 7 as "BP/temp".)

---

## 6. History / health-data sync — `0x0e` flag=1 (BINARY, no CRC)

The workhorse. **Request is protobuf; response is a fixed binary record** (my protobuf parser
deliberately rejects it — field-0 garbage is the tell).

### 6.1 Request (app→watch), protobuf
`f1 = sub-op` (**0 = read data record, 1 = read config/status**) · `f2 = category index` ·
`f3 = offset/page` (0 in captures). e.g. `08 00 10 05 18 00` = read-data, category 5, offset 0.

### 6.2 Response (watch→app), binary record
Two header shapes seen; both embed a date and a data region:

```
Shape A (category 0):   02 00 | 20 <lenLo lenHi 00> | <u32 countB> | <yr:u16LE><mo:u8><dy:u8> | data…
Shape B (categories 1,2,5,7): 04 00 | 10 <cat> | 20 <u24 valА> | <u32 valB> | <yr:u16LE><mo><dy> | data…
```
- `yr:u16LE` = `ea 07` = **2026**, then month, day — the date marker is rock-solid and appears
  at off 10 (shape A) / off 12 (shape B). It anchors every record.
- Leading `02`/`04` = record-present flag; `10 <cat>` echoes the requested category.
- `read-status` requests (`f1=1`) return a short record `04 00 08 01 10 <cat>` (or `02 00 08 01`).

### 6.3 Category map (correlated; measured metric where confident)

| cat | metric | evidence |
|-----|------------------|----------|
| 0 | **HR / daily activity** (steps/cal/dist + intraday HR) | biggest activity record; grows after activity (per-interval u32 counters increment); HR tail = `ff00…` no-sample markers then one byte per HR sample in bpm (real readings redacted) |
| 1 | health metric A — **UNRESOLVED** (candidate stress/temp) | 41–43 B; captured readings were low (well below HR/SpO2/BP ranges) |
| 2 | **SpO2 (blood oxygen)** ✓ | features1 manual reading: sample byte = SpO2 % (captured value in normal 90–100 % range, redacted); the only category carrying a 90–100 reading |
| 3 | (empty in all captures) | `02 00 10 03` only |
| 4 | (empty in all captures) | `02 00 10 04` only |
| 5 | **sleep** (largest, most transitions) | 89–144 B; stage/duration runs (`0d`/`0e`/`03` = stages 1–4). A fuller sleep record starts `05 00…` instead of `04 00…` |
| 7 | health metric C — **UNRESOLVED** (candidate BP/temp) | 39 B, header `…20 21 01 00 15…`, no populated reading captured |

**HR (cat 0):** the tail is a run of `ff 00` no-sample markers followed by one byte per HR
sample (value = bpm). Format: `0xff` = no sample; otherwise a bpm byte. (Captured bpm values are
biometric → redacted; the byte layout is what matters for implementation.)

**SpO2 (cat 2) — RESOLVED:** the manual SpO2 test lands in **cat 2** — the only category whose
sample byte held a value in the 90–100 range (a normal SpO2 %; captured value redacted). Record
shape (sample byte shown as synthetic `<spo2>`):
`04 00 10 02 | 20 <u24> | <u32 count> | <yr:u16LE> mm dd | … | 02 00 <u32 nsamp> | … <spo2>`.

Still open: exact **per-sample stride** for the HR series (interleaved `<offset,value>` vs.
packed), and the metric identity of **cat 1 / cat 7**. **Blood pressure was never measured** in
any capture (no systolic/diastolic ~120/80 pair appears), so BP's category is unconfirmed —
needs a capture with a BP reading.

---

## 7. Live push / watch-originated control — `0x10` (watch→app only)
Multiplexed watch→app channel keyed by `f1`:
- `f1=3` (`f2`=9 at connect, 15 mid-workout), `f1=4`: **status/ready ping** (records-available).
- `f1=1`: **media transport control** (§9.4). `f1=2`: **find-phone** (§9.5).

So `0x10` is both the periodic status ping AND the transport for watch-initiated phone controls
(media, find-phone). See §9.4–9.5 for the control payloads.

---

## 8. Incoming call / telephony — HFP (classic Bluetooth), NOT the command channel

The incoming-call signal does **not** use the C1 command protocol at all — there is **no call
opcode** on `0x0026`/`0x0028`. Calls are delivered over **classic-Bluetooth Hands-Free Profile
(HFP) via RFCOMM**, with the watch as HF/headset (HS) and the phone as Audio Gateway (AG).
Verified in `call-012300-*`: across the entire ring window (frames 23840–24160, 18:19:30–48
PDT) there are **zero** command-channel frames — the watch rings purely from HFP. No caller number/text
ever appears in the command-channel stream. (The "<city>" strings are all `0x12` weather; weather area
530 = the CA locale, unrelated to the caller.)

**SLC setup** (once, on link): standard HFP — `AT+BRSF`, `AT+BAC`, `AT+CIND=?`/`AT+CIND?`,
`AT+CMER=3,0,0,1`, `AT+CHLD=?`, **`AT+CLIP=1`** (enable caller-ID), **`AT+CCWA=1`**
(call-waiting), `AT+COPS`, `AT+CGMI`→`<phone-vendor>`, `AT+CGMM`→`<phone-model>`. Indicator order from
`+CIND=?`: **1=call, 2=callsetup, 3=service, 4=signal, 5=roam, 6=battchg, 7=callheld**.

**Incoming-call sequence** (AG→HF, unsolicited):
```
+CIEV: 2,1     callsetup=1 (incoming)                 → watch starts ringing
RING                                                   ┐ repeats ~every 5 s
+CLIP: "+1XXXXXXXXXX",145,,,"<caller name>"             ┘ (4 RING/+CLIP pairs captured)
+CIEV: 1,1     call=1 (answered / active)
+CIEV: 2,0     callsetup=0
+CIEV: 1,0     call=0 (ended)
```
The watch issues `AT+CLCC` (list current calls) as HS during the ring.

**Caller-ID IS fully recoverable** from the `+CLIP` unsolicited result:
- **number** = `+1XXXXXXXXXX` (type **145** = international, `+` prefix)
- **name/alpha** = `"<caller name>"` (present → a *saved* contact, not an unknown number)

(The number was missed in an earlier byte-grep only because tshark dissects HFP text into
`bthfp.*` fields, so the ASCII digits never appear in the `btatt`/`btspp`/`data.data` fields —
an extraction artifact, not real absence. It is plainly present as `"+1XXXXXXXXXX"`.)

**Answer / reject** (HF→AG, standard HFP): `ATA` = answer, `AT+CHUP` = hang up / reject,
`AT+CHLD=<n>` = hold/swap (watch advertised `+CHLD: (0,1,1x,2,2x,3)`). `AT+CHUP` observed at
end-of-call.

**Implication for the coordinator (Gadgetbridge):** incoming-call ring, caller number+name, and
answer/reject are **standard HFP driven by Android's Bluetooth HFP-AG stack automatically** —
NOT a custom feature to build on the command channel. Message notifications, by contrast, DO ride
the command channel as `0x11`/`0x13`.

---

## 9. Phone-batch features — dial, find-device, alarm, media, find-phone, camera

**Mechanism matrix** — what the coordinator must implement on the command channel vs. what Android's Bluetooth
stack provides for free:

| feature | direction | channel | who implements |
|---|---|---|---|
| Dial install | phone→watch | **bulk plane** (D1→D2→D4) | coordinator |
| Dial apply/switch | phone→watch | auto-applied by install (standalone switch command not captured) | coordinator |
| Find-device (ring watch) | phone→watch | **C1 command `0x18`** | coordinator |
| Alarm get/set | phone→watch | **C1 command `0x07`** | coordinator |
| Media transport (play/pause/next/prev) | watch→phone | **C1 command `0x10`** (NOT AVRCP) | coordinator |
| Find-phone (ring phone) | watch→phone | **C1 command `0x10`** push | coordinator |
| Camera shutter | watch→phone | **classic-BT HID** (mouse click) | Android |
| Incoming call | phone→watch | **classic-BT HFP** (§8) | Android |

Verified in `final-20260711-014454-*` (batch in the tail, ~18:35–18:44 PDT). Zero **AVRCP/AVCTP**
frames in the whole capture → media is not AVRCP. Two NEW C1 opcodes appeared: **`0x07`** and
**`0x18`**.

### 9.1 Dial install — bulk plane (D1 announce → D2 chunks → D4 complete)
Watch faces, AGPS almanacs, and firmware all use a **separate bulk plane on the same write/notify
handles** (first byte ≠ `0xC1`/`0xC3`). Verified installing custom dial `custom_id_25022.bin`
(231,293-byte ZIP; manifest `dial.json` seen in-stream: `{name:"CWR05G_23687", dial_version:4,
dial_type:6, resolution_ratio:"466x466", platform:"ats3085s", describe_name:"Vibrant Metal",
preview:"preview_0565.bmp"}`; the ZIP contains `dial.json`, `file.json`, `BG_0565.bmp`,
`kcal_*_8888.png`, …).

- **`D1` file-info announce** (app→watch): `D1 | status(00) | size:u32LE | size2:u32LE |
  type(0x0f) | filename<null-terminated>`. Real: `d1 00 7d870300 7d870300 0f
  "custom_id_25022.bin"\0` → size `0x0003877d` = 231293. Watch acks `d1 0000` on `0x0028`.
- **`D2` data chunks** (app→watch): `D2 | counter | <≤234 payload bytes>` — thousands of frames,
  reassembled in counter order. (Identical channel to the firmware DFU in decode-notes.)
- **`D4` transfer-complete** (app→watch): `D4 00 00 | u32`. Real: `d4 0000 35b70000`
  (`0x0000b735`). The u32 is a **checksum/CRC**, not the size (it ≠ 231293, and the AGPS D4
  `d4 0000 9bb80000` likewise ≠ its file size). Watch acks `d4 0000`.
- **Apply = automatic**: the install auto-activates the face. The post-install `0x16` dial-list
  read shows the active dial (field 14) = `custom_id_25022.bin` (was `YHZN_1021@LC.bin`). No
  separate C1 switch command was transmitted.
- The AGPS almanac (`ephemeris.gnss`, `offEphemeris.agnss`) uses the **same** D1/D2/D4 plane on
  connect — this is decode-notes' "0xD2 bulk", now with its D1/D4 control frames decoded.

*UNRESOLVED:* switching to an ALREADY-installed dial by Id (opcode undetermined) was not
exercised (we only installed + auto-applied). Likely a small C1 command carrying the dial Id.

### 9.2 Find-device (ring/buzz the watch) — C1 command `0x18` (phone→watch)
`f1=2, f2=1, f3 = 1 (start buzz) / 0 (stop)`. Real: `08 02 10 01 18 01` → watch buzzes, acks
empty; `08 02 10 01 18 00` stops. (Same opcode for both start and stop, distinguished by `f3` —
this resolves decode-notes open item #5.)

### 9.3 Alarm — C1 command `0x07` (phone→watch) — RESOLVES the earlier UNRESOLVED
`f1 = 1 (get) / 2 (set)` · `f2 = alarm count` · `f3 = repeated alarm entry`. Alarm entry
(protobuf):
```
f1 = index      f2 = enabled(1)     f4 = hour     f5 = minute
f7 = 7-byte weekday-repeat (one byte per day; all-zero = one-shot)
f9,f10 = type / snooze (≈4, 10)     f3,f6,f8 = UNRESOLVED
```
Add (1 alarm): `08 02 10 01  1a1b 08 00 10 01 18 00 20 00 28 18 30 01 3a07 00000000000000 40 01
48 04 50 0a` → alarm[0], hour 0, minute 24. The **update** op edited minute 24→26 (`28 18`→`28
1a`) and wrote **two** alarms (`08 02 10 02 …`) with alarm[1] hour 1, minute 36. hour=f4 /
minute=f5 is confirmed by which bytes the add→update pair moved.

### 9.4 Media transport control (from the watch) — C1 command `0x10` push, NOT AVRCP
The capture has **zero AVRCP/AVCTP frames**. Media control is delivered as watch→app `0x10`
notifications: `f1=1, f2 = action`. Observed `f2 ∈ {1,2,3}` clustered in the media-test window
(18:37–18:40) = play/pause · next · previous (exact code↔action mapping inferred, not
label-verified). The coordinator must consume these on the command channel and drive Android's media session
itself — there is no AVRCP for Android to bridge.

### 9.5 Find-phone (ring the phone, from the watch) — C1 command `0x10` push
Same watch→app `0x10` control channel, distinguished by `f1` (observed `08 02 10 02` and `08 04`
outside the media window, at 18:41/18:44). The app rings the phone on receipt. Mechanism (command-channel
`0x10`) is certain; the **exact `f1`/`f2` code is tentative** — `0x10` multiplexes several
watch-originated events (routine status `f1=3 f2=9/15`, media `f1=1`, these). Needs a clean
single-action capture to pin the code.

### 9.6 Camera shutter (from the watch) — classic-BT HID, NOT the command channel
The shutter is a **classic-Bluetooth HID input report — a mouse right-button click** (Report
Type Input, Protocol Code Mouse `0x02`, Button Right = 1), 3 press/release pairs at
18:42:34–40. Not the command channel, not AVRCP, not a consumer/volume key. Android's HID stack delivers the
event; only 6 `bthid` frames exist. The coordinator does not implement this.

### 9.7 Workout GPS track + summary — NOT synced over BLE in any capture (needs fresh capture)
Exhaustively ruled out across `workout-*` and `final-*`:
- **Not on C1**: a broad scan (every C1 payload, scales 1e5/1e6/1e7, LE/BE signed+unsigned +
  float32) finds no lat/lon near the workout location.
- **Not in the bulk plane**: the only `D1` file-info announces in any capture are `ephemeris.gnss`,
  `offEphemeris.agnss` (AGPS), `custom_id_25022.bin` (dial), and `res.ota` (firmware, size
  `0x0020c6e0` = 2,148,064 B — matches decode-notes). **No workout/track/sport file is ever
  announced.** All `D2` chunks in `workout-*` (457 frames) belong to the AGPS ephemeris uploads.
  (The many ~39.6/−121 "hits" in the D2 scan are the AGPS almanac's own coarse-position data,
  which the phone pushes TO the watch — not a track FROM it.)
- **Not in 0x0e history**: request categories are only `0,1,2,3,4,5,7`; none is a sport/track
  category, and no sport-summary (duration/type/distance/speed/pace) record appears. cat 0
  merely accumulates step/HR intervals.

The watch HAS a GPS receiver (AGPS ephemeris is pushed to it), so a track likely exists on-device
but the **sport-history sync was never triggered** in these
captures — the ~30 s workout produced only live-screen metrics, no post-workout upload. **Verdict:
QUEUE A FRESH CAPTURE** that (a) runs a workout, then (b) explicitly pulls workout history in the
app (Records/History screen) so the stored sport-history read/response exchange (likely another `D1`-announced file or a
new `0x0e`/bulk flow) is on the wire. Until then the trackpoint format is genuinely undetermined.

---

## 10. UNRESOLVED

**Needs a FRESH capture (data genuinely absent from all current traces):**
1. **Workout GPS track + summary** — proven absent from C1, the bulk plane, and 0x0e history
   (§9.7). The sport-history sync was never triggered. Capture: run a workout, then open the
   app's Records/History screen to force the stored sport-history pull (vendor-SDK-suggested opcode; not confirmed from our captures).
2. **Blood pressure** category — no BP measurement was ever taken; its 0x0e category (candidate
   cat 1 or 7) is unconfirmed. Capture a manual BP reading.
3. **Dial switch-to-existing** (by Id; opcode undetermined) — only install+auto-apply was
   seen (§9.1). Capture: switch between two already-installed faces.
4. **Exact `0x10` sub-codes** for media (play/pause/next/prev) and find-phone (§9.4–9.5) —
   mechanism known (command-channel 0x10), codes tentative. Capture: one action per press.
5. **contacts**, **DND**, **sedentary/drink reminders**, per-app **notification-switch write** —
   not exercised.

**Resolvable from existing data but not yet pinned (lower value):**
6. **HR per-slot cadence** — the daily-HR (cat 0) 2-byte slot array decodes (bpm = 2nd byte), but
   the per-slot timebase is provisional. *(The full category map is now RESOLVED — 0=HR, 1=stress,
   2=SpO2, 3=sleep, 4=workout, 5=activity, 7=HRV; the earlier "sleep=cat5"/"cat7=BP" labels were
   wrong. See [`health-metrics.md`](health-metrics.md).)*
7. **Time f9 = 1140** — constant across all captures; not the timezone carrier (the watch derives
   tz from f8 UTC-epoch vs f4–f6 local, PDT −7 h here), so f9 is some other constant
   (locale/protocol tag). Meaning undetermined; not blocking (tz is recoverable without it).
8. **0x22** setting identity; **0x04** bitmap meaning (fields 1–11,22,23,25 booleans — likely
   per-app notification switches).

---

## 11. Corrections to `docs/decode-notes.md`
(decode-notes must not be edited — recording deltas here instead)

1. **LEN is 16-bit little-endian at off6–7**, not a 1-byte field at off6 with 4 reserved bytes.
   Reserved is off8–10 (3 bytes). Proven by the 377-byte weather frame (`LEN=79 01`).
2. **CRC is present only on watch→app protobuf frames** (LEN = total−2). app→watch commands
   carry **no CRC** (LEN = total); `0x0e` flag=1 binary records carry no CRC in either
   direction. decode-notes' "CRC over the whole frame, LEN excludes CRC" is true only for the
   watch→app protobuf class.
3. Module id is **`UC6228CI`** (8 chars), not `UC62228CI`.
4. Opcode meanings refined: `0x11` = detailed notification (text in f6), `0x13` = summary
   notification (text in f5, flag=1), **`0x12` = weather** (new), `0x03` = profile/goals bundle
   (new), `0x04` = boolean bitmap (new), `0x10` = watch push (new). The `0x0e` block is split
   into flag=0 (protobuf switches) and flag=1 (binary history sync).
5. Wire opcodes are a small namespace (`0x01`–`0x22`); history/health sync is carried by `0x0e`
   flag=1 + a category index (not by any separate high-level opcode).
6. Two more wire opcodes decoded from the phone-batch capture: **`0x07` = alarm**, **`0x18` =
   find-device** (§9). The `0xD1/0xD4` frames are the **bulk-plane control** (file-info announce
   / transfer-complete) that brackets the `0xD2` chunk stream (§9.1) — not "TBD" as
   decode-notes had them. Incoming call = HFP (§8), camera shutter = HID (§9.6): both are
   classic-BT, off the command channel entirely.

---

## Appendix — reproduce

```
decode.py     <capture.log> [ophex,...]   # full frame decode
inventory.py                              # opcode census + CRC audit
buckets.py    <ophex> [capname]           # sub-command buckets
health.py     <capture.log>               # 0x0e flag1 req/resp pairing
```
tshark filter: `btatt.handle == 0x0026 || btatt.handle == 0x0028`; opcode = value byte index 5.
