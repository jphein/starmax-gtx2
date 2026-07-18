"""Multi-watch registry: scan merge, last-seen, and the HA payload shape."""
from __future__ import annotations

import datetime as _dt

from gtx2_bridge.registry import Registry


class _Clock:
    def __init__(self):
        self.t = _dt.datetime(2026, 7, 14, 9, 0, tzinfo=_dt.timezone.utc)

    def __call__(self):
        self.t += _dt.timedelta(seconds=30)
        return self.t


def test_merge_scan_adds_watches_with_last_seen():
    reg = Registry(clock=_Clock())
    watches = reg.merge_scan([("GTX2-A", "AA:BB:CC:11:22:33", -55),
                              ("GTX2-B", "AA:BB:CC:44:55:66", -70)])
    assert len(watches) == 2
    a = next(w for w in watches if w.slug == "aabbcc112233")
    assert a.name == "GTX2-A" and a.rssi == -55 and a.seen and a.last_seen


def test_stronger_rssi_sorts_first():
    reg = Registry(clock=_Clock())
    reg.merge_scan([("GTX2-B", "AA:BB:CC:44:55:66", -70),
                    ("GTX2-A", "AA:BB:CC:11:22:33", -55)])
    order = [w.slug for w in reg.watches()]
    assert order[0] == "aabbcc112233"           # -55 dBm beats -70 dBm


def test_watch_missing_from_next_sweep_marked_unseen_but_kept():
    reg = Registry(clock=_Clock())
    reg.merge_scan([("GTX2-A", "AA:BB:CC:11:22:33", -55),
                    ("GTX2-B", "AA:BB:CC:44:55:66", -70)])
    reg.merge_scan([("GTX2-A", "AA:BB:CC:11:22:33", -50)])   # B gone this sweep
    by_slug = {w.slug: w for w in reg.watches()}
    assert by_slug["aabbcc112233"].seen is True
    assert by_slug["aabbcc445566"].seen is False             # kept, greyed out
    assert len(by_slug) == 2


def test_as_payload_shape():
    reg = Registry(clock=_Clock())
    reg.merge_scan([("GTX2-A", "AA:BB:CC:11:22:33", -55)])
    p = reg.as_payload()
    assert p["count"] == 1 and "scanned_at" in p
    w = p["watches"][0]
    assert set(w) >= {"mac", "slug", "name", "rssi", "last_seen", "seen"}
