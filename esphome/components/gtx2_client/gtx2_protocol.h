// GTX2 0x0FF0 / C1 protocol — a dependency-free C++ port of the verified starmax_client library.
//
// This is the protocol TRUTH ported to C++ so an ESP32-C3 ESPHome node can speak it directly:
//   * framing.py         -> build_command / frame_to_pdus / parse_frame / Reassembler
//   * crc.py             -> crc16_ccitt_false        (byte-identical; also proven in crown_ble.c)
//   * commands/base.py   -> build_bind/find/set_time/weather/alarm/state/health builders
//   * commands/files.py  -> dial-list request + dial-switch + dial-list-reply active-face parse
//   * records.py         -> activity (steps/dist/cal) + latest HR/SpO2 record decode
//   * internal opcode-resolution RE -> decode_input (music/find-phone LE inputs on op 0x10)
//
// Every builder is asserted BYTE-IDENTICAL to the Python reference by test/test_gtx2_protocol.cpp
// (golden vectors from test/gen_golden.py). NOTHING here depends on ESPHome, Arduino or ESP-IDF —
// it compiles for the host (g++) and on-device unchanged. The ESPHome glue lives in gtx2_client.*.
//
// Provenance: STANDALONE lane (Track B) — may use the APK schema; NOT clean-room; must not inform the GB PR.
#pragma once
#include <cstddef>
#include <cstdint>
#include <string>
#include <vector>

namespace gtx2_proto {

using Bytes = std::vector<uint8_t>;

// ---- C1 envelope constants (framing.py) ----
static constexpr uint8_t SOF = 0xC1;           // start-of-frame
static constexpr uint8_t CONT = 0xC3;          // last continuation fragment
static constexpr uint8_t MIDDLE = 0xC2;        // middle continuation fragment
static constexpr uint8_t DIR_A2W = 0x01;       // app -> watch (command)
static constexpr uint8_t DIR_W2A = 0x00;       // watch -> app (reply / push)
static constexpr uint8_t PROTO_VER = 0x01;
static constexpr uint8_t SEQ_HIGH_BIT = 0x80;
static constexpr size_t HEADER_LEN = 11;

// ---- wire opcodes (commands/base.py, commands/files.py, internal opcode RE) ----
static constexpr uint8_t OP_BIND = 0x01;
static constexpr uint8_t OP_SET_TIME = 0x02;
static constexpr uint8_t OP_FEATURE_BITMAP = 0x04;
static constexpr uint8_t OP_DEVICE_STATE = 0x05;
static constexpr uint8_t OP_ALARM = 0x07;
static constexpr uint8_t OP_HEALTH_SYNC = 0x0E;
static constexpr uint8_t OP_CONTROL_INPUT = 0x10;   // watch->app music/find-phone push (LE input)
static constexpr uint8_t OP_WEATHER = 0x12;
static constexpr uint8_t OP_DIAL_LIST = 0x16;       // list request/reply; also the (inferred) set op
static constexpr uint8_t OP_FIND_DEVICE = 0x18;
static constexpr uint8_t OP_CROWN = 0xA1;           // custom-fw crown stream (cfw-crown-protocol.md)

// ---- GATT (transport.py) ----
// Service 0x0FF0 (NOT Nordic UART). Write char 0x0001 (WWR), notify char 0x0002.
static constexpr uint16_t SVC_UUID16 = 0x0FF0;
static constexpr uint16_t CHR_WRITE_UUID16 = 0x0001;
static constexpr uint16_t CHR_NOTIFY_UUID16 = 0x0002;

// =============================================================================
// CRC-16/CCITT-FALSE (crc.py): poly 0x1021, init 0xFFFF, no reflect, no xorout.
// =============================================================================
uint16_t crc16_ccitt_false(const uint8_t *data, size_t len);

// =============================================================================
// CRC-16/XMODEM (files.py) — the bulk-plane whole-file check: poly 0x1021, init 0x0000.
// =============================================================================
uint16_t crc16_xmodem(const uint8_t *data, size_t len);

// =============================================================================
// Bulk plane (D1/D2/D3/D4) — dial/resource install (commands/files.py, byte-exact). [CAP]
// Raw frames, NOT the C1 envelope; each must fit ONE ATT PDU (no C1 fragmentation).
// Sequence: D3(state probe) -> D1(announce) -> D2*(chunks <=234 B) -> D4(finalize crc16-xmodem).
// =============================================================================
static constexpr uint8_t D1 = 0xD1, D2 = 0xD2, D3 = 0xD3, D4 = 0xD4;
static constexpr uint8_t D1_TYPE_FLAG = 0x0F;
static constexpr size_t DP_CHUNK_MAX = 234;   // D2 payload cap (236-B ATT value = d2 + ctr + 234)

Bytes build_d3_query();                                             // d3 00
Bytes build_d1_announce(const std::string &name, uint32_t size);   // d1 00 <u32 size> <u32 size> 0f name\0
Bytes build_d2_chunk(uint8_t counter, const uint8_t *payload, size_t len);  // d2 <ctr> <payload>
Bytes build_d4_finalize(uint16_t crc16);                           // d4 00 00 <u32 crc16>

// A watch->app bulk-plane ACK (the D1/D2/D3/D4 REPLY on the notify char). [CAP] verified byte-exact
// against our own BLE capture @5806-5816s:
//   D2 window ack (every 15 RECEIVED chunks):  d2 00 00 <u32 cum_off LE> <u32 running_crc LE>
//        → off = cumulative blob offset; val LOW-16 = crc16_xmodem(blob[0:off]). Window = 15×234=3510 B.
//   D3 state reply:  d3 00 00 <u32 staged_off LE> <u32 f2 LE>   → off = staged_off (0 = fresh).
//   D1 reply: d1 00 00   ·   D4 verify-OK reply: d4 00 00   (no fields → has_fields=false).
// A closed-loop pusher compares its own running crc16_xmodem at `off` to the ack and retransmits the
// window from the last-verified offset on mismatch (the vendor's flow control — our open-loop stream
// silently discards a dropped chunk → D4 whole-file CRC fail → install rejected).
struct DpAck { uint8_t kind = 0; uint32_t off = 0; uint32_t val = 0; bool has_fields = false; };
bool parse_dp_ack(const uint8_t *pdu, size_t len, DpAck &out);

// =============================================================================
// API-direct chunked dial push (node-only; NOT a Python port). HA renders the native container
// off-node and pushes it over the ESPHome native API in base64 slices — because the API hard-caps
// ONE message at 32 KiB (api_frame_helper.h MAX_MESSAGE_SIZE) so the whole ~35 KB blob can't ride a
// single service arg. The node base64-decodes each slice, reassembles in order, and hands the whole
// blob to the D-plane streamer. This keeps the blob off HTTP entirely (no blobd, no cross-VLAN
// firewall). Both pieces are pure + host-testable (test/test_gtx2_protocol.cpp).
// =============================================================================
// Decode standard base64 (RFC 4648, '=' padded; ASCII whitespace tolerated). false on invalid input.
bool base64_decode(const std::string &b64, Bytes &out);

// Reassembles the decoded slices of one dial blob. Strict in-order; any gap/overflow/mismatch is a
// protocol error (caller resets + the driver resends from seq 0). seq==0 (re)starts a transfer.
class ChunkAssembler {
 public:
  enum class Result { OK, COMPLETE, ERROR };
  static constexpr uint32_t MAX_TOTAL = 262144;  // 256 KB sanity cap (largest vendor dial ~231 KB)
  Result feed(uint32_t dial_id, uint32_t seq, uint32_t total_len, const Bytes &chunk);
  uint32_t dial_id() const { return dial_id_; }
  const Bytes &buffer() const { return buf_; }
  Bytes take();     // move the completed buffer OUT (leaves this empty) — lets the caller stream it
                    // with no second copy (1x peak RAM, not 2x).
  void reset();

