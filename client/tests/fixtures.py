"""Real GTX2 frames captured over BLE, used as codec test vectors.

Every frame here is PII-free by construction:
  * app->watch command frames carry no personal content (bind is empty; set-time is a
    clock value; find/alarm/health-sync are control opcodes);
  * the one watch->app frame is the 0x22 setting reply, whose only payload is the setting
    value 244 (this exact frame is published in docs/protocol-spec.md §1.2);
  * the fragmented 0x16 reply contains only watch-face filenames, which are already
    published in docs/protocol-spec.md §3.10.

Provenance: extracted from BLE captures
(btsnoop HCI). Frame hex is the fully-reassembled on-wire frame (SOF..CRC).
"""

# --- app->watch commands (LEN = total, no CRC) ---------------------------------------
# bind / hello (0x01), empty payload -> 11-byte header-only frame. seq=0x01. [pairing]
BIND_SEQ01 = "c101010100010b00000000"

# set-time (0x02). seq=0x07. Local 2026-07-11 00:08:12 (UTC-7), weekday Sat(5),
# epoch f8=1783753692, f9=1140. [pairing]
SET_TIME_SEQ07 = ("c1070101000227000000000802121808ea0f1007180b2000"
                  "2808300c380540dcd7c7d20648f408")

# find-device (0x18): buzz on / off. seq 0x0b / 0x0c. [final]
FIND_ON_SEQ0B = "c10b010100181100000000080210011801"
FIND_OFF_SEQ0C = "c10c010100181100000000080210011800"

# alarm (0x07): get / set-one-alarm(index0, 00:24, one-shot). seq 0x0d / 0x0e. [final]
ALARM_GET_SEQ0D = "c10d010100070f0000000008011000"
ALARM_SET_SEQ0E = ("c10e010100072c00000000080210011a1b0800100118002000"
                   "281830013a070000000000000040014804500a")

# health-sync request (0x0e flag=1), read-data. cat0 seq0x0a, cat5 seq0x12. [pairing]
HEALTH_CAT0_SEQ0A = "c10a0101010e1100000000080010001800"
HEALTH_CAT5_SEQ12 = "c1120101010e1100000000080010051800"

# --- watch->app protobuf reply (LEN = total-2, CRC trailer) --------------------------
# setting reply (0x22): payload f1=1, f2=244; CRC 0x7c5e stored LE (5e 7c). seq 0x82.
# This is the golden CRC vector from docs/protocol-spec.md §1.2. [pairing]
SETTING_REPLY_SEQ82 = "c182000100221000000000080110f4015e7c"

# --- fragmented watch->app reply (0xC1 + 0xC3) ---------------------------------------
# 0x16 dial/resource list. First PDU is 240 bytes on the wire; the C3 continuation adds
# 15 bytes (pdu[2:]) -> 255-byte reassembled frame (LEN=253 + 2-byte CRC). [pairing]
DIAL_LIST_C1 = "c18600010016fd000000001806200c280730014001521a0801100118808010221059485a4e5f31303231404c432e62696e521a080110021880800e221043573036475f3138375f30332e62696e521a0801100618808010221043573036475f3138375f30342e62696e521a0806100b1880800c2210435730375f363230385f30312e62696e521b080110031880801a2211435730375f31323630375f30312e62696e52190801100618808006220f435730375f4d31323630332e62696e521a080610011880803c22106e756d3036313130395f31302e62696e588080c0016080809601721059485a4e5f31303231404c"
DIAL_LIST_C3 = "c386432e62696e780488010390010cbfeb"
