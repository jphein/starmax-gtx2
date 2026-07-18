// GTX2 ESPHome node — the "super-iTag" client (docs/cfw-crown-ha.md).
//
// A ble_client child node that speaks OUR custom 0x0FF0 / C1 protocol (bind + framing + CRC via
// gtx2_protocol.*), so an ESP32-C3 becomes a room-aware GTX2 gateway exactly like JP's iTag nodes:
// the node holding the GATT link IS the room. It:
//   * binds, discovers the 0x0FF0 write/notify chars, subscribes to notifies;
//   * fires `esphome.gtx2_input {input, detail, node}` for the LE inputs (music / find-phone) so
//     HA automations bind them to lights/scenes (itag_lights.yaml pattern);
//   * polls health (activity/HR/SpO2) + device state → sensors;
//   * exposes SAFE commands (find/buzz, set-time, weather, alarm, dial-list/switch) as methods
//     callable from YAML lambdas. DANGER-tier ops (flash/dnd/music/camera/call) are NOT here.
#pragma once
#ifdef USE_ESP32

#include <esp_gattc_api.h>

#include <string>

#include "esphome/components/api/custom_api_device.h"
#include "esphome/components/ble_client/ble_client.h"
#include "esphome/core/component.h"

#include "gtx2_protocol.h"

#ifdef USE_SENSOR
#include "esphome/components/sensor/sensor.h"
#endif
#ifdef USE_BINARY_SENSOR
#include "esphome/components/binary_sensor/binary_sensor.h"
#endif
#ifdef USE_TEXT_SENSOR
#include "esphome/components/text_sensor/text_sensor.h"
#endif
#ifdef USE_TIME
#include "esphome/components/time/real_time_clock.h"
#endif