 private:
  Bytes buf_;
  uint32_t dial_id_{0}, total_{0}, next_seq_{0};
  bool active_{false};
};


// =============================================================================
// Minimal protobuf writer (protobuf.py ProtobufWriter): fields serialised in insertion order.
// =============================================================================
class PbWriter {
 public:
  PbWriter &varint(uint32_t field, uint64_t value);
  PbWriter &boolean(uint32_t field, bool value) { return varint(field, value ? 1 : 0); }
  PbWriter &bytes(uint32_t field, const uint8_t *data, size_t len);
  PbWriter &str(uint32_t field, const std::string &s);
  PbWriter &message(uint32_t field, const Bytes &sub) { return bytes(field, sub.data(), sub.size()); }
  const Bytes &data() const { return buf_; }
  Bytes take() { return std::move(buf_); }

 private:
  void tag(uint32_t field, uint32_t wire);
  Bytes buf_;
};

void encode_varint(Bytes &out, uint64_t value);

// =============================================================================
// Minimal protobuf reader — enough to pull one scalar/len-delimited field by number.
// =============================================================================
class PbReader {
 public:
  PbReader(const uint8_t *data, size_t len) : data_(data), len_(len) {}
  // Returns true and fills `out` with the LAST len-delimited value for `field` (last-wins).
  bool get_bytes(uint32_t field, const uint8_t *&out, size_t &out_len) const;

