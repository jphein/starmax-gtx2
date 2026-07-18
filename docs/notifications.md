# Notifications on the GTX2 — how they work, and why CLI *display* is gated

Authoritative write-up of the notification investigation. Capture-derived (PORTABLE — also
explains the constraint for the Gadgetbridge coordinator). Decision: **CLI notifications are out
of scope on an LE-only host** (see §4); everything else the CLI does works over LE.

## 1. The message notification frames (LE, byte-correct)
Message notifications ride the `0xC1` command channel:
* **`0x11` detailed** (flag 0): `08 01 10 02 18 06 20 64 28 00 32 1a "<title>" 3a 00` — title→f6,
  body→f7. Our `build_notification_detailed()` is **byte-identical** to a captured notification
  that displayed (`"How to use TechEmpower.org"`). The `0x11` was never the problem.
* **`0x13` summary/count** (flag 1): text→f5.

## 2. The LE enable exchange (necessary, implemented)
The vendor app runs a notification-**enable** exchange on connect *before* any `0x11`; without it
the watch ignores the notification:
1. **`0x04` feature bitmap** — payload `08 01 10 02`; watch replies with its capability bitmap.
2. **`0x03` profile+toggles bundle** — `{f1:2, f2:profile, f3:notif-toggles(all on), f4:goals}`.

Available in the `notify` path as **opt-in** (`notify.enable_notifications()` +
`settings.build_feature_bitmap()`): default **off** — `notify --enable` sends it. Off by default
because the `0x03` bundle writes a default profile (resetting the watch's profile/goals) and
notifications won't display on LE-only anyway (§3). **There is no per-app whitelist/registration**
— the `0x11` `f3` is a category/icon index; the filter is the `0x03` per-category toggles gated by
the `0x04` enable.

## 3. Why it still doesn't DISPLAY — the classic-BT companion gate
Live test: the full LE handshake **+ enable + `0x11` on a held LE connection still showed "app
not connected" and no notification.** HCI forensics (`pairing` + `notif-real`) explain why:

* The vendor app talks to the watch **entirely over CLASSIC BR/EDR** — **zero LE connections**
  (no LE Create-Connection, no LE meta events). The command/notification data (ATT incl. `0x11`)
  rides **ATT-over-BR/EDR** (fixed L2CAP CID `0x0004`) on the classic ACL.
* The watch is a **dual-mode** device with different addresses: classic `F4:4E:FD:11:22:34`,
  LE `F4:4E:FD:11:22:33` (example — yours will differ).
* "App connected" = a **classic companion link**: SSP bond (Just Works, persistent link key) →
  RFCOMM → **HFP Service-Level Connection on RFCOMM channel 1** (watch = HFP Hands-Free `0x111e`,
  phone = Audio Gateway; `+BRSF/+CIND/+CMER/+CHLD/+COPS`). Maintained/re-paged continuously
  (17 classic ACL connects across a session).

An LE-only client (`bleak`, `ControllerMode=le`) drives GATT over LE fine (dial-push installed
live) but **cannot be a classic HFP companion**, so the watch reports "app not connected" and
suppresses notifications. The `0x04`/`0x03` enable is **necessary but not sufficient** here.

## 4. Why the classic-companion workaround is out of scope (path A, rejected)
We scouted making the LE host a classic companion (dual-mode + HFP-AG). Two blockers stack:

1. **Dual-mode breaks LE on the host (live-confirmed):** switching the controller to
   `ControllerMode=dual` makes the watch's LE GATT connect fail — notify char `0x0002` not found,
   0 LE services enumerated. So we can't even send the LE `0x11` while classic is up on this
   adapter.
2. **The watch won't bridge transports:** the app does *all* data — companion **and** the `0x11`
   ATT — over classic. A classic companion + an LE data session is almost certainly not honored;
   the watch expects notifications on the same (classic) transport as the companion.

⇒ Real solutions, both **out of scope** for the standalone Python/`bleak` client:
* a **GATT-over-BR/EDR (classic ATT) client** — raw L2CAP fixed channel `0x0004` ATT + the HFP
  companion, all over classic. `bleak` is LE-only and cannot do this; it's a from-scratch
  classic-BT stack project.
* or a **second BT adapter** dedicated to the classic HFP companion while the LE adapter does
  GATT — hardware + a bridged "connected" state the watch may still not accept.

The staged BlueZ/HFP-AG recipe (SSP bond → HFP SLC on RFCOMM ch1, via ofono or a custom
RFCOMM-AG stub) is documented for a future revisit.

## 5. Decision
**B — notifications stay on the phone companion; the CLI owns everything else over LE**
(health, dial install + custom faces, settings, alarms, find, time, workouts). The LE-side
notification-enable (§2) is committed as the correct, forward-compatible piece; it just can't
overcome the classic-companion gate on LE-only hardware.