namespace esphome {
namespace gtx2_client {

namespace espbt = esphome::esp32_ble_tracker;

class GTX2Client : public Component,
                   public ble_client::BLEClientNode,
                   public api::CustomAPIDevice {
 public:
  void setup() override;
  void dump_config() override;
  float get_setup_priority() const override { return setup_priority::AFTER_BLUETOOTH; }
  void gattc_event_handler(esp_gattc_cb_event_t event, esp_gatt_if_t gattc_if,
                           esp_ble_gattc_cb_param_t *param) override;

  // --- config setters (from codegen) ---
  void set_node_name(const std::string &n) { this->node_name_ = n; }
  void set_label(const std::string &l) { this->label_ = l; }   // log-tag discriminator only
  void set_event_name(const std::string &e) { this->event_name_ = e; }
  void set_health_interval(uint32_t ms) { this->health_interval_ = ms; }
#ifdef USE_TIME
  void set_time_source(time::RealTimeClock *t) { this->time_ = t; }
#endif
#ifdef USE_SENSOR
  void set_heart_rate_sensor(sensor::Sensor *s) { this->hr_ = s; }
  void set_spo2_sensor(sensor::Sensor *s) { this->spo2_ = s; }
  void set_steps_sensor(sensor::Sensor *s) { this->steps_ = s; }
  void set_distance_sensor(sensor::Sensor *s) { this->distance_ = s; }
  void set_calories_sensor(sensor::Sensor *s) { this->calories_ = s; }
#endif
#ifdef USE_BINARY_SENSOR
  void set_connected_sensor(binary_sensor::BinarySensor *s) { this->connected_bs_ = s; }
#endif
#ifdef USE_TEXT_SENSOR
  void set_firmware_sensor(text_sensor::TextSensor *s) { this->firmware_ts_ = s; }
  void set_active_face_sensor(text_sensor::TextSensor *s) { this->active_face_ts_ = s; }
#endif

  // --- SAFE commands (call from YAML lambdas: id(gtx2)->find(true); …) ---
  void find(bool on);                             // 0x18 buzz/find
  void set_time_now();                            // 0x02 sync clock from the time source
  // 0x02 with a LIVE-VALUE encode: hour+minute = real clock; day + (optionally) month + week carry an
  // external value's digits (grid watts/kW) so a multi-digit face renders it live via a tiny command
  // (no image re-push). month/week default to the REAL calendar (backward-compat with 4-arg callers);
  // pass month>=1 / week>=0 to hijack them as extra digit carriers. (Empirical: whether the watch
  // renders a PUSHED month/week digit cleanly — vs format/clip like `date` — is under HW test.)
  void set_time_custom(int hour, int minute, int second, int day, int month = -1, int week = -1);
  void request_health();                          // 0x0e read (activity/HR/SpO2)
  void request_state();                           // 0x05 + 0x16 (firmware / active face)
  void switch_dial(uint32_t dial_id);             // 0x16 [inferred] switch active face
  // [FW] DELETE an installed face over 0x16 (operate {f1=DELETE, f2=dial_name}); re-reads the list
  // to confirm. `delete_dial(id)` targets a custom face (custom_id_<id>.bin); the by-name form
  // deletes a built-in/market face by its filename from the 0x16 list. See delete-opcode-RE.md.
  void delete_dial(uint32_t dial_id);
  void delete_dial_by_name(const std::string &dial_name);
  // [FW] ACTIVATE/SET an installed face as the displayed one over 0x16 (operate {f1=SET(1), f2=dial_name}).
  // Symmetric sibling of delete_dial (DELETE HW-proven). Install shows "well done" but never auto-switches
  // the face; this is the candidate explicit remote activate. Test on spare before trusting.
  void activate_dial(uint32_t dial_id);
  void activate_dial_by_name(const std::string &dial_name);
  // [PROBE] Same nested DialInfo payload as switch_dial, but under a caller-chosen C1 opcode + flag —
  // sweep candidates (0xED / 0x6D / 0x16+flag=1) to find the REAL activate opcode (0x16-set +
  // D4-auto are both no-op on this watch). Temporary RE aid; drop once the opcode is captured.
  void probe_switch(uint32_t dial_id, uint32_t opcode, uint32_t flag);
  void push_weather(int temp_current, int temp_max, int temp_min, int condition,
                    const std::string &city);     // 0x04 enable + 0x12 weather
  void set_alarm(int index, int hour, int minute, bool enabled);  // 0x07
  void activate();                                // 0x04 feature-enable handshake (node-side "Activate")
  bool is_connected() const { return this->connected_; }

  // [#15 dial-push — signature FINAL, body is a no-op STUB until the D-plane streaming lands]
  // Node flow: http_request GET the pre-rendered NATIVE dial container (dialfmt: not RGB565) into a
  // buffer → call this. Real impl streams the bulk plane D3 → D1 (custom_id_<dial_id>.bin) →
  // D2×N (≤234 B, PACED — fire-hosing overruns the C3 GATT queue) → D4 (crc16_xmodem over the whole
  // container); the install auto-activates on D4. Byte-ref: starmax_client dials.plan_dial_push.
  void push_dial_blob(uint32_t dial_id, const uint8_t *blob, size_t len);
  void push_dial_blob(uint32_t dial_id, const std::string &blob) {  // convenience for http_request bodies
    this->push_dial_blob(dial_id, reinterpret_cast<const uint8_t *>(blob.data()), blob.size());
  }

  // [API-direct] Reassemble a base64-chunked dial container pushed over the ESPHome native API
  // (the whole ~35 KB blob exceeds the 32 KB API message cap, so HA sends it in slices). seq==0
  // (re)starts; on the final slice (accumulated == total_len) it streams via push_dial_blob. No
  // HTTP/blobd/firewall. `total_len` is the RAW (decoded) container length.
  void push_dial_chunk(uint32_t dial_id, uint32_t seq, uint32_t total_len, const std::string &b64);

 protected:
  void send_frame_(const gtx2_proto::Bytes &frame);
  void handle_frame_(const gtx2_proto::Frame &f);
  void poll_tick_();
  uint8_t next_seq_() {
    this->seq_ = (this->seq_ >= 0xFF) ? 1 : this->seq_ + 1;
    return this->seq_;
  }
  const char *node_() const;

  // --- dial-push (D-plane bulk transfer) state machine — CLOSED-LOOP, vendor-faithful ---
  // Streams the native container over the bulk plane (D3→D1→D2*→D4) as NO_RSP Write-Commands (the
  // watch install handler ignores a Write-Request). The transfer is CLOSED-LOOP on the watch's
  // windowed acks [CAP]: the watch replies `d2 00 00 <cum_off> <running_crc16>` every DP_WINDOW (15)
  // chunks it RECEIVES. We stream one window, WAIT for that ack, and verify our own
  // crc16_xmodem(blob[0:cum_off]) == the watch's running crc; on match we advance the retransmit
  // anchor, on mismatch/timeout we RE-SEND the window from the last verified offset (dp_last_good_off_).
  // This is the fix for the real install-blocker: an open-loop stream silently loses a chunk under
  // radio contention → the watch's whole-file D4 CRC fails → the dial is discarded and never lists;
  // more bytes = more windows = geometrically worse (the size/flakiness correlation). The final partial
  // window (<15) gets NO ack — we send D4 directly and the whole-file CRC covers it (retransmit the
  // tail from the anchor if D4 doesn't verify). The 7a740e0 congestion gate is the per-chunk send floor.
  bool dp_precheck_(size_t len);                     // shared guards (connected/idle/non-empty/MTU)
  void dp_begin_(uint32_t dial_id);                  // start the D-plane from an already-filled dp_blob_
  esp_err_t dp_write_(const gtx2_proto::Bytes &frame);  // raw NO_RSP write; returns the enqueue result
  void dp_advance_();                                // send pump (paced, congestion-gated) + ack timeouts
  void dp_on_ack_(const uint8_t *pdu, size_t len);   // handle a watch D-plane ack (0xD*): verify/advance/resend
  void dp_abort_(const char *why);

  std::string node_name_;
  std::string label_;      // optional per-instance log discriminator (multi-watch node); LOG-ONLY
  std::string log_tag_;    // cached "node_name_" or "node_name_/label_"; built once in setup()
  std::string event_name_{"esphome.gtx2_input"};
  uint32_t health_interval_{300000};
  uint16_t write_handle_{0};
  uint16_t notify_handle_{0};
  uint16_t mtu_payload_{20};
  uint8_t seq_{0};
  bool connected_{false};
  int pending_cat_{-1};      // category of the in-flight 0x0e read (dispatch the reply)
  int poll_index_{0};        // round-robin over the polled categories
  // dial-push state
  // [install fix] crude per-chunk pace (ms) for the NO_RSP D-plane; ~15ms is safely slower than the
  // app's ~9ms/chunk (capture). First-pass; refine to D2-ack windowing once install is confirmed.
  static constexpr uint32_t DP_PACE_MS = 15;
  // [transport] back-pressure handling for the NO_RSP D-plane. DP_RETRY_MS re-emits a back-pressured
  // frame; DP_STALL_MS aborts a wedged / perpetually-congested stream so the dial slot frees.
  static constexpr uint32_t DP_RETRY_MS = 8;
  static constexpr uint32_t DP_STALL_MS = 12000;  // global watchdog — closed-loop adds ack RTTs + resends
  // [closed-loop] windowed-ack flow control (byte-exact from the vendor capture; see dp_on_ack_).
  static constexpr size_t DP_WINDOW = 15;              // chunks the watch acks at a time [CAP]
  static constexpr uint32_t DP_ACK_TIMEOUT_MS = 2500;  // no window/handshake ack → retransmit
  static constexpr uint8_t DP_MAX_RESENDS = 8;         // consecutive retransmits before abort
  // Send-driven states: SEND_* emit the next frame(s); WAIT_* block on the watch's D-plane ack. A
  // retransmit rewinds to dp_last_good_off_, so any resent frame is byte-identical.
  enum class DpState : uint8_t {
    IDLE,
    SEND_D3, WAIT_D3,        // probe → await d3 00 00 <staged_off>
    SEND_D1, WAIT_D1,        // announce → await d1 00 00
    SEND_WINDOW, WAIT_ACK,   // stream ≤DP_WINDOW chunks → await d2 window ack (verify running crc)
    SEND_D4, WAIT_D4,        // finalize → await d4 00 00 verify-OK
    COMPLETE                 // installed → confirm + IDLE
  };
  DpState dp_state_{DpState::IDLE};
  gtx2_proto::Bytes dp_blob_;   // the native container being streamed
  gtx2_proto::ChunkAssembler dp_chunk_;  // [API-direct] reassembles base64 slices before streaming
  uint32_t dp_dial_id_{0};
  uint16_t dp_crc_{0};          // crc16-xmodem of dp_blob_ (D4 finalize)
  size_t dp_offset_{0};         // next blob byte to send (cumulative offset streamed)
  uint8_t dp_ctr_{0};           // D2 counter (next chunk)
  bool dp_congested_{false};    // [transport] last ESP_GATTC_CONGEST_EVT state for our conn (WWR gate)
  uint32_t dp_last_progress_ms_{0};  // millis() of the last accepted frame (global stall watchdog)
  // [closed-loop] window / ack tracking
  size_t dp_win_chunks_{0};     // chunks emitted in the current window (0..DP_WINDOW)
  size_t dp_last_good_off_{0};  // last CRC-verified cumulative offset (retransmit anchor)
  uint8_t dp_resends_{0};       // consecutive retransmits of the current window
  uint32_t dp_wait_since_ms_{0};// millis() the current WAIT_* began (ack timeout)
  // [#24 quiesce-siblings] the instance currently doing a dial-push on the shared C3 radio (nullptr =
  // free). Siblings yield their poll while a push is in flight, so it doesn't contend for the radio.
  static GTX2Client *radio_push_owner_;
  gtx2_proto::Reassembler reasm_{gtx2_proto::DIR_W2A};
  gtx2_proto::NotifyDedup dedup_;
#ifdef USE_TIME
  time::RealTimeClock *time_{nullptr};
#endif
#ifdef USE_SENSOR
  sensor::Sensor *hr_{nullptr};
  sensor::Sensor *spo2_{nullptr};
  sensor::Sensor *steps_{nullptr};
  sensor::Sensor *distance_{nullptr};
  sensor::Sensor *calories_{nullptr};
#endif
#ifdef USE_BINARY_SENSOR
  binary_sensor::BinarySensor *connected_bs_{nullptr};
#endif
#ifdef USE_TEXT_SENSOR
  text_sensor::TextSensor *firmware_ts_{nullptr};
  text_sensor::TextSensor *active_face_ts_{nullptr};
#endif
};

}  // namespace gtx2_client
}  // namespace esphome
#endif  // USE_ESP32
