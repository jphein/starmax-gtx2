# GTX2 dial `type` bindings — the firmware-side truth (RE)

**What binds where** for the `type` field of a `dial.json` `item[]` — i.e. which live datum the GTX2
firmware drives into each widget. This decides **how an external value (grid kW, any HA sensor) can be
shown on a custom face**, and why the "date" slot showed a phantom value.

**Date:** 2026-07-16 · **Evidence:** the firmware's own binding dispatch table
carved from the retail app (`firmware/…zephyr.bin` §section0 offset `0x13A6D4`), cross-checked against
all captured vendor faces (`work/watchface-format/unpacked/{act06120103,cw07630401,cwr01g21505}`), the
dial schema (`docs/watchface-format.md §1.4`), `starmax_client.dialface.DATA_BINDINGS`, and
`build_set_time` (`starmax_client/commands/base.py`). Companion: [`watchface-format.md`](watchface-format.md),
[`protocol-spec.md`](protocol-spec.md).

---

## 1. The firmware binding vocabulary (authoritative)

The retail firmware carries an ordered dispatch table of binding-name strings:

```
year, month, day, hour, min, second, hourhi, hourlo, minhi, minlo, week, date, time,
battery, disturb, bluetooth, step, distance
```

(plus, from the schema + vendor faces: the structural `background`/`other`/`calling` and the health
`heart`/`calorie`/`sport`/`steplist`/`updategao`.) These split into **two groups**:

| Group | Bindings | Source of the value | App-writable? |
|---|---|---|---|
| **RTC calendar/clock** | `year month day hour min second hourhi hourlo minhi minlo week date time` | the real-time clock, set by `set_time` (0x02) / `set_time_custom` | **YES** — via set-time, but only a **valid calendar value** (see §3) |
| **On-device status/sensor** | `battery disturb bluetooth step distance heart calorie sport` | the watch's own hardware/counters | **NO** — read from device, not app input |

There is **no** generic text / number / "custom" data binding. Weather *data* exists in the firmware
(`weather_type` / `temperature` protobuf, pushed via `push_weather` 0xB9) but has **zero dial-widget
binding** to surface it on a custom face. (The `custom` / `temperature` strings elsewhere in the binary
are a `dial.json` structural field and a weather data-model field respectively — **not** renderable
bindings.)

---

## 2. How each date/calendar binding renders (the gotcha)

| `type` | Renders | Vendor widget | Good as a plain number? |
|---|---|---|---|
| `day` | RTC **day-of-month** (1–31) | `text`, plain digits (`act06120103`: `min_numwidth=2`) | **YES** — the honest numeric slot |
| `date` | RTC **date, FORMATTED/compound** (MM-DD-ish, with separators/units) | `text` with `unit_count=11` (`cwr01g21505`) | **NO** — formatted; clips to its viewport |
| `month` | RTC **month** (1–12) | **`array`** of 12 month-name images | NO — it's an image array |
| `week` | RTC **weekday** (0–6) | **`array`** of 7 weekday images | NO — it's an image array |
| `hour`/`min` | RTC clock | `text` (big) or `pointer` hand | yes (but they *are* the clock) |
| `second` | RTC seconds (0–59) | `text` / `pointer` | yes |

**`day` ≠ `date`.** `day` is a bare integer; `date` is a formatted compound field. Binding a plain
digit-font number widget to `type="date"` renders garbage/clipped (this is the origin of the on-glass
"ticking ~70" phantom — `date` formatting a month+day value, **not** battery, **not** the pushed day).
**Use `day` for a plain pushed number; never `date`.** For weekday/month use the **array** widget.

---

## 3. Can a binding carry a value we push? — YES, within limits

`build_set_time` (0x02) writes the **full** datetime — `f1=year f2=month f3=day f4=hour f5=min
f6=second f7=weekday f8=epoch f9=tz` — so `set_time_custom` can drive the RTC calendar bindings to an
arbitrary datetime. Therefore:

- **Usable plain-number carriers** (fed by set-time, rendered as bare digits): **`day` (1–31), `second`
  (0–59), `hour` (0–23), `min` (0–59)**.
- **Not usable as plain numbers:** `date` (formatted + clips), `week`/`month` (image arrays).
- **Constraint:** the value must fit the field's valid RTC range. You **cannot** show raw watts (e.g.
  `934`) through `day` — the RTC clamps day to 1–31. You **can** show **integer kW 0–12** through `day`
  (fits 1–31) and **tenths of kW 0–9** through `second` (fits 0–59).

**Proven on hardware (2026-07-16):** the shipping live-kW face feeds `hour/min = clock, day = integer
kW, second = tenths kW` via `set_time_custom` and rendered `1126 W → "1.1 kW"` with **no image re-push**.

**On-glass diagnostic confirmation (2026-07-16, JP's read of a labelled binding-map face):** every
prediction held — `day` renders a **big, readable plain number** (the firmware HONORS a large *declared*
widget height, not the tiny vendor h≈20); `date` **truncates** its compound value (showed `0731`);
`week`/`month` render as **image arrays**. This locks Path A (§4).

---

## 4. Consequence for showing an external value (grid kW)

Two viable paths; the choice is a **size/readability** call (settled by the on-glass diagnostic):

- **Path A — live-on-`day` (no re-push).** JP's kW is integer 0–12, which *fits* `day` (int part) +
  `second` (tenths). Push it via `set_time_custom`; the value updates live with **no blob re-push**.
  **LOCKED (confirmed on-glass 2026-07-16):** the firmware honors a large declared `day` height, so
  `day` renders big + readable — kW lives on `day` at full size, live, **no baking**. Shipping approach.
- **Path B — bake per value (re-push).** Bake a big/prominent kW number into the background art and
  re-push the whole blob on each change (reliable since the radio-contention fix, see
  [[dial-push-streams-not-installs]] / `gtx2-face-value-display`). Needed if `day` is too small, or for
  values that don't fit a calendar field (raw watts, >12, more precision).

**Geometry note — the widget height is OURS to set (confirmed on-glass 2026-07-16).** The firmware
honors the *declared* `w`/`h` for plain-number text bindings: `day` declared large renders big + readable
(vendors merely *chose* a tiny h≈20 for it — that is not a firmware limit). The one exception is `date`,
which truncates its compound value regardless of declared size (on-glass: `0731`); `week`/`month` are
image arrays. ⇒ a *big* readable number can live on ANY plain-number binding (`day`/`second`/`hour`/`min`)
just by declaring a large height — it is NOT restricted to the `hour`/`min` clock slots, and it does NOT
require baking. Path B (bake) is only needed for values that don't fit a calendar range (raw watts, >12,
more precision).

---

## 5. The `date`→`day` fix (spec-level)

`starmax_client.dialface` correctly supports **both** `date` and `day` and passes them through unchanged
— so this is **not** a dialface code bug. The fix is a **widget-spec** choice: any face that wants a
plain pushed number on a calendar slot must use `type="day"` (not `type="date"`). For weekday/month,
use a `widget:"array"` with the month/week image set. (The grid-watts live face's `_grid_widgets`
already uses `day` for int-kW; only a stray `date` experiment needs reverting to `day`.)
