"""Generic value-slot push for the *custom* GTX2 firmware (CFW opcode ``0xA2``).

**Status: [CFW] — targets firmware that DOES NOT EXIST YET.** Stock GTX2 firmware has no arbitrary-
data channel; live values today hijack the RTC calendar fields (``day``/``month`` via ``0x02``),
which caps them at ~2 tiny integers and consumes the real calendar. This module implements the
app->watch half of the generic slot channel specified in
the CFW arbitrary-fields design (§1 data model, §2 wire format): N signed ``int32``
numeric slots (fixed-point via a per-slot decimal hint) + short ASCII text slots, pushed on the
existing control characteristic under a new CFW opcode ``0xA2`` — the ``0xA_`` CFW family, beside
``0xA0`` raw-accel and ``0xA1`` crown. It is the twin of :mod:`starmax_client.crown`: same framing,
transport-independent, fully offline-testable.

Freshness is **watch-local** — the firmware stamps ``k_uptime_get_32()`` on receipt, so **no time is
pushed** and a wrong host clock cannot spoof staleness (design §2.2).

Wire format (design §2.2)::

    flag=0  numeric batch:  count:u8 , count x { idx:u8 , dec:u8 , value:i32_le }   # 6 B/slot
    flag=1  text set:       idx:u8 , len:u8 , ascii[len]                            # len <= CFW_TXT_LEN-1

⚠️ **Wire contract with the firmware.** ``CFW_NUM_SLOTS`` / ``CFW_TXT_SLOTS`` / ``CFW_TXT_LEN`` and
the byte layout here MUST stay identical to the firmware header ``cfw_slots.h``. Any change
is a coordinated two-sided edit. The tests in ``tests/test_slots.py`` pin the on-wire bytes as the
shared golden vectors.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import List, Sequence, Tuple, Union

from . import framing

# --------------------------------------------------------------------------- opcode / flags
OP_CFW_SLOTS = 0xA2        # [CFW] generic value-slot push (design §2.1; 0xA_ CFW family)
FLAG_NUMERIC = 0x00        # flag=0: numeric batch — count x {idx, dec, value}
FLAG_TEXT = 0x01           # flag=1: single text slot — idx, len, ascii

# --------------------------------------------------------------------------- slot contract
# MUST match firmware cfw_slots.h (design §1.1). Coordinate any change with the firmware side.
CFW_NUM_SLOTS = 8          # numeric slots slot0..slot7
CFW_TXT_SLOTS = 2          # text slots text0..text1
CFW_TXT_LEN = 20           # bytes per text slot incl. NUL -> max 19 ASCII on the wire
DEC_MAX = 3                # decimal-places hint range 0..3 (fixed-point)

_INT32_MIN, _INT32_MAX = -(2 ** 31), 2 ** 31 - 1


class SlotError(ValueError):
    """Raised when a slot push violates the contract (bad index / dec / int32 range / text)."""


# --------------------------------------------------------------------------- data model
@dataclass(frozen=True)
class SlotVal:
    """One numeric slot write: ``slot[idx] = value``, rendered with ``dec`` decimal places.

    ``value`` is a raw signed int32; the renderer places the decimal point, so ``SlotVal(0, 124, 1)``
    displays ``12.4`` and ``SlotVal(1, 1126, 3)`` displays ``1.126`` — one slot carries the whole
    value (no more ``day``+``month`` split for "X.X kW")."""
    idx: int
    value: int
    dec: int = 0

    def validate(self) -> None:
        if not 0 <= self.idx < CFW_NUM_SLOTS:
            raise SlotError(f"slot idx {self.idx} out of range 0..{CFW_NUM_SLOTS - 1}")
        if not 0 <= self.dec <= DEC_MAX:
            raise SlotError(f"slot {self.idx}: dec {self.dec} out of range 0..{DEC_MAX}")
        if not _INT32_MIN <= self.value <= _INT32_MAX:
            raise SlotError(f"slot {self.idx}: value {self.value} outside int32 range")


def encode_fixed(value: float, dec: int = 0) -> int:
    """Half-up fixed-point encode, environment-independent (no banker's rounding).

    ``encode_fixed(12.4, 1) -> 124``; ``encode_fixed(1.126, 3) -> 1126``; ``encode_fixed(-0.55, 1)
    -> -6``. Pair the result with the SAME ``dec`` in a :class:`SlotVal`. Analog of
    ``faces.encode_grid_live``'s half-up rounding."""
    if not 0 <= dec <= DEC_MAX:
        raise SlotError(f"dec {dec} out of range 0..{DEC_MAX}")
    n = int(abs(value) * (10 ** dec) + 0.5)
    return -n if value < 0 else n


# --------------------------------------------------------------------------- frame builders
def build_set_slots(slots: Sequence[Union[SlotVal, Tuple[int, int, int]]], *, seq: int = 0) -> bytes:
    """[CFW] Build the app->watch numeric-batch frame (design §2.2 flag=0) — one write updates many
    slots, atomic-ish. ``slots`` is :class:`SlotVal` objects or ``(idx, value, dec)`` tuples. Every
    slot is validated; a :class:`SlotError` is raised BEFORE any byte is emitted (never write OOB).
    An empty batch is rejected. A full 8-slot batch is ``1 + 8*6 = 49`` B — one un-fragmented write."""
    vals = [s if isinstance(s, SlotVal) else SlotVal(*s) for s in slots]
    if not vals:
        raise SlotError("build_set_slots: empty batch (nothing to push)")
    if len(vals) > CFW_NUM_SLOTS:
        raise SlotError(f"batch of {len(vals)} slots exceeds CFW_NUM_SLOTS={CFW_NUM_SLOTS}")
    for v in vals:
        v.validate()
    payload = bytes([len(vals)]) + b"".join(
        struct.pack("<BBi", v.idx, v.dec, v.value) for v in vals)
    return framing.build_command(OP_CFW_SLOTS, payload, flag=FLAG_NUMERIC, seq=seq)


def build_set_text_slot(idx: int, text: str, *, seq: int = 0) -> bytes:
    """[CFW] Build the app->watch text-set frame (design §2.2 flag=1). ``text`` is ASCII and is
    truncated to ``CFW_TXT_LEN-1`` (19) bytes to match the firmware cap. Raises :class:`SlotError`
    on a bad text-slot index or non-ASCII input."""
    if not 0 <= idx < CFW_TXT_SLOTS:
        raise SlotError(f"text slot idx {idx} out of range 0..{CFW_TXT_SLOTS - 1}")
    try:
        raw = text.encode("ascii")
    except UnicodeEncodeError as err:
        raise SlotError(f"text slot {idx}: non-ASCII text {text!r}") from err
    raw = raw[:CFW_TXT_LEN - 1]     # defensive truncate (firmware ignores len overflow — design §2.2)
    payload = bytes([idx, len(raw)]) + raw
    return framing.build_command(OP_CFW_SLOTS, payload, flag=FLAG_TEXT, seq=seq)


def max_slots_per_frame(mtu_payload: int) -> int:
    """Largest numeric-slot count that fits ONE PDU of ``mtu_payload`` ATT bytes.

    Budget = mtu_payload - C1 header(11) - count(1), floored at 0, div 6 B/slot, capped at
    CFW_NUM_SLOTS. At MTU 247 (mtu_payload=244) the frame limit is far above the 8-slot store cap,
    so a full batch always fits one write."""
    budget = mtu_payload - framing.HEADER_LEN - 1
    return max(0, min(CFW_NUM_SLOTS, budget // 6))
