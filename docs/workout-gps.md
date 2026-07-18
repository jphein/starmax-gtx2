# Health categories, activity readout, and workout GPS track

Authoritative decode of the watch's per-category history sync. Standalone lane
(live poll + capture, 2026-07-12).

> **Canonical cross-doc summary** (category map + per-metric decode + availability verdicts +
> negatives + coexistence): [`health-metrics.md`](health-metrics.md). This doc is the workout/GPS +
> activity deep-dive.

## 1. The syncType / category enum — AUTHORITATIVE (corrects earlier labels)
Fetched on the `0x0e` history-sync channel (flag 1), one record per category:

| cat | meaning | status |
|---|---|---|
| 0 | HR (intraday HR series) | [CAP] |
| 1 | stress | authoritative |
| 2 | SpO2 | [CAP] |
| **3** | **sleep** | **CORRECTED — was mislabeled cat 5** |
| **4** | **workout** (summary + GPS trail) | authoritative |
| **5** | **activity: steps / distance / calories** | **live-confirmed** |
| 7 | HRV | authoritative (was mislabeled BP/temp) |

Single source of truth: `commands.base` (health.py + records.py import from it — the old triple
definition is what let the labels drift). The earlier "near-empty cat-5 *sleep* stub" was in fact
the *activity* record (cat 5) read under the wrong label — real sleep is cat 3.

## 2. Activity (cat 5) — LIVE-CONFIRMED
`records.extract_activity(record)` decodes the ActivityDataModel. After the date marker
(`ea 07 <mm> <dd>`) comes a u16, then a little-endian `u32[]` =
`[totalStep, ?, active?, calories, distance_m, ?]`.

Live check (2026-07-12): **steps=162** (matched the watch face), **distance_m=115** (~0.71 m/step),
**calories≈397** (ticked up over the poll). Exposed in `sync-health` (the cat-5 row prints
steps/distance/calories). Polling ~1.5 s gives an effectively-live step readout. Fetch:
`health.build_activity_history()` (0x0e history-sync, cat 5 — [CAP] layer).

## 3. Workout (cat 4) — summary DECODED; GPS route a confirmed NEGATIVE
Fetch: **`health.build_workout_history()`** = `0x0e` history-sync at **category 4** — the same
history channel as the other metrics.

### GPS route — CONFIRMED NEGATIVE (no polyline on this firmware)
The GTX2 does **not retain a GPS route** (empirical): `0x15` is capability-only,
`trailData` (key=6) is the sole track struct, and this firmware never fills it — the real records
have **no key=6 block**. `gpstrack.decode_gps_track` (signed-`int32`-LE `[lat][lng]` 8-byte points,
`÷1e7`, drops `(0,0)`) stays **dead-code-ready / UNVERIFIED**: correct by construction, but there
is **no route to feed it** on this firmware. Issue #12 closed as a negative.

### Workout summary — DECODED (field map PINNED against a live watch readout)
`workout.decode_sport_head(record)` → `SportHead`. **PAYLOAD-ABSOLUTE** offsets, every field
ground-truth-confirmed on the 2026-07-12 workout:

| field | offset | type | note |
|---|--:|---|---|
| startTime | 16 | packed SportTime | year u16LE@16, month@18, day@19, hour@20, min@21, sec@22 → datetime (NOT epoch) |
| duration_s | 23 | u32LE | |
| avgHR / maxHR / minHR | 32 / 33 / 34 | u8 | |
| totalStep | 55 | u32LE | |
| totalCalories | 59 | u32LE | |
| totalDistance_m | 63 | u32LE | |
| cadence_spm | 75 | u16LE | ✓ on 2026-07-12; reads absurd on the older 2026-07-11 record (older-format variance) — trust where sane |
| stride_cm | 77 | u8 | |

Validated on both real fixtures: 2026-07-12 matches ground truth exactly; 2026-07-11 lands sane on
all fields **except cadence** (flagged above, not forced). Exposed in `sync-health` (the cat-4 row
prints the summary + a "no GPS route" note).

**Still TODO — NOT guessed:** `sportType` (a constant `byte@9 = 106` across our two walks is a
candidate, but no enum ground-truth to confirm) and `mets` (a known value of 3, but no plain int
`3`/`30`/`×10` at any offset → scaled/float; locate on more data). **Post-step tail framing:** after
`hr` + `step` the record is mostly zero-padding with scattered non-KLV bytes that do NOT tile as
clean `[key][u32]` blocks (a skip-zeros-resume walk breaks at an anomaly on BOTH records). Kept the
clean-stop + `remainder`; needs the reserved-slot rule or more records.

### 3.1 Workout record framing — KLV — block framing CRACKED
The cat-4 record is a **fixed head preamble then a KLV block stream** (`starmax_client.workout`):

```
data[0 : 0x64]                 fixed head preamble (100 B) — summary; NOT a keyed block
then repeated:  [key:u8] [length:u32 LE] [value]      (5-byte block header)
```

Block framing is **CONFIRMED from the real record** — proven exactly by the HR block
`01 1a000000` → key=1 (hr), len=26, value = the 26 bpm bytes. The keys observed in real records are
**0=head** (preamble), **1=hr**, and **3=step**; **key 6 = trailData** is the GPS-track struct
(handled by the client, but never populated on this firmware). `workout.parse_workout_klv()` splits
the preamble + keyed blocks (stopping cleanly at the first zero-padding/anomaly, returning
`remainder`+`clean`) and hands `key=6` to `gpstrack.decode_gps_track`. The real record has blocks
**hr(26) + step(513) only, no key 6** — confirming the empty trail.

Status of the earlier open items:
- **(a) head-field offsets — RESOLVED.** Pinned against a live watch readout; see the SportHead
  table in §3. (the earlier "all int32-LE" assumption was partly wrong — avg/max/min HR are `u8`s, not int32.)
- **(b) post-step tail framing — STILL OPEN.** After `hr` + `step` the tail is mostly zero-padding
  with scattered non-KLV bytes; it does NOT tile as clean `[u8][u32]` blocks (a skip-zeros-resume
  walk breaks at an anomaly on BOTH real records). Kept the clean-stop + `remainder`; resolve with
  the reserved-slot rule or more records. `sportType` / `mets` also still TODO (§3).

## 4. What's left (needs more data)
- **`sportType` + `mets`** — pin from more workout records / the sport-type enum (don't guess).
- **Post-step tail blocks** (kmSpeed/pace/elevation/etc.) — pin the reserved-slot framing from
  additional records.
- **GPS route** — nothing further: confirmed the firmware stores no polyline (§3). The decoder is
  ready if a future firmware ever populates `trailData`.

## Provenance / PII
Standalone (interop-RE) lane; no APK bytes shipped. **Location + biometrics are PII** — synthetic coords in
tests, activity/GPS readouts stay local, nothing personal committed.
