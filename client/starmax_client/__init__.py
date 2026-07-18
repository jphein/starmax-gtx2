"""starmax_client -- standalone BLE client for the Starmax GTX2 (Runmefit) smartwatch.

Layers:
    crc, protobuf   -- primitives
    framing         -- 0xC1 frame codec + 0xC3 reassembly (transport-independent)
    commands        -- command builders (bind, set-time, notify, weather, health-sync,
                       find-device, alarms)
    records         -- 0x0e flag=1 binary health-record header decode
    transport       -- bleak NUS transport (import pulls in bleak lazily)

The codec layers have no third-party deps and are fully unit-tested offline against real
captured frames. See README.md.
"""
from . import commands, crc, framing, protobuf, records  # noqa: F401

__all__ = ["commands", "crc", "framing", "protobuf", "records"]
__version__ = "0.1.0"
