"""Multi-watch registry: discover every GTX2 in range and track last-seen state.

A periodic BLE scan (via the library's :func:`starmax_client.transport.scan`) finds every
advertising ``GTX2*`` watch; the registry merges each sweep into a last-seen cache and hands
HA a list of ``{mac, name, rssi, last_seen, seen}`` records to publish. Per-watch health/state
(connected, firmware, active face, metrics) is layered on top by the dispatcher when a watch is
actually reached.

The scan is injected (``scan_fn``) so the merge/serialise logic is fully unit-testable offline.
"""
from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

from .config import mac_slug


@dataclass
class WatchRecord:
    mac: str
    name: str = "GTX2"
    rssi: Optional[int] = None
    last_seen: Optional[str] = None       # ISO-8601
    seen: bool = True                     # seen in the most recent sweep?

    @property
    def slug(self) -> str:
        return mac_slug(self.mac)

    def as_dict(self) -> dict:
        return {"mac": self.mac, "slug": self.slug, "name": self.name, "rssi": self.rssi,
                "last_seen": self.last_seen, "seen": self.seen}


class Registry:
    """Last-seen cache of discovered watches, keyed by MAC slug."""

    def __init__(self, clock: Optional[Callable[[], _dt.datetime]] = None) -> None:
        self._watches: Dict[str, WatchRecord] = {}
        self._clock = clock or (lambda: _dt.datetime.now().astimezone())

    def merge_scan(self, found: List[Tuple[str, str, Optional[int]]]) -> List[WatchRecord]:
        """Fold one ``scan`` result ``[(name, address, rssi), …]`` into the cache.

        Watches present in this sweep get ``seen=True`` + a fresh ``last_seen``; watches only in
        the cache are marked ``seen=False`` (kept, so HA shows a greyed-out "last seen" entry).
        """
        now = self._clock().isoformat()
        present = set()
        for name, address, rssi in found:
            slug = mac_slug(address)
            present.add(slug)
            self._watches[slug] = WatchRecord(mac=address, name=name or "GTX2", rssi=rssi,
                                              last_seen=now, seen=True)
        for slug, rec in self._watches.items():
            if slug not in present:
                rec.seen = False
        return self.watches()

    def watches(self) -> List[WatchRecord]:
        return sorted(self._watches.values(), key=lambda r: (not r.seen, -(r.rssi or -999)))

    def as_payload(self) -> dict:
        return {"scanned_at": self._clock().isoformat(),
                "count": len(self._watches),
                "watches": [w.as_dict() for w in self.watches()]}


async def scan_watches(timeout: float = 8.0, name_prefix: str = "GTX2"
                       ) -> List[Tuple[str, str, Optional[int]]]:
    """Thin wrapper over the library scan (live). Returns ``[(name, address, rssi), …]``."""
    from starmax_client.transport import scan
    return await scan(timeout=timeout, name_prefix=name_prefix)
