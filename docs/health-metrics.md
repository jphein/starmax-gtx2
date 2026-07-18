# Health metrics — category map, per-metric decode, availability verdicts

Authoritative summary of the GTX2's health-data surface: the `syncType`/category map, how each
category decodes, and what the watch does and does **not** provide. Everything here is
**capture-derived + live-confirmed on real data (2026-07-12)** unless flagged. Track B (standalone)
lane — findings stated freely.

> The wire mechanics of the history channel (`0x0e` flag=1, per-category read-data / read-status,
> the shape-A/B envelope + date marker) are in [`protocol-spec.md` §6](protocol-spec.md). This doc
> is the *semantic* layer: which category is which metric, and how each one's bytes decode.

## Category / syncType map (AUTHORITATIVE)

The watch's own health-switch list enumerates exactly these categories — this is its **entire**
health surface (no cat 6; nothing ≥ 8). Source of truth in code: `commands/base.py` `CAT_*` /
`SYNC_CATEGORIES`.

| syncType / cat | Metric | Status |
|---|---|---|
| **0** | Heart rate (intraday HR series) | ✅ decoded (live) |
| **1** | Stress | ✅ value decoded; scaling assumed |
| **2** | SpO2 (blood oxygen) | ✅ decoded (live) |
| **3** | Sleep | ⚠️ path ready; every capture an empty stub |
| **4** | Workout summary (+ inline HR curve) | ✅ decoded byte-exact (live) |
| **5** | Activity — steps / distance / calories | ✅ steps live-confirmed; dist/cal provisional |
| **7** | HRV | ✅ value decoded; scaling assumed |

> ⚠️ **Historical correction:** cat 5 was previously **mislabeled "sleep"** and cat 7 mislabeled
> "BP/temp". The real mapping is above (sleep = **cat 3**, activity = **cat 5**, HRV = **cat 7**).
> Any doc still saying "cat 5 = sleep" or "cat 0 = HR/activity" is stale — this table wins.

## Per-metric decode

All category records share the `0x0e` flag=1 envelope; the date is found by scanning the head for a
valid `year(2000-2100) month(1-12) day(1-31)` marker (`ea 07 mm dd` = 2026-mm-dd), because the date
offset differs per category (generic shapes 10/12, workout @16, activity @13). Decode all fields
**relative to that marker**, so the parse is frame-base independent.

- **HR — cat 0.** Two records: a `02 00 <nsamp>` *summary* (often empty) and a ~519 B *detail*
  (head marker `0x20`, flag 0x03) carrying a fixed array of **2-byte slots** `[marker][bpm]`. **BPM
  is the SECOND byte**; `ff 00` = unlogged slot. Sparse by design (the watch logs resting HR
  intermittently). Dense HR comes from the workout curve, not here.
- **Stress — cat 1.** One dated sub-record/day: `year:u16 month:u8 day:u8 <packed time> value:u32`,
  value at **date+8** (e.g. 8, 10). ⚠️ **offset confirmed, scaling ASSUMED** (treated as a 0-100
  score). A `02 00 <nsamp>` intraday series also exists but its cadence/scaling is unconfirmed → not
  decoded.
- **SpO2 — cat 2.** Percentage samples over the day (values > 0 accumulated for avg/min/max). ✅.
- **Sleep — cat 3.** Path implemented, but every capture so far is an **empty stub** — layout
  deliberately **unparsed** (nothing fabricated). Finishes when a populated sleep capture arrives.
- **Workout — cat 4.** A **KLV stream**: a fixed ~100 B head, then `[key:u8][len:u32 LE][value]`
  blocks (HR block = key 1). Summary-head fields (relative to the date marker), all validated
  byte-exact against a real capture:

  | field | offset | note |
  |---|---|---|
  | startTime | packed at the marker | `2026-07-12 08:02:17` in the capture |
  | duration (s) | +23 | 499 |
  | avgHR / maxHR / minHR | +32 / +33 / +34 | 80 / 104 / 58 |
  | steps | +55 | 63 |
  | calories | +59 | 27 |
  | distance (m) | +63 | 42 |
  | cadence (spm) | +75 | 7 — **best-effort**, range-guard before display |
  | stride (cm) | +77 | 66 — **best-effort**, range-guard |
  | trailData | key 6 | inline HR/pace trail |

  Intraday HR curve = the KLV block whose in-range byte distribution matches the head avg/max/min
  (block key/len drift between builds, so id-based selection is unsafe — head-matching finds the
  live curve and rejects embedded copies of other workouts). Sport-type: **no verified field** →
  logged as generic exercise.
- **Activity — cat 5.** Daily totals. **Steps** = `u32` at **date+6** — **live-confirmed** (162 and
  225 matched the watch face exactly). Adjacent u32s are read as distance/calories but are
  **PROVISIONAL** (implausible in the low-activity fixture). The record also carries a tagged
  intraday stream, but no contiguous 24/48-slot bucket array reproduces the daily total from a
  single low-activity capture → hourly buckets **unconfirmed** (daily total is the reliable figure).
- **HRV — cat 7.** Same shape as stress: daily `u32` at **date+8** (e.g. 79, 112). ⚠️ **offset
  confirmed, scaling ASSUMED** (treated as ms).

## NOT provided by this watch (closed negatives)

Grounded on the watch's declared health-category set `{0,1,2,3,4,5,7}` and empirical capture:

- **GPS route / polyline — NO.** The watch stores **no route**; workouts carry distance & pace
  only, never a coordinate track. Capture-proven (two GPS-locked walks stored no track). `0x15` is
  capability-only.
- **Raw accelerometer — NO (over BLE).** The XYZ stream is **chip-internal** (the discrete ST
  LIS2DH12); stock firmware never exposes raw accel on the wire. (Reading it would require custom
  firmware — see [`firmware-dfu.md`](firmware-dfu.md) §F; not a stock-BLE capability.)
- **Live push / streaming — NO.** All health data is **polled** via `0x0e` history sync. There is
  no realtime sensor stream on the command channel; the only watch-originated pushes are the `0x10`
  control channel (media / find-phone / "records available"), not sensor data.
- **Blood pressure — NO.** No BP health category exists on this watch; it was never measured.
- **Body energy — NO.** No such category; the watch does not emit it.
- **VO2max** — not a category; at best a field inside the cat-4 workout head — **needs a capture**
  with a known watch-face VO2max to locate. Unconfirmed; not decoded.

## Coexistence — single-owner BLE peripheral

The GTX2 is a **single-owner** BLE peripheral: **while a phone (or any central) holds it, the watch
stops advertising**, so a second central — e.g. the Linux `starmax` CLI — cannot discover or
connect. Gadgetbridge (phone) and the CLI (Linux) therefore **time-share** the watch; they can never
be connected concurrently. Disconnect one before using the other.

## Gadgetbridge coordinator (separate lane)

A separate **Gadgetbridge coordinator** (built strictly clean-room — captures + observed behaviour
only) is **HARDWARE-VERIFIED on-phone (2026-07-12)**: steps, distance, SpO2, stress, HRV, daily HR,
and workouts all display. That lane lives in a separate Gadgetbridge fork and is **not included in
this repo**. This standalone client and the GB coordinator are independent implementations of the
same capture-derived protocol; the decodes above are corroborated across both.
