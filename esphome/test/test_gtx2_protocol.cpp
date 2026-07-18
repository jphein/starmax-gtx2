// Host (g++) parity test for the gtx2_client protocol port.
//
// Proves the C++ builders are BYTE-IDENTICAL to the verified starmax_client Python (golden
// vectors from gen_golden.py), that inbound frames reassemble + CRC-check + route to the right
// LE input, and that the health-record + reply decoders match the Python extractors on identical
// bytes. NO ESPHome/IDF/BLE — this is the offline verification CI + maintainers rely on.
//
// Build & run:  ./run_host_test.sh   (compiles gtx2_protocol.cpp with -std=c++17 and runs)
#include <cstdio>
#include <cstring>
#include <string>
#include <vector>

#include "gtx2_protocol.h"
#include "golden_vectors.h"

using gtx2_proto::Bytes;

static int g_fail = 0;
static int g_pass = 0;

static std::string hex(const uint8_t *d, size_t n) {
  std::string s;
  char b[4];
  for (size_t i = 0; i < n; i++) {
    std::snprintf(b, sizeof(b), "%02x", d[i]);
    s += b;
  }
  return s;
}

static void expect_eq_bytes(const char *name, const Bytes &got, const uint8_t *exp, size_t elen) {
  bool ok = got.size() == elen && std::memcmp(got.data(), exp, elen) == 0;
  if (ok) {
    g_pass++;
  } else {
    g_fail++;
    std::printf("  FAIL %s\n    got : %s\n    want: %s\n", name, hex(got.data(), got.size()).c_str(),
                hex(exp, elen).c_str());
  }
}

static void check(const char *name, bool ok) {
  if (ok) {
    g_pass++;
  } else {
    g_fail++;
    std::printf("  FAIL %s\n", name);
  }
}

template <typename T>
static void check_eq(const char *name, T got, T want) {
  if (got == want) {
    g_pass++;
  } else {
    g_fail++;
    std::printf("  FAIL %s (got != want)\n", name);
  }
}

// Look up a golden vector by name.
static const GoldenVec *gv(const char *name) {
  for (size_t i = 0; i < GOLDEN_N; i++)
    if (std::strcmp(GOLDEN[i].name, name) == 0) return &GOLDEN[i];
  return nullptr;
}
#define EXPECT_BUILD(name, expr)                          \
  do {                                                    \
    const GoldenVec *v = gv(name);                        \
    check("golden present: " name, v != nullptr);         \
    if (v) expect_eq_bytes(name, (expr), v->bytes, v->len); \
  } while (0)

// Feed a whole inbound frame through dedup(x2, as the watch double-sends) + reassembler.
static bool route_input(const uint8_t *frame, size_t len, gtx2_proto::InputEvent &ev) {
  gtx2_proto::NotifyDedup dedup;
  gtx2_proto::Reassembler re(gtx2_proto::DIR_W2A);
  std::vector<gtx2_proto::Frame> frames;
  // The watch delivers each PDU twice; dedup must collapse the pair to one.
  for (int copy = 0; copy < 2; copy++)
    if (dedup.accept(frame, len)) re.feed(frame, len, frames);
  if (frames.size() != 1) return false;
  const gtx2_proto::Frame &f = frames[0];
  if (f.has_crc && !f.crc_ok) return false;
  ev = gtx2_proto::decode_input(f.opcode, f.payload.data(), f.payload.size());
  return ev.ok;
}

