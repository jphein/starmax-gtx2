#include "gtx2_protocol.h"

#include <cstdio>
#include <utility>   // std::move (ChunkAssembler::take)

namespace gtx2_proto {

// =============================================================================
// CRC-16/CCITT-FALSE — matches crc.py and crown_ble.c (golden-vector verified).
// =============================================================================
uint16_t crc16_ccitt_false(const uint8_t *data, size_t len) {
  uint16_t crc = 0xFFFF;
  for (size_t i = 0; i < len; i++) {
    crc ^= static_cast<uint16_t>(data[i]) << 8;
    for (int b = 0; b < 8; b++)
      crc = (crc & 0x8000) ? static_cast<uint16_t>((crc << 1) ^ 0x1021)
                           : static_cast<uint16_t>(crc << 1);
  }
  return crc;
}

// =============================================================================
// Protobuf varint + writer/reader.
// =============================================================================
void encode_varint(Bytes &out, uint64_t value) {
  do {
    uint8_t b = static_cast<uint8_t>(value & 0x7F);
    value >>= 7;
    if (value) b |= 0x80;
    out.push_back(b);
  } while (value);
}

void PbWriter::tag(uint32_t field, uint32_t wire) {
  encode_varint(buf_, (static_cast<uint64_t>(field) << 3) | wire);
}

PbWriter &PbWriter::varint(uint32_t field, uint64_t value) {
  tag(field, 0);
  encode_varint(buf_, value);
  return *this;
}

PbWriter &PbWriter::bytes(uint32_t field, const uint8_t *data, size_t len) {
  tag(field, 2);
  encode_varint(buf_, len);
  buf_.insert(buf_.end(), data, data + len);
  return *this;
}

PbWriter &PbWriter::str(uint32_t field, const std::string &s) {
  return bytes(field, reinterpret_cast<const uint8_t *>(s.data()), s.size());
}

static bool read_varint(const uint8_t *d, size_t len, size_t &pos, uint64_t &out) {
  uint64_t v = 0;
  int shift = 0;
  while (pos < len) {
    uint8_t b = d[pos++];
    v |= static_cast<uint64_t>(b & 0x7F) << shift;
    if (!(b & 0x80)) {
      out = v;
      return true;
    }
    shift += 7;
    if (shift > 63) return false;
  }
  return false;
}

bool PbReader::get_bytes(uint32_t field, const uint8_t *&out, size_t &out_len) const {
  size_t pos = 0;
  bool found = false;
  while (pos < len_) {
    uint64_t tag;
    if (!read_varint(data_, len_, pos, tag)) break;
    uint32_t f = static_cast<uint32_t>(tag >> 3);
    uint32_t wire = static_cast<uint32_t>(tag & 0x7);
    if (wire == 0) {  // varint
      uint64_t v;
      if (!read_varint(data_, len_, pos, v)) break;
    } else if (wire == 2) {  // length-delimited
      uint64_t l;
      if (!read_varint(data_, len_, pos, l)) break;
      if (pos + l > len_) break;
      if (f == field) {
        out = data_ + pos;
        out_len = static_cast<size_t>(l);
        found = true;  // last-wins
      }
      pos += static_cast<size_t>(l);
    } else if (wire == 5) {  // 32-bit
      pos += 4;
    } else if (wire == 1) {  // 64-bit
      pos += 8;
    } else {
      break;  // groups / unknown
    }
  }
  return found;
}

// =============================================================================
// Frame build + fragmentation (framing.build_command / frame_to_pdus).
// =============================================================================
Bytes build_command(uint8_t opcode, const Bytes &payload, uint8_t flag, uint8_t seq) {
  size_t total = HEADER_LEN + payload.size();
  Bytes f;
  f.reserve(total);
  f.push_back(SOF);
  f.push_back(seq);
  f.push_back(DIR_A2W);
  f.push_back(PROTO_VER);
  f.push_back(flag);
  f.push_back(opcode);
  f.push_back(static_cast<uint8_t>(total & 0xFF));
  f.push_back(static_cast<uint8_t>((total >> 8) & 0xFF));
  f.push_back(0);
  f.push_back(0);
  f.push_back(0);
  f.insert(f.end(), payload.begin(), payload.end());
  return f;
}

std::vector<Bytes> frame_to_pdus(const Bytes &frame, size_t mtu) {
  std::vector<Bytes> pdus;
  if (mtu < HEADER_LEN + 1) return pdus;  // caller must use a sane MTU (>=12)
  if (frame.size() <= mtu) {
    pdus.push_back(frame);
    return pdus;
  }
  uint8_t seq = frame[1];
  pdus.emplace_back(frame.begin(), frame.begin() + mtu);
  size_t step = mtu - 2;  // 0xC3 header is 2 bytes
  for (size_t i = mtu; i < frame.size(); i += step) {
    Bytes p;
    p.push_back(CONT);
    p.push_back(seq);
    size_t end = i + step < frame.size() ? i + step : frame.size();
    p.insert(p.end(), frame.begin() + i, frame.begin() + end);
    pdus.push_back(std::move(p));
  }
  return pdus;
}

// ---- builders ----
Bytes build_bind(uint8_t seq) { return build_command(OP_BIND, {}, 0, seq); }

Bytes build_find_device(bool on, uint8_t seq) {
  PbWriter w;
  w.varint(1, 2).varint(2, 1).varint(3, on ? 1 : 0);
  return build_command(OP_FIND_DEVICE, w.data(), 0, seq);
}

Bytes build_feature_bitmap(uint8_t seq) {
  PbWriter w;
  w.varint(1, 1).varint(2, 2);
  return build_command(OP_FEATURE_BITMAP, w.data(), 0, seq);
}

Bytes build_state_query(uint8_t seq) {
  PbWriter w;
  w.varint(1, 1).varint(2, 0).varint(3, 0);
  return build_command(OP_DEVICE_STATE, w.data(), 0, seq);
}

Bytes build_alarm_get(uint8_t seq) {
  PbWriter w;
  w.varint(1, 1).varint(2, 0);
  return build_command(OP_ALARM, w.data(), 0, seq);
}

Bytes build_dial_list_request(uint8_t seq) {
  PbWriter w;
  w.varint(1, 0);
  return build_command(OP_DIAL_LIST, w.data(), 0, seq);
}

Bytes build_health_sync(uint8_t category, uint8_t subop, uint32_t offset, uint8_t seq) {
  PbWriter w;
  w.varint(1, subop).varint(2, category).varint(3, offset);
  return build_command(OP_HEALTH_SYNC, w.data(), 1, seq);  // flag=1
}

uint32_t tz_field(int total_offset_minutes) {
  int m = total_offset_minutes % 1440;
  return static_cast<uint32_t>(((m + 1440) % 1440));
}

Bytes build_set_time(int year, int month, int day, int hour, int minute, int second,
                     int weekday_mon0, uint64_t epoch, uint32_t tz_f9, uint8_t seq) {
  PbWriter tw;
  tw.varint(1, year).varint(2, month).varint(3, day).varint(4, hour).varint(5, minute)
      .varint(6, second).varint(7, weekday_mon0).varint(8, epoch).varint(9, tz_f9);
  PbWriter w;
  w.varint(1, 2).message(2, tw.data());
  return build_command(OP_SET_TIME, w.data(), 0, seq);
}

Bytes build_weather(const WeatherParams &wx, uint8_t seq) {
  PbWriter fc;
  // f7 = UI "current" (big number), f8 = range high, f9 = range low — differential-calibrated
  // (corrects §3.7's degenerate f7/f9 labels). See base.py build_weather for the derivation.
  fc.varint(1, wx.month).varint(2, wx.day).varint(3, wx.hour).varint(4, wx.minute)
      .varint(5, wx.condition).varint(6, wx.temp_current).varint(7, wx.temp_current)
      .varint(8, wx.temp_max).varint(9, wx.temp_min).str(10, wx.city);
  // Synthesized forecast (the app always sends f11/f19; with none the widget scrapes the big temp +
  // condition icon from stale slots → shows temp_min + a flickering icon). §3.7 field structure:
  // f11 hourly = {1:hi, 2:temp}, f19 daily = {1:hi, 2:lo, 3:cond}. One entry each, from the args —
  // byte-identical to base.py build_weather's empty-array default (golden "weather" vector).
  fc.message(11, PbWriter().varint(1, wx.temp_max).varint(2, wx.temp_current).data());
  fc.message(19, PbWriter().varint(1, wx.temp_max).varint(2, wx.temp_min).varint(3, wx.condition).data());
  fc.varint(22, wx.pressure_cpa);
  PbWriter w;
  w.varint(1, 2).varint(2, 1).message(3, fc.data());
  return build_command(OP_WEATHER, w.data(), 0, seq);
}

Bytes build_alarm_set(int index, int hour, int minute, bool enabled, uint8_t seq) {
  static const uint8_t kWeekdays[7] = {0, 0, 0, 0, 0, 0, 0};  // one-shot (all zero)
  PbWriter a;
  a.varint(1, index).boolean(2, enabled).varint(3, 0).varint(4, hour).varint(5, minute)
      .varint(6, 1).bytes(7, kWeekdays, 7).varint(8, 1).varint(9, 4).varint(10, 10);
  PbWriter w;
  w.varint(1, 2).varint(2, 1).message(3, a.data());
  return build_command(OP_ALARM, w.data(), 0, seq);
}

Bytes build_dial_switch(uint32_t dial_id, uint32_t color, uint32_t align, uint8_t seq) {
  PbWriter info;
  info.varint(1, 1).varint(2, dial_id).varint(3, color).varint(4, align);
  PbWriter w;
  w.varint(1, 2).message(2, info.data());
  return build_command(OP_DIAL_LIST, w.data(), 0, seq);  // OP_DIAL_SET == 0x16 (inferred)
}

// [FW] DELETE an installed watch face by its on-watch filename. The GTX2 dial control plane is one
// protobuf message on 0x16 — protocol_watch_dial_plate_operate { f1 operate enum (0=INQUIRE,
// 1=SET, 2=DELETE), f2 dial_name bytes } — and the watch deletes BY FILENAME (firmware logs
// plate_management_delete_name:file_name=%s). Enum value + field layout are byte-exact from the
// firmware protobuf-c tables (internal delete-opcode RE notes); the vendor app never
// sends this, so it is [FW]-derived, not yet live-captured — dry-run/confirm before trusting.
Bytes build_dial_delete(const std::string &dial_name, uint8_t seq) {
  PbWriter w;
  w.varint(1, 2).str(2, dial_name);  // {operate=DELETE(2), dial_name}
  return build_command(OP_DIAL_LIST, w.data(), 0, seq);
}

// [FW] ACTIVATE (SET as current/displayed) an installed face by filename — the SAME
// protocol_watch_dial_plate_operate handler as delete, with operate=SET(1) instead of DELETE(2):
// { f1=1, f2=dial_name }. DELETE(2)+filename-string is HW-proven, so SET(1)+filename-string is the
// byte-symmetric, most-likely-correct "show this face" op. NOTE this is DISTINCT from the broken
// build_dial_switch (which sends f1=2=DELETE + a nested-DialInfo message in f2, a no-op) and from
// probe_switch's opcode sweep — neither ever tried operate=SET(1) with the filename string.
// [FW]-derived, not yet live-captured: test on the sacrificial spare before trusting.
Bytes build_dial_activate(const std::string &dial_name, uint8_t seq) {
  PbWriter w;
  w.varint(1, 1).str(2, dial_name);  // {operate=SET(1), dial_name}
  return build_command(OP_DIAL_LIST, w.data(), 0, seq);
}

// =============================================================================
// Frame parse + reassembly.
// =============================================================================
static bool is_binary_record(uint8_t opcode, uint8_t flag) {
  return opcode == OP_HEALTH_SYNC && flag == 1;
}

bool parse_frame(const uint8_t *buf, size_t len, Frame &out) {
  if (len < HEADER_LEN || buf[0] != SOF) return false;
  out.direction = (buf[2] == DIR_W2A) ? DIR_W2A : DIR_A2W;
  out.opcode = buf[5];
  out.flag = buf[4];
  out.seq = buf[1];
  size_t length_field = static_cast<size_t>(buf[6]) | (static_cast<size_t>(buf[7]) << 8);
  out.is_binary = is_binary_record(out.opcode, out.flag);

  if (length_field < HEADER_LEN || length_field > len) {
    // Tolerate a length_field that overshoots only for binary/a2w where we clamp; else fail.
    if (out.direction != DIR_A2W && !out.is_binary) return false;
  }
  size_t pl_end = length_field <= len ? length_field : len;
  if (pl_end < HEADER_LEN) pl_end = HEADER_LEN;

  if (out.direction == DIR_A2W || out.is_binary) {
    out.has_crc = false;
    out.crc_ok = false;
    out.payload.assign(buf + HEADER_LEN, buf + pl_end);
    return true;
  }
  // watch->app protobuf: LEN = total-2, CRC over buf[0:LEN] at [LEN:LEN+2] (LE).
  if (len < length_field + 2) return false;
  out.payload.assign(buf + HEADER_LEN, buf + length_field);
  uint16_t stored = static_cast<uint16_t>(buf[length_field]) |
                    (static_cast<uint16_t>(buf[length_field + 1]) << 8);
  out.has_crc = true;
  out.crc_ok = (stored == crc16_ccitt_false(buf, length_field));
  return true;
}

void Reassembler::reset() {
  buf_.clear();
  open_ = false;
  declared_ = 0;
}

size_t Reassembler::declared_total(const uint8_t *buf, size_t len) const {
  if (len < 8) return 0;
  size_t length_field = static_cast<size_t>(buf[6]) | (static_cast<size_t>(buf[7]) << 8);
  if (direction_ == DIR_A2W) return length_field;
  if (is_binary_record(buf[5], buf[4])) return length_field;
  return length_field + 2;  // + CRC
}

void Reassembler::try_complete(bool force, std::vector<Frame> &out) {
  if (!open_) return;
  if (force || buf_.size() >= declared_) {
    size_t take = force ? buf_.size() : declared_;
    Frame fr;
    if (parse_frame(buf_.data(), take, fr)) out.push_back(std::move(fr));
    reset();
  }
}

bool Reassembler::feed(const uint8_t *pdu, size_t len, std::vector<Frame> &out) {
  if (len == 0) return true;
  if (pdu[0] == SOF) {
    if (open_) try_complete(true, out);  // new frame before previous completed: flush best-effort
    if (len < 8) {
      reset();
      return false;
    }
    buf_.assign(pdu, pdu + len);
    open_ = true;
    declared_ = declared_total(pdu, len);
    try_complete(false, out);
    return true;
  }
  if (pdu[0] == CONT || pdu[0] == MIDDLE) {
    if (!open_) return false;  // orphan continuation
    if (len < 2) { reset(); return false; }  // undersized continuation: [pdu+2, pdu+len) is an invalid range → OOB read (untrusted radio input)
    buf_.insert(buf_.end(), pdu + 2, pdu + len);
    try_complete(false, out);
    return true;
  }
  return false;  // unexpected channel byte
}

bool NotifyDedup::accept(const uint8_t *pdu, size_t len) {
  Bytes cur(pdu, pdu + len);
  if (armed_ && has_last_ && cur == last_) {
    armed_ = false;
    return false;
  }
  last_ = std::move(cur);
  has_last_ = true;
  armed_ = true;
  return true;
}

// =============================================================================
// Decoders.
// =============================================================================
InputEvent decode_input(uint8_t opcode, const uint8_t *p, size_t len) {
  InputEvent ev;
  if (opcode != OP_CONTROL_INPUT || len < 2 || p[0] != 0x08) return ev;
  uint8_t f1 = p[1];
  if (f1 == 0x01) {  // media control (f1=1); optional f2 at p[3] when p[2]==0x10
    int act = (len >= 4 && p[2] == 0x10) ? p[3] : 1;
    ev.name = act == 2 ? "music.prev" : act == 3 ? "music.next" : "music.play_pause";
    ev.detail = act;
    ev.ok = true;
  } else if (f1 == 0x02 || f1 == 0x04) {  // find-phone (f1=2 or 4)
    ev.name = "find_phone";
    ev.ok = true;
  } else if (f1 == 0x03) {  // new health records available
    ev.name = "records_ready";
    ev.detail = (len >= 4 && p[2] == 0x10) ? p[3] : 0;
    ev.ok = true;
  }
  return ev;
}

// --- health record header (records.parse_health_record_header) → data region ---
static bool valid_date(const uint8_t *p, size_t off, size_t len) {
  if (off + 4 > len) return false;
  int year = p[off] | (p[off + 1] << 8);
  int mo = p[off + 2], dy = p[off + 3];
  return year >= 2000 && year <= 2100 && mo >= 1 && mo <= 12 && dy >= 1 && dy <= 31;
}

// Returns pointer/len of the data region (past the date marker), or false for status/dateless.
static bool record_data(const uint8_t *p, size_t len, const uint8_t *&d, size_t &dlen) {
  if (len < 3) return false;
  uint8_t flag = p[0];
  bool shape_a = flag == 0x02;
  uint8_t marker = p[2];
  if (marker == 0x08) return false;  // status reply (no date/data)
  size_t date_off = shape_a ? 10 : 12;
  size_t marker_off = SIZE_MAX;
  if (valid_date(p, date_off, len)) {
    marker_off = date_off;
  } else {  // fallback: scan for the first plausible date (records._scan_date_marker)
    for (size_t i = 0; i + 4 <= len; i++)
      if (valid_date(p, i, len)) { marker_off = i; break; }
  }
  if (marker_off == SIZE_MAX) return false;
  d = p + marker_off + 4;
  dlen = len - (marker_off + 4);
  return true;
}

static uint32_t u32le(const uint8_t *d, size_t off, size_t len) {
  if (off + 4 > len) return 0;
  return static_cast<uint32_t>(d[off]) | (static_cast<uint32_t>(d[off + 1]) << 8) |
         (static_cast<uint32_t>(d[off + 2]) << 16) | (static_cast<uint32_t>(d[off + 3]) << 24);
}

ActivityData decode_activity(const uint8_t *record, size_t len) {
  ActivityData out;
  const uint8_t *d;
  size_t dlen;
  if (!record_data(record, len, d, dlen)) return out;
  size_t navail = dlen >= 2 ? (dlen - 2) / 4 : 0;
  if (navail < 5) return out;  // need through distance_m (u32 index 4)
  out.steps = u32le(d, 2 + 4 * 0, dlen);
  out.calories = u32le(d, 2 + 4 * 3, dlen);
  out.distance_m = u32le(d, 2 + 4 * 4, dlen);
  out.ok = true;
  return out;
}

int decode_latest_hr(const uint8_t *record, size_t len) {
  const uint8_t *d;
  size_t dlen;
  if (!record_data(record, len, d, dlen)) return -1;
  // Find the tail: the first 0xff that is part of a RUN (another 0xff within a 4-byte window).
  size_t tail = SIZE_MAX;
  for (size_t i = 0; i < dlen; i++) {
    if (d[i] != 0xFF) continue;
    size_t window_end = i + 4 < dlen ? i + 4 : dlen;
    for (size_t j = i + 1; j < window_end; j++)
      if (d[j] == 0xFF) { tail = i; break; }
    if (tail != SIZE_MAX) break;
  }
  if (tail == SIZE_MAX) return -1;
  int last = -1;
  for (size_t k = tail; k < dlen; k++)
    if (d[k] >= 30 && d[k] <= 220) last = d[k];
  return last;
}

int decode_latest_spo2(const uint8_t *record, size_t len) {
  const uint8_t *d;
  size_t dlen;
  if (!record_data(record, len, d, dlen)) return -1;
  // Locate the first `02 00` sub-header, read exactly nsamp bytes after it.
  size_t hdr = SIZE_MAX;
  for (size_t i = 0; i + 1 < dlen; i++)
    if (d[i] == 0x02 && d[i + 1] == 0x00) { hdr = i; break; }
  if (hdr == SIZE_MAX || hdr + 6 > dlen) return -1;
  uint32_t nsamp = u32le(d, hdr + 2, dlen);
  size_t start = hdr + 6;
  if (nsamp == 0 || start >= dlen) return -1;
  size_t end = start + nsamp < dlen ? start + nsamp : dlen;
  int last = -1;
  for (size_t k = start; k < end; k++)
    if (d[k] >= 70 && d[k] <= 100) last = d[k];
  return last;
}

std::string parse_firmware_stamp(const uint8_t *payload, size_t len) {
  PbReader r(payload, len);
  const uint8_t *f3;
  size_t f3len;
  if (!r.get_bytes(3, f3, f3len) || f3len < 18) return "";
  auto word = [&](size_t o) { return f3[o] | (f3[o + 1] << 8); };  // u16LE
  char buf[24];
  std::snprintf(buf, sizeof(buf), "%04d-%02d-%02d %02d:%02d:%02d", word(6), word(8), word(10),
                word(12), word(14), word(16));
  return std::string(buf);  // MAC (f3[0:6]) deliberately dropped — PII
}

std::string parse_active_dial(const uint8_t *payload, size_t len) {
  PbReader r(payload, len);
  const uint8_t *f14;
  size_t f14len;
  if (!r.get_bytes(14, f14, f14len)) return "";
  return std::string(reinterpret_cast<const char *>(f14), f14len);
}

// =============================================================================
// CRC-16/XMODEM + bulk-plane (D1/D2/D3/D4) builders (files.py, byte-exact). [CAP]
// =============================================================================
uint16_t crc16_xmodem(const uint8_t *data, size_t len) {
  uint16_t crc = 0x0000;
  for (size_t i = 0; i < len; i++) {
    crc ^= static_cast<uint16_t>(data[i]) << 8;
    for (int b = 0; b < 8; b++)
      crc = (crc & 0x8000) ? static_cast<uint16_t>((crc << 1) ^ 0x1021)
                           : static_cast<uint16_t>(crc << 1);
  }
  return crc;
}

Bytes build_d3_query() { return Bytes{D3, 0x00}; }

Bytes build_d1_announce(const std::string &name, uint32_t size) {
  // d1 00 <u32 size LE> <u32 field2=size LE> 0f <name ascii> 00  (field2=size => from-scratch push)
  Bytes f{D1, 0x00};
  for (int i = 0; i < 4; i++)
    f.push_back(static_cast<uint8_t>((size >> (8 * i)) & 0xFF));   // size
  for (int i = 0; i < 4; i++)
    f.push_back(static_cast<uint8_t>((size >> (8 * i)) & 0xFF));   // field2 = size
  f.push_back(D1_TYPE_FLAG);
  f.insert(f.end(), name.begin(), name.end());
  f.push_back(0x00);
  return f;
}

Bytes build_d2_chunk(uint8_t counter, const uint8_t *payload, size_t len) {
  Bytes f{D2, counter};
  f.insert(f.end(), payload, payload + len);
  return f;
}

Bytes build_d4_finalize(uint16_t crc16) {
  // d4 00 00 <u32 crc LE> — 16-bit crc stored in a u32, high half zero.
  return Bytes{D4, 0x00, 0x00,
               static_cast<uint8_t>(crc16 & 0xFF), static_cast<uint8_t>((crc16 >> 8) & 0xFF),
               0x00, 0x00};
}

bool parse_dp_ack(const uint8_t *pdu, size_t len, DpAck &out) {
  // Every watch->app bulk-plane reply is "Dx 00 00 …". D2/D3 carry two u32 LE fields; D1/D4 don't.
  if (pdu == nullptr || len < 3)
    return false;
  const uint8_t k = pdu[0];
  if (k != D1 && k != D2 && k != D3 && k != D4)
    return false;
  if (pdu[1] != 0x00 || pdu[2] != 0x00)
    return false;
  out = DpAck{};
  out.kind = k;
  if (len >= 11) {
    out.off = static_cast<uint32_t>(pdu[3]) | (static_cast<uint32_t>(pdu[4]) << 8) |
              (static_cast<uint32_t>(pdu[5]) << 16) | (static_cast<uint32_t>(pdu[6]) << 24);
    out.val = static_cast<uint32_t>(pdu[7]) | (static_cast<uint32_t>(pdu[8]) << 8) |
              (static_cast<uint32_t>(pdu[9]) << 16) | (static_cast<uint32_t>(pdu[10]) << 24);
    out.has_fields = true;
  }
  return true;
}

// ---- API-direct chunked dial push -------------------------------------------------------
static inline int b64_val(uint8_t c) {
  if (c >= 'A' && c <= 'Z') return c - 'A';
  if (c >= 'a' && c <= 'z') return c - 'a' + 26;
  if (c >= '0' && c <= '9') return c - '0' + 52;
  if (c == '+') return 62;
  if (c == '/') return 63;
  return -1;
}

bool base64_decode(const std::string &b64, Bytes &out) {
  out.clear();
  out.reserve(b64.size() * 3 / 4 + 3);
  uint32_t acc = 0;
  int bits = 0;
  size_t pad = 0;
  for (unsigned char c : b64) {
    if (c == '\n' || c == '\r' || c == ' ' || c == '\t')
      continue;                       // tolerate whitespace/newlines in transport
    if (c == '=') {
      pad++;
      continue;
    }
    if (pad)
      return false;                   // data after padding
    int v = b64_val(c);
    if (v < 0)
      return false;                   // invalid character
    acc = (acc << 6) | static_cast<uint32_t>(v);
    bits += 6;
    if (bits >= 8) {
      bits -= 8;
      out.push_back(static_cast<uint8_t>((acc >> bits) & 0xFF));
    }
  }
  return pad <= 2;                     // 0..2 '=' is the only valid tail
}

void ChunkAssembler::reset() {
  Bytes().swap(buf_);
  dial_id_ = 0;
  total_ = 0;
  next_seq_ = 0;
  active_ = false;
}

Bytes ChunkAssembler::take() {
  total_ = 0;
  next_seq_ = 0;
  active_ = false;
  return std::move(buf_);   // buf_ left empty; caller owns the storage (no copy)
}

ChunkAssembler::Result ChunkAssembler::feed(uint32_t dial_id, uint32_t seq, uint32_t total_len,
                                            const Bytes &chunk) {
  if (seq == 0) {                                        // (re)start a transfer
    if (total_len == 0 || total_len > MAX_TOTAL) {
      this->reset();
      return Result::ERROR;
    }
    Bytes().swap(buf_);
    buf_.reserve(total_len);
    dial_id_ = dial_id;
    total_ = total_len;
    next_seq_ = 0;
    active_ = true;
  }
  if (!active_)                                          // missed seq 0
    return Result::ERROR;
  if (seq != next_seq_ || dial_id != dial_id_ || total_len != total_ ||
      buf_.size() + chunk.size() > total_) {             // gap/dup/mismatch/overflow
    this->reset();
    return Result::ERROR;
  }
  buf_.insert(buf_.end(), chunk.begin(), chunk.end());
  next_seq_++;
  if (buf_.size() == total_) {
    active_ = false;                                     // complete; buffer() valid until reset()
    return Result::COMPLETE;
  }
  return Result::OK;
}

}  // namespace gtx2_proto