 private:
  const uint8_t *data_;
  size_t len_;
};

// =============================================================================
// Frame build (app->watch: LEN = whole frame, no CRC) + PDU fragmentation.
// =============================================================================
Bytes build_command(uint8_t opcode, const Bytes &payload, uint8_t flag, uint8_t seq);
std::vector<Bytes> frame_to_pdus(const Bytes &frame, size_t mtu);

// ---- command builders (byte-identical to commands/base.py + files.py) ----
Bytes build_bind(uint8_t seq);
Bytes build_find_device(bool on, uint8_t seq);
Bytes build_feature_bitmap(uint8_t seq);
Bytes build_state_query(uint8_t seq);
Bytes build_alarm_get(uint8_t seq);
Bytes build_dial_list_request(uint8_t seq);
Bytes build_health_sync(uint8_t category, uint8_t subop, uint32_t offset, uint8_t seq);

// f9 (set-time tz): total UTC offset in minutes, wrapped non-negative (base._tz_field).
uint32_t tz_field(int total_offset_minutes);
Bytes build_set_time(int year, int month, int day, int hour, int minute, int second,
                     int weekday_mon0, uint64_t epoch, uint32_t tz_f9, uint8_t seq);

struct WeatherParams {
  int month, day, hour, minute;
  int condition;         // watch condition code
  int temp_current, temp_max, temp_min;  // Celsius
  std::string city;      // PII-free label
  uint32_t pressure_cpa; // hPa * 100
};
Bytes build_weather(const WeatherParams &w, uint8_t seq);

Bytes build_alarm_set(int index, int hour, int minute, bool enabled, uint8_t seq);
Bytes build_dial_switch(uint32_t dial_id, uint32_t color, uint32_t align, uint8_t seq);
// [FW] DELETE an installed face by its on-watch filename over 0x16 (byte-ref: starmax_client
// files.build_dial_delete; internal delete-opcode RE notes).
Bytes build_dial_delete(const std::string &dial_name, uint8_t seq);
// [FW] ACTIVATE/SET an installed face as current by filename over 0x16 (operate {f1=SET(1), f2=dial_name}) —
// the symmetric sibling of build_dial_delete (DELETE(2) is HW-proven). Candidate remote-activate; the
// watch's auto-activate-on-install + build_dial_switch are both no-ops on this firmware.
Bytes build_dial_activate(const std::string &dial_name, uint8_t seq);

// =============================================================================
// Frame parse + reassembly (framing.py).
// =============================================================================
struct Frame {
  uint8_t opcode = 0;
  uint8_t flag = 0;
  uint8_t seq = 0;
  uint8_t direction = DIR_W2A;
  bool is_binary = false;   // 0x0e flag=1 record (no CRC)
  bool has_crc = false;
  bool crc_ok = false;
  Bytes payload;
};

// Parse a fully-reassembled frame. Returns false on a malformed/short frame.
bool parse_frame(const uint8_t *buf, size_t len, Frame &out);

// Streaming reassembler: join 0xC1 + 0xC3/0xC2 fragments into whole frames (watch->app).
class Reassembler {
 public:
  explicit Reassembler(uint8_t direction = DIR_W2A) : direction_(direction) {}
  void reset();
  // Feed one notification PDU; append any completed frames to `out`. Returns false on a
  // protocol error (orphan continuation / bad channel byte) but keeps self-healing.
  bool feed(const uint8_t *pdu, size_t len, std::vector<Frame> &out);

 private:
  size_t declared_total(const uint8_t *buf, size_t len) const;
  void try_complete(bool force, std::vector<Frame> &out);
  uint8_t direction_;
  Bytes buf_;
  bool open_ = false;
  size_t declared_ = 0;
};

// The watch delivers every notification twice (see transport._NotifyDedup). Skip-toggle dedup
// that preserves genuine byte-identical fragments (empty 'ff 00' runs).
class NotifyDedup {
 public:
  void reset() { has_last_ = false; armed_ = false; last_.clear(); }
  bool accept(const uint8_t *pdu, size_t len);  // true = keep, false = drop the doubled copy

 private:
  Bytes last_;
  bool has_last_ = false;
  bool armed_ = false;
};

// =============================================================================
// Decoders.
// =============================================================================
// LE inputs (internal opcode RE): op 0x10, discriminator at payload[0]==0x08.
struct InputEvent {
  bool ok = false;
  std::string name;   // "music.play_pause"|"music.prev"|"music.next"|"find_phone"|"records_ready"
  int detail = 0;     // music action / records count (0 when n/a)
};
InputEvent decode_input(uint8_t opcode, const uint8_t *payload, size_t len);

// Health records (records.py). Each takes a FULL 0x0e record (post-reassembly payload).
struct ActivityData {
  bool ok = false;
  uint32_t steps = 0, distance_m = 0, calories = 0;
};
ActivityData decode_activity(const uint8_t *record, size_t len);   // cat 5
int decode_latest_hr(const uint8_t *record, size_t len);           // cat 0 -> bpm, -1 if none
int decode_latest_spo2(const uint8_t *record, size_t len);         // cat 2 -> %, -1 if none

// Protobuf replies. parse_firmware_stamp reads 0x05 f3 (drops the MAC — PII); active_dial reads
// 0x16 f14. Both return "" when absent.
std::string parse_firmware_stamp(const uint8_t *payload, size_t len);
std::string parse_active_dial(const uint8_t *payload, size_t len);

}  // namespace gtx2_proto