int main() {
  std::printf("gtx2_client protocol parity test\n");

  // --- CRC canonical check value (crc.py docstring: "123456789" -> 0x29B1) ---
  check_eq<uint16_t>("crc \"123456789\"", gtx2_proto::crc16_ccitt_false((const uint8_t *)"123456789", 9),
                     0x29B1);
  check_eq<uint32_t>("tz_field(-420)==1020", gtx2_proto::tz_field(-420), 1020u);
  check_eq<uint32_t>("tz_field(0)==0", gtx2_proto::tz_field(0), 0u);
  check_eq<uint32_t>("tz_field(+480)==480", gtx2_proto::tz_field(480), 480u);

  // --- builders: byte-identical to Python ---
  EXPECT_BUILD("bind", gtx2_proto::build_bind(GV_SEQ));
  EXPECT_BUILD("find_on", gtx2_proto::build_find_device(true, GV_SEQ));
  EXPECT_BUILD("find_off", gtx2_proto::build_find_device(false, GV_SEQ));
  EXPECT_BUILD("feature_bitmap", gtx2_proto::build_feature_bitmap(GV_SEQ));
  EXPECT_BUILD("state_query", gtx2_proto::build_state_query(GV_SEQ));
  EXPECT_BUILD("alarm_get", gtx2_proto::build_alarm_get(GV_SEQ));
  EXPECT_BUILD("dial_list_req", gtx2_proto::build_dial_list_request(GV_SEQ));
  EXPECT_BUILD("health_sync", gtx2_proto::build_health_sync(GV_HEALTH_CAT, 0, 0, GV_SEQ));
  EXPECT_BUILD("set_time", gtx2_proto::build_set_time(GV_ST_YEAR, GV_ST_MONTH, GV_ST_DAY, GV_ST_HOUR,
                                                GV_ST_MIN, GV_ST_SEC, GV_ST_WDAY, GV_ST_EPOCH,
                                                GV_ST_TZ, GV_SEQ));
  {
    gtx2_proto::WeatherParams w{GV_W_MONTH, GV_W_DAY,  GV_W_HOUR, GV_W_MINUTE,       GV_W_COND,
                          GV_W_CUR,   GV_W_TMAX, GV_W_TMIN, std::string(GV_W_CITY),
                          GV_W_PRESSURE_CPA};
    EXPECT_BUILD("weather", gtx2_proto::build_weather(w, GV_SEQ));
  }
  EXPECT_BUILD("alarm_set", gtx2_proto::build_alarm_set(0, GV_AL_HOUR, GV_AL_MIN, true, GV_SEQ));
  EXPECT_BUILD("dial_switch", gtx2_proto::build_dial_switch(GV_DIAL_ID, 0, 0, GV_SEQ));
  EXPECT_BUILD("dial_delete", gtx2_proto::build_dial_delete(GV_DIAL_DELETE_NAME, GV_SEQ));

  // --- inbound LE inputs: parse + CRC + route (via dedup x2 + reassembler) ---
  struct {
    const char *vec;
    const char *want_name;
    int want_detail;
  } inputs[] = {
      {"in_music_play", "music.play_pause", 1}, {"in_music_prev", "music.prev", 2},
      {"in_music_next", "music.next", 3},       {"in_find_phone", "find_phone", 0},
      {"in_find_phone4", "find_phone", 0},      {"in_records", "records_ready", 5},
  };
  for (auto &t : inputs) {
    const GoldenVec *v = gv(t.vec);
    check(t.vec, v != nullptr);
    if (!v) continue;
    gtx2_proto::InputEvent ev;
    bool ok = route_input(v->bytes, v->len, ev);
    check((std::string("route ") + t.vec).c_str(), ok && ev.name == t.want_name);
    if (t.want_detail) check_eq((std::string("detail ") + t.vec).c_str(), ev.detail, t.want_detail);
  }

  // --- dial-push (D-plane) plan: D3 -> D1 -> D2* -> D4, byte-identical to plan_dial_push ---
  {
    const int N = 500;
    std::vector<uint8_t> blob(N);
    for (int i = 0; i < N; i++) blob[i] = (uint8_t) ((i * 7 + 3) & 0xFF);  // mirrors gen_golden DIAL_BLOB
    const int CH = (int) gtx2_proto::DP_CHUNK_MAX;
    gtx2_proto::Bytes plan = gtx2_proto::build_d3_query();
    gtx2_proto::Bytes d1 = gtx2_proto::build_d1_announce("custom_id_25022.bin", (uint32_t) N);
    plan.insert(plan.end(), d1.begin(), d1.end());
    for (int off = 0; off < N; off += CH) {
      int n = (N - off < CH) ? (N - off) : CH;
      gtx2_proto::Bytes c = gtx2_proto::build_d2_chunk((uint8_t) ((off / CH) & 0xFF), blob.data() + off, n);
      plan.insert(plan.end(), c.begin(), c.end());
    }
    gtx2_proto::Bytes d4 = gtx2_proto::build_d4_finalize(gtx2_proto::crc16_xmodem(blob.data(), blob.size()));
    plan.insert(plan.end(), d4.begin(), d4.end());
    const GoldenVec *v = gv("dial_plan");
    check("dial_plan present", v != nullptr);
    if (v)
      check("dial_plan byte-parity vs plan_dial_push",
            plan.size() == v->len && std::memcmp(plan.data(), v->bytes, v->len) == 0);
  }

  // --- fragmentation round-trip: a large frame -> PDUs -> reassembled identically ---
  {
    Bytes big = gtx2_proto::build_command(0x16, Bytes(60, 0xAB), 0, 9);
    auto pdus = gtx2_proto::frame_to_pdus(big, 20);
    check("fragmentation split >1 PDU", pdus.size() > 1);
    // Reassemble as app->watch (no CRC): feed the PDUs, expect the original payload back.
    gtx2_proto::Reassembler re(gtx2_proto::DIR_A2W);
    std::vector<gtx2_proto::Frame> frames;
    for (auto &p : pdus) re.feed(p.data(), p.size(), frames);
    check("reassembled to 1 frame", frames.size() == 1);
    if (frames.size() == 1) check("reassembled payload == 60x0xAB",
                                  frames[0].payload == Bytes(60, 0xAB));
  }

  // --- health record decode: identical bytes as the Python extractors ---
  {
    gtx2_proto::ActivityData act = gtx2_proto::decode_activity(GV_rec_activity, sizeof(GV_rec_activity));
    check("activity decodes", act.ok);
    check_eq<uint32_t>("activity steps", act.steps, (uint32_t)GV_ACT_STEPS);
    check_eq<uint32_t>("activity distance", act.distance_m, (uint32_t)GV_ACT_DIST);
    check_eq<uint32_t>("activity calories", act.calories, (uint32_t)GV_ACT_CAL);
    check_eq<int>("latest HR", gtx2_proto::decode_latest_hr(GV_rec_hr, sizeof(GV_rec_hr)), GV_HR_LAST);
    check_eq<int>("latest SpO2", gtx2_proto::decode_latest_spo2(GV_rec_spo2, sizeof(GV_rec_spo2)),
                  GV_SPO2_LAST);
  }

  // --- reply decode: firmware stamp (MAC dropped) + active dial ---
  {
    check("firmware stamp",
          gtx2_proto::parse_firmware_stamp(GV_rec_state, sizeof(GV_rec_state)) == GV_FW_STAMP);
    check("active dial",
          gtx2_proto::parse_active_dial(GV_rec_diallist, sizeof(GV_rec_diallist)) == GV_ACTIVE_DIAL);
  }

  // --- base64 decode (API-direct chunk transport) ---
  {
    auto b64eq = [](const std::string &in, const Bytes &want) {
      Bytes got;
      return gtx2_proto::base64_decode(in, got) && got == want;
    };
    check("b64 'SGVsbG8=' -> Hello", b64eq("SGVsbG8=", Bytes{'H', 'e', 'l', 'l', 'o'}));
    check("b64 'TWFu' -> Man", b64eq("TWFu", Bytes{'M', 'a', 'n'}));
    check("b64 '' -> empty", b64eq("", Bytes{}));
    check("b64 tolerates newline", b64eq("SGVs\nbG8=", Bytes{'H', 'e', 'l', 'l', 'o'}));
    Bytes junk;
    check("b64 rejects invalid char", !gtx2_proto::base64_decode("****", junk));
    check("b64 rejects data-after-pad", !gtx2_proto::base64_decode("SG=x", junk));
  }

  // --- ChunkAssembler: reassemble a binary blob from in-order base64 slices ---
  {
    Bytes blob(1000);
    for (size_t i = 0; i < blob.size(); i++) blob[i] = (uint8_t) ((i * 37 + 5) & 0xFF);
    auto enc = [](const uint8_t *d, size_t n) {  // test-only base64 encoder (not shipped)
      static const char *A =
          "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
      std::string s;
      for (size_t i = 0; i < n; i += 3) {
        uint32_t v = (uint32_t) d[i] << 16;
        int have = 1;
        if (i + 1 < n) { v |= (uint32_t) d[i + 1] << 8; have = 2; }
        if (i + 2 < n) { v |= (uint32_t) d[i + 2]; have = 3; }
        s += A[(v >> 18) & 63];
        s += A[(v >> 12) & 63];
        s += have >= 2 ? A[(v >> 6) & 63] : '=';
        s += have >= 3 ? A[v & 63] : '=';
      }
      return s;
    };
    gtx2_proto::ChunkAssembler ca;
    const uint32_t DID = 25041, CH = 384;
    auto R = gtx2_proto::ChunkAssembler::Result::OK;
    uint32_t seq = 0;
    for (size_t off = 0; off < blob.size(); off += CH, seq++) {
      size_t n = (blob.size() - off < CH) ? blob.size() - off : CH;
      Bytes dec;
      gtx2_proto::base64_decode(enc(blob.data() + off, n), dec);
      R = ca.feed(DID, seq, (uint32_t) blob.size(), dec);
    }
    check("chunk reassembly COMPLETE", R == gtx2_proto::ChunkAssembler::Result::COMPLETE);
    check("chunk reassembly bytes match blob", ca.buffer() == blob);
    check("chunk reassembly dial_id preserved", ca.dial_id() == DID);
    Bytes moved = ca.take();   // 1x-peak path: move the finished buffer out, no copy
    check("chunk take() yields the blob + empties the assembler", moved == blob && ca.buffer().empty());

    gtx2_proto::ChunkAssembler e;   // error paths
    check("chunk missed-seq0 -> ERROR",
          e.feed(DID, 1, 100, Bytes(10)) == gtx2_proto::ChunkAssembler::Result::ERROR);
    check("chunk seq0 -> OK", e.feed(DID, 0, 20, Bytes(10)) == gtx2_proto::ChunkAssembler::Result::OK);
    check("chunk seq-gap -> ERROR",
          e.feed(DID, 2, 20, Bytes(10)) == gtx2_proto::ChunkAssembler::Result::ERROR);
    gtx2_proto::ChunkAssembler o;
    o.feed(DID, 0, 20, Bytes(10));
    check("chunk overflow -> ERROR",
          o.feed(DID, 1, 20, Bytes(15)) == gtx2_proto::ChunkAssembler::Result::ERROR);
    gtx2_proto::ChunkAssembler b;
    check("chunk total>MAX -> ERROR",
          b.feed(DID, 0, gtx2_proto::ChunkAssembler::MAX_TOTAL + 1, Bytes(1)) ==
              gtx2_proto::ChunkAssembler::Result::ERROR);
  }

  std::printf("\n%d passed, %d failed\n", g_pass, g_fail);
  return g_fail ? 1 : 0;
}
