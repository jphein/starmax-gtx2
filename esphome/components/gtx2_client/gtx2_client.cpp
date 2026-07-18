#include "gtx2_client.h"
#ifdef USE_ESP32

#include <string>

#include <esp_gap_ble_api.h>   // [#24 link-tolerance] esp_ble_gap_update_conn_params (anti-0x08)

#include "esphome/core/application.h"
#include "esphome/core/hal.h"       // millis() (D-plane stall watchdog)
#include "esphome/core/helpers.h"   // format_hex_pretty (0x16 dial-list diag log)
#include "esphome/core/log.h"

namespace esphome {
namespace gtx2_client {

// [#24 quiesce-siblings] The ≤3 GTX2Client instances share ONE C3 radio. While one is doing a dial-
// push, the others' polls contend for connection events → dropped chunks / a starved link. This
// class-static names the instance currently pushing (nullptr = radio free); siblings yield their poll.
// Assumes one radio per node (true today: max_connections:3 on one C3). Multi-adapter → key by adapter.
GTX2Client *GTX2Client::radio_push_owner_ = nullptr;

static const char *const TAG = "gtx2_client";

// Health categories we poll (commands/base.py syncType enum).
static constexpr uint8_t CAT_ACTIVITY_HR = 0;  // intraday HR
static constexpr uint8_t CAT_SPO2 = 2;
static constexpr uint8_t CAT_ACTIVITY = 5;  // steps / distance / calories
static const uint8_t POLL_CATS[] = {CAT_ACTIVITY, CAT_ACTIVITY_HR, CAT_SPO2};
static constexpr int POLL_CATS_N = 3;

void GTX2Client::setup() {
  if (this->node_name_.empty())
    this->node_name_ = App.get_name();
  // Cache the log tag once: "node_name" alone, or "node_name/label" when a per-instance label is
  // configured. This is LOG-ONLY — the gtx2_input event still reports node_name (the HA room).
  this->log_tag_ = this->label_.empty() ? this->node_name_ : this->node_name_ + "/" + this->label_;
  if (this->health_interval_ > 0) {
    this->set_interval("gtx2_poll", this->health_interval_, [this]() { this->poll_tick_(); });
  }
}

const char *GTX2Client::node_() const {
  // Fall back to node_name_ if a log line ever fires before setup() caches log_tag_.
  return this->log_tag_.empty() ? this->node_name_.c_str() : this->log_tag_.c_str();
}

void GTX2Client::dump_config() {
  ESP_LOGCONFIG(TAG, "GTX2 client:");
  ESP_LOGCONFIG(TAG, "  node: %s", this->node_name_.c_str());
  if (!this->label_.empty())
    ESP_LOGCONFIG(TAG, "  label: %s (log tag: %s)", this->label_.c_str(), this->node_());
  ESP_LOGCONFIG(TAG, "  event: %s", this->event_name_.c_str());
  ESP_LOGCONFIG(TAG, "  health poll: %ums", this->health_interval_);
}

// =============================================================================
// BLE plumbing.
// =============================================================================
void GTX2Client::gattc_event_handler(esp_gattc_cb_event_t event, esp_gatt_if_t gattc_if,
                                     esp_ble_gattc_cb_param_t *param) {
  switch (event) {
    case ESP_GATTC_DISCONNECT_EVT: {
      this->connected_ = false;
      this->write_handle_ = 0;
      this->notify_handle_ = 0;
      this->mtu_payload_ = 20;
      this->pending_cat_ = -1;
      this->reasm_.reset();
      this->dedup_.reset();
      this->dp_congested_ = false;
      if (this->dp_state_ != DpState::IDLE) {   // abort any in-flight dial-push
        this->dp_state_ = DpState::IDLE;
        gtx2_proto::Bytes().swap(this->dp_blob_);
      }
      if (GTX2Client::radio_push_owner_ == this)   // [#24] release the shared radio if WE held it
        GTX2Client::radio_push_owner_ = nullptr;
#ifdef USE_BINARY_SENSOR
      if (this->connected_bs_ != nullptr)
        this->connected_bs_->publish_state(false);
#endif
      ESP_LOGI(TAG, "[%s] disconnected", this->node_());
      break;
    }
    case ESP_GATTC_CFG_MTU_EVT: {
      uint16_t mtu = param->cfg_mtu.mtu;
      this->mtu_payload_ = mtu > 23 ? static_cast<uint16_t>(mtu - 3) : 20;
      ESP_LOGD(TAG, "[%s] MTU=%u (payload=%u)", this->node_(), mtu, this->mtu_payload_);
      break;
    }
    case ESP_GATTC_SEARCH_CMPL_EVT: {
      auto *wr = this->parent()->get_characteristic(espbt::ESPBTUUID::from_uint16(gtx2_proto::SVC_UUID16),
                                                    espbt::ESPBTUUID::from_uint16(
                                                        gtx2_proto::CHR_WRITE_UUID16));
      auto *nt = this->parent()->get_characteristic(espbt::ESPBTUUID::from_uint16(gtx2_proto::SVC_UUID16),
                                                    espbt::ESPBTUUID::from_uint16(
                                                        gtx2_proto::CHR_NOTIFY_UUID16));
      if (wr == nullptr || nt == nullptr) {
        ESP_LOGW(TAG, "[%s] 0x0FF0 write/notify char not found — not a GTX2?", this->node_());
        break;
      }
      this->write_handle_ = wr->handle;
      this->notify_handle_ = nt->handle;
      auto status = esp_ble_gattc_register_for_notify(this->parent()->get_gattc_if(),
                                                      this->parent()->get_remote_bda(), nt->handle);
      if (status)
        ESP_LOGW(TAG, "[%s] register_for_notify failed: %d", this->node_(), status);
      break;
    }
    case ESP_GATTC_REG_FOR_NOTIFY_EVT: {
      if (param->reg_for_notify.handle != this->notify_handle_)
        break;
      if (param->reg_for_notify.status != ESP_GATT_OK) {
        ESP_LOGW(TAG, "[%s] notify registration failed: %d", this->node_(),
                 param->reg_for_notify.status);
        break;
      }
      // Notifies are live. The node no longer needs the parent's service tree.
      this->node_state = espbt::ClientState::ESTABLISHED;
      this->connected_ = true;
      this->reasm_.reset();
      this->dedup_.reset();
      ESP_LOGI(TAG, "[%s] connected + bound (wr=0x%x nt=0x%x)", this->node_(), this->write_handle_,
               this->notify_handle_);
#ifdef USE_BINARY_SENSOR
      if (this->connected_bs_ != nullptr)
        this->connected_bs_->publish_state(true);
#endif
      // [#24 link-tolerance] Request a longer supervision timeout + slave latency so a starved/coex-
      // contended connection event doesn't trip the 0x08 supervision-timeout drop — a link drop sweeps
      // the STAGED (not-yet-committed) dial off the watch entirely. Default supervision (~2s) is what
      // the 0x08 trips on one C3 juggling 3 links + WiFi coex; 6s tolerates the coex gaps. Fire-and-
      // forget (the watch may renegotiate); constraint timeout > (1+latency)*max_int*2 holds (6000>500).
      {
        esp_ble_conn_update_params_t cp{};
        const uint8_t *bda = this->parent()->get_remote_bda();
        for (int i = 0; i < 6; i++) cp.bda[i] = bda[i];
        cp.min_int = 24;   // 30 ms (1.25 ms units)
        cp.max_int = 40;   // 50 ms
        cp.latency = 4;    // may skip up to 4 events (power + missed-event tolerance)
        cp.timeout = 600;  // 6000 ms supervision (10 ms units)
        esp_err_t rc = esp_ble_gap_update_conn_params(&cp);
        if (rc != ESP_OK)
          ESP_LOGW(TAG, "[%s] conn-param update request rc=%d", this->node_(), (int) rc);
      }
      // [find-fix #17] PACE the connect-write burst. The watch ignores app commands (0x18 find
      // included) until the 0x04 feature-bitmap "enable" runs (docs/notifications.md §2). All these
      // sends are per-INSTANCE (this->send_frame_ / this->next_seq_), NO shared/static guard — but
      // WRITE_WITHOUT_RESPONSE has no flow control, so bind+state+enable fired back-to-back overrun
      // the shared C3 controller queue under two-links-on-one-radio contention, and the 2nd
      // concurrently-connecting watch's enable was DROPPED (spare cold-buzz dead; an explicit later
      // re-enable worked = a transient drop, not a permanent failure). So schedule EACH write off the
      // burst, STAGGERED per connection slot so N watches don't align, and re-send the enable once for
      // resilience vs a dropped WWR. NON-destructive + idempotent (the 0x03 profile bundle — which
      // resets profile/goals — is never sent on this path). Guards on connected_ in case the link
      // drops mid-handshake. Sized for JP's 3-watches-per-board directive.
      {
        uint32_t base = (uint32_t) (this->parent()->get_conn_id() % 8) * 80;  // per-connection offset
        this->set_timeout("gtx2_c_bind", base + 0, [this]() {
          if (this->connected_) this->send_frame_(gtx2_proto::build_bind(this->next_seq_()));
        });
        this->set_timeout("gtx2_c_state", base + 150, [this]() {
          if (this->connected_) this->request_state();
        });
        this->set_timeout("gtx2_c_enable", base + 340, [this]() {
          if (this->connected_) this->send_frame_(gtx2_proto::build_feature_bitmap(this->next_seq_()));
        });
        this->set_timeout("gtx2_c_enable_retry", base + 1000, [this]() {  // idempotent retry vs a dropped WWR
          if (this->connected_) this->send_frame_(gtx2_proto::build_feature_bitmap(this->next_seq_()));
        });
      }
      break;
    }
    case ESP_GATTC_NOTIFY_EVT: {
      if (param->notify.handle != this->notify_handle_ || param->notify.value_len == 0)
        break;
      // [closed-loop D-plane] During a push, the watch's bulk-plane acks (0xD1..0xD4) arrive on this
      // same notify char. The C1 Reassembler rejects them ("unexpected NUS channel byte") and drops
      // them — the open-loop bug. Route them to the D-plane ack handler FIRST (before dedup: dp_on_ack_
      // is idempotent to the watch's double-send — it only acts in the matching WAIT_* state and
      // ignores acks at/behind the retransmit anchor).
      if (this->dp_state_ != DpState::IDLE && param->notify.value[0] >= gtx2_proto::D1 &&
          param->notify.value[0] <= gtx2_proto::D4) {
        this->dp_on_ack_(param->notify.value, param->notify.value_len);
        break;
      }
      // Watch double-sends every PDU; dedup, then reassemble into whole C1 frames.
      if (!this->dedup_.accept(param->notify.value, param->notify.value_len))
        break;
      std::vector<gtx2_proto::Frame> frames;
      this->reasm_.feed(param->notify.value, param->notify.value_len, frames);
      for (auto &f : frames)
        this->handle_frame_(f);
      break;
    }
    case ESP_GATTC_WRITE_CHAR_EVT:
      // [install fix] The D-plane is now NO_RSP (Write-Command) + self-clocked on a DP_PACE_MS timer
      // (dp_begin_/dp_advance_) — NO_RSP writes produce no WRITE_CHAR_EVT, so nothing to drive here.
      break;
    case ESP_GATTC_CONGEST_EVT: {
      if (param->congest.conn_id != this->parent()->get_conn_id())
        break;  // a sibling watch's link on this shared C3 radio — not ours
      // [transport] Controller/L2CAP TX back-pressure. On the NO_RSP D-plane there is no ATT ack, so a
      // frame written while congested is dropped silently → holey blob → D4 CRC mismatch → install
      // rejected → face never lists. Gate the pump on it: pause on congest, resume on decongest.
      this->dp_congested_ = param->congest.congested;
      ESP_LOGD(TAG, "[%s] CONGEST %s (dp_state=%d off=%u/%u)", this->node_(),
               this->dp_congested_ ? "ON" : "off", (int) this->dp_state_,
               (unsigned) this->dp_offset_, (unsigned) this->dp_blob_.size());
      if (!this->dp_congested_ && this->dp_state_ != DpState::IDLE) {
        // decongested mid-stream → kick the pump now (named "dp_pace" timeout coalesces with the
        // pending retry timer, so this can't double-advance).
        this->set_timeout("dp_pace", 0, [this]() { this->dp_advance_(); });
      }
      break;
    }
    default:
      break;
  }
}

void GTX2Client::send_frame_(const gtx2_proto::Bytes &frame) {
  if (!this->connected_ || this->write_handle_ == 0) {
    ESP_LOGW(TAG, "[%s] send while not connected — dropped", this->node_());
    return;
  }
  // Write-path observability: surfaces the connect-burst order (bind/state/enable/…) + seq at DEBUG
  // (per the control-node design — the staggered connect sends were otherwise silent). C1 frames only.
  if (frame.size() >= 6)
    ESP_LOGD(TAG, "[%s] tx C1 op=0x%02x seq=%u len=%u", this->node_(), frame[5],
             frame[1] & 0x7F, (unsigned) frame.size());
  for (auto &pdu : gtx2_proto::frame_to_pdus(frame, this->mtu_payload_)) {
    esp_ble_gattc_write_char(this->parent()->get_gattc_if(), this->parent()->get_conn_id(),
                             this->write_handle_, static_cast<uint16_t>(pdu.size()),
                             const_cast<uint8_t *>(pdu.data()), ESP_GATT_WRITE_TYPE_NO_RSP,
                             ESP_GATT_AUTH_REQ_NONE);
  }
}

// =============================================================================
// Inbound routing.
// =============================================================================
void GTX2Client::handle_frame_(const gtx2_proto::Frame &f) {
  if (f.has_crc && !f.crc_ok) {
    ESP_LOGW(TAG, "[%s] dropping frame op=0x%02x with bad CRC", this->node_(), f.opcode);
    return;
  }
  switch (f.opcode) {
    case gtx2_proto::OP_CONTROL_INPUT: {  // 0x10 — music / find-phone LE input (internal opcode RE)
      gtx2_proto::InputEvent ev = gtx2_proto::decode_input(f.opcode, f.payload.data(), f.payload.size());
      if (!ev.ok)
        break;
      ESP_LOGI(TAG, "[%s] input: %s (detail=%d)", this->node_(), ev.name.c_str(), ev.detail);
      this->fire_homeassistant_event(this->event_name_,
                                     {{"input", ev.name},
                                      {"detail", std::to_string(ev.detail)},
                                      {"node", this->node_name_}});
      break;
    }
    case gtx2_proto::OP_HEALTH_SYNC: {  // 0x0e — binary health record; dispatch by the pending category
      const uint8_t *d = f.payload.data();
      size_t n = f.payload.size();
      int cat = this->pending_cat_;
      this->pending_cat_ = -1;
      if (cat == CAT_ACTIVITY) {
        gtx2_proto::ActivityData a = gtx2_proto::decode_activity(d, n);
        if (a.ok) {
          ESP_LOGD(TAG, "[%s] activity steps=%u dist=%um cal=%u", this->node_(), a.steps,
                   a.distance_m, a.calories);
#ifdef USE_SENSOR
          if (this->steps_ != nullptr)
            this->steps_->publish_state(a.steps);
          if (this->distance_ != nullptr)
            this->distance_->publish_state(a.distance_m);
          if (this->calories_ != nullptr)
            this->calories_->publish_state(a.calories);
#endif
        }
      } else if (cat == CAT_ACTIVITY_HR) {
        int hr = gtx2_proto::decode_latest_hr(d, n);
#ifdef USE_SENSOR
        if (hr >= 0 && this->hr_ != nullptr)
          this->hr_->publish_state(hr);
#endif
      } else if (cat == CAT_SPO2) {
        int spo2 = gtx2_proto::decode_latest_spo2(d, n);
#ifdef USE_SENSOR
        if (spo2 >= 0 && this->spo2_ != nullptr)
          this->spo2_->publish_state(spo2);
#endif
      }
      break;
    }
    case gtx2_proto::OP_DEVICE_STATE: {  // 0x05 — firmware build stamp (MAC dropped in the parser)
#ifdef USE_TEXT_SENSOR
      std::string fw = gtx2_proto::parse_firmware_stamp(f.payload.data(), f.payload.size());
      if (!fw.empty() && this->firmware_ts_ != nullptr)
        this->firmware_ts_->publish_state(fw);
#endif
      break;
    }
    case gtx2_proto::OP_DIAL_LIST: {  // 0x16 — dial-list reply (active face + [diag] full installed list)
      // [install diag] log the RAW 0x16 reply so a post-push read shows the FULL installed list —
      // is the pushed dial IN it (installed) or absent (rejected at install-validation)?
      // CHUNKED hex: a full reply is ~400 B, but the single-line log buffer truncates at ~160 B —
      // that truncation caused a real misread (only the first ~5 of ~13 entries were seen). Logging
      // in slices makes the WHOLE list + capacity fields visible. (Authoritative full-list read is
      // still the Python `starmax_client dial-list`, which reassembles + parses the entire reply.)
      const size_t total = f.payload.size();
      ESP_LOGI(TAG, "[%s] 0x16 dial-list reply: %u B (full hex in 64B chunks)", this->node_(),
               (unsigned) total);
      for (size_t off = 0; off < total; off += 64) {
        const size_t n = (total - off) < 64 ? (total - off) : 64;
        ESP_LOGI(TAG, "[%s]   0x16[%u..%u]: %s", this->node_(), (unsigned) off, (unsigned) (off + n),
                 format_hex_pretty(f.payload.data() + off, n).c_str());
      }
#ifdef USE_TEXT_SENSOR
      std::string face = gtx2_proto::parse_active_dial(f.payload.data(), f.payload.size());
      if (!face.empty() && this->active_face_ts_ != nullptr)
        this->active_face_ts_->publish_state(face);
#endif
      break;
    }
    default:
      ESP_LOGV(TAG, "[%s] unhandled frame op=0x%02x len=%u", this->node_(), f.opcode,
               (unsigned) f.payload.size());
      break;
  }
}

void GTX2Client::poll_tick_() {
  if (!this->connected_)
    return;
  if (GTX2Client::radio_push_owner_ != nullptr && GTX2Client::radio_push_owner_ != this)
    return;   // [#24 quiesce] a sibling instance is pushing on the shared radio — yield this poll
  if (this->dp_state_ != DpState::IDLE)   // don't inject a C1 poll write into a dial-push stream
    return;
  uint8_t cat = POLL_CATS[this->poll_index_ % POLL_CATS_N];
  this->poll_index_++;
  this->pending_cat_ = cat;
  this->send_frame_(gtx2_proto::build_health_sync(cat, 0, 0, this->next_seq_()));
}

// =============================================================================
// SAFE command surface (callable from YAML lambdas).
// =============================================================================
void GTX2Client::find(bool on) {
  ESP_LOGI(TAG, "[%s] find/buzz %s", this->node_(), on ? "start" : "stop");
  this->send_frame_(gtx2_proto::build_find_device(on, this->next_seq_()));
}

void GTX2Client::activate() {
  // Node-side "Activate" (the dashboard button had no node path). Re-runs the NON-destructive
  // feature-enable: set-time (when a valid source exists) + the 0x04 feature bitmap. Deliberately
  // omits the 0x03 profile bundle — that writes a default profile, resetting the watch's
  // profile/goals (docs/notifications.md §2); keep it opt-in, never on an auto path.
  ESP_LOGI(TAG, "[%s] activate: feature-enable handshake", this->node_());
#ifdef USE_TIME
  if (this->time_ != nullptr && this->time_->now().is_valid())
    this->set_time_now();
#endif
  this->send_frame_(gtx2_proto::build_feature_bitmap(this->next_seq_()));
}

void GTX2Client::request_health() { this->poll_tick_(); }

void GTX2Client::request_state() {
  this->send_frame_(gtx2_proto::build_state_query(this->next_seq_()));
  this->send_frame_(gtx2_proto::build_dial_list_request(this->next_seq_()));
}

void GTX2Client::switch_dial(uint32_t dial_id) {
  ESP_LOGI(TAG, "[%s] switch dial -> %u", this->node_(), dial_id);
  this->send_frame_(gtx2_proto::build_dial_switch(dial_id, 0, 0, this->next_seq_()));
}

void GTX2Client::delete_dial(uint32_t dial_id) {
  // Custom faces live on the watch as custom_id_<id>.bin (same wire name push uses).
  this->delete_dial_by_name("custom_id_" + std::to_string(dial_id) + ".bin");
}

void GTX2Client::delete_dial_by_name(const std::string &dial_name) {
  // [FW] DELETE by filename over 0x16 (operate {f1=DELETE(2), f2=dial_name}). Byte-exact from the
  // firmware protobuf-c tables (delete-opcode-RE.md) — the vendor app has no delete, so this is
  // NOT yet live-captured. We fire the DELETE, then re-read the 0x16 list. The active-face text
  // sensor updates from the reply; for AUTHORITATIVE removal confirmation of an arbitrary face use
  // the Python `starmax_client dial-delete` (its confirm re-reads + parses the FULL list) — the
  // node's diag log is chunked but the node does not parse the whole inventory.
  ESP_LOGI(TAG, "[%s] delete dial -> %s [FW opcode]", this->node_(), dial_name.c_str());
  this->send_frame_(gtx2_proto::build_dial_delete(dial_name, this->next_seq_()));
  this->send_frame_(gtx2_proto::build_dial_list_request(this->next_seq_()));
}

void GTX2Client::activate_dial(uint32_t dial_id) {
  // Custom faces live on the watch as custom_id_<id>.bin (the wire name push installs).
  this->activate_dial_by_name("custom_id_" + std::to_string(dial_id) + ".bin");
}

void GTX2Client::activate_dial_by_name(const std::string &dial_name) {
  // [FW] SET the named face as the ACTIVE/displayed one over 0x16 (operate {f1=SET(1), f2=dial_name}) —
  // the byte-symmetric sibling of delete_dial_by_name (DELETE(2) is HW-proven). The watch's own
  // auto-activate-on-install is a no-op (install shows the "well done" success screen but never switches
  // the face or lists it in the carousel), so this explicit SET is the candidate remote "show this face".
  // Re-reads the 0x16 list after. [FW]-derived, not yet live-captured — test on the spare first.
  ESP_LOGI(TAG, "[%s] activate dial -> %s [FW SET opcode]", this->node_(), dial_name.c_str());
  this->send_frame_(gtx2_proto::build_dial_activate(dial_name, this->next_seq_()));
  this->send_frame_(gtx2_proto::build_dial_list_request(this->next_seq_()));
}

void GTX2Client::probe_switch(uint32_t dial_id, uint32_t opcode, uint32_t flag) {
  // [PROBE] Build the SAME nested DialInfo payload build_dial_switch uses, but send it under a
  // caller-chosen C1 opcode + flag. The 0x16-set and D4-auto-activate paths are both no-ops on this
  // watch (uncaptured guesses), so sweep 0xED / 0x6D / 0x16+flag=1 from HA to find the real activate
  // opcode; read the active-face text sensor after each. Temporary RE aid.
  if (!this->connected_ || this->write_handle_ == 0) {
    ESP_LOGW(TAG, "[%s] probe_switch: not connected — dropped", this->node_());
    return;
  }
  gtx2_proto::PbWriter info;
  info.varint(1, 1).varint(2, dial_id).varint(3, 0).varint(4, 0);   // {isSelected=1, dialId, color=0, align=0}
  gtx2_proto::PbWriter w;
  w.varint(1, 2).message(2, info.data());                            // outer {f1=2, f2=DialInfo}
  gtx2_proto::Bytes frame =
      gtx2_proto::build_command(static_cast<uint8_t>(opcode), w.data(),
                                static_cast<uint8_t>(flag), this->next_seq_());
  ESP_LOGI(TAG, "[%s] probe_switch: dial=%u opcode=0x%02x flag=%u (%u-byte C1)", this->node_(),
           dial_id, static_cast<unsigned>(opcode & 0xFF), static_cast<unsigned>(flag),
           static_cast<unsigned>(frame.size()));
  this->send_frame_(frame);
}

void GTX2Client::set_alarm(int index, int hour, int minute, bool enabled) {
  this->send_frame_(gtx2_proto::build_alarm_set(index, hour, minute, enabled, this->next_seq_()));
}

#ifdef USE_TIME
// Days since the Unix epoch for a civil date (Howard Hinnant's algorithm) — lets us derive the
// UTC offset (set-time f9) from the local broken-down time + the UTC timestamp, host-independent.
static int64_t days_from_civil(int y, unsigned m, unsigned d) {
  y -= m <= 2;
  const int64_t era = (y >= 0 ? y : y - 399) / 400;
  const unsigned yoe = static_cast<unsigned>(y - era * 400);
  const unsigned doy = (153 * (m + (m > 2 ? -3 : 9)) + 2) / 5 + d - 1;
  const unsigned doe = yoe * 365 + yoe / 4 - yoe / 100 + doy;
  return era * 146097 + static_cast<int>(doe) - 719468;
}
#endif

void GTX2Client::set_time_now() {
#ifdef USE_TIME
  if (this->time_ == nullptr) {
    ESP_LOGW(TAG, "[%s] set-time: no time source configured", this->node_());
    return;
  }
  ESPTime t = this->time_->now();
  if (!t.is_valid()) {
    ESP_LOGW(TAG, "[%s] set-time: time not yet valid", this->node_());
    return;
  }
  int weekday_mon0 = (t.day_of_week + 5) % 7;  // ESPTime: 1=Sun..7=Sat -> Python Monday=0
  int64_t local_as_utc = days_from_civil(t.year, t.month, t.day_of_month) * 86400LL +
                         t.hour * 3600 + t.minute * 60 + t.second;
  int offset_min = static_cast<int>((local_as_utc - static_cast<int64_t>(t.timestamp)) / 60);
  this->send_frame_(gtx2_proto::build_set_time(t.year, t.month, t.day_of_month, t.hour, t.minute,
                                         t.second, weekday_mon0,
                                         static_cast<uint64_t>(t.timestamp),
                                         gtx2_proto::tz_field(offset_min), this->next_seq_()));
  ESP_LOGI(TAG, "[%s] set-time synced", this->node_());
#else
  ESP_LOGW(TAG, "[%s] set-time: build without `time:` — unavailable", this->node_());
#endif
}

// Live-value clock: hour+minute stay the REAL time (so the watch still tells time); `day` — and,
// optionally, `month` + `week` — carry an external value's digits (grid watts/kW) → a multi-digit face
// renders it and the watch redraws it, no image re-push. month/week default to the REAL calendar
// (month<1 / week<0 sentinels → backward-compat with the 4-arg integer-kW driver); pass explicit
// month>=1 (1-12) / week>=0 (weekday, Mon=0) to hijack them as extra carriers. The epoch is computed
// FROM the synthetic calendar fields (year + the month/day we actually send) so the RTC and the widgets
// agree — else an RTC seeded from the real epoch would overwrite the carriers on the next tick.
// ⚠️ EMPIRICAL: whether the watch renders a PUSHED month/week as our bare digit (vs formatting/clipping
// like `date`) — and whether it honours the explicit weekday vs re-deriving it from the epoch date — is
// under HW test (re-push the diagnostic face, feed distinct day/month/week, read on glass) before any
// multi-digit face commits to these carriers.
void GTX2Client::set_time_custom(int hour, int minute, int second, int day, int month, int week) {
#ifdef USE_TIME
  if (this->time_ == nullptr) {
    ESP_LOGW(TAG, "[%s] set-time-custom: no time source configured", this->node_());
    return;
  }
  ESPTime t = this->time_->now();
  if (!t.is_valid()) {
    ESP_LOGW(TAG, "[%s] set-time-custom: time not yet valid", this->node_());
    return;
  }
  int use_month = (month >= 1) ? month : t.month;                    // synthetic month carrier, or real
  int use_weekday = (week >= 0) ? week : ((t.day_of_week + 5) % 7);   // synthetic weekday carrier (Mon=0), or real
  // tz offset from the real, valid sample (same derivation as set_time_now):
  int64_t real_local_as_utc = days_from_civil(t.year, t.month, t.day_of_month) * 86400LL +
                              t.hour * 3600 + t.minute * 60 + t.second;
  int offset_min = static_cast<int>((real_local_as_utc - static_cast<int64_t>(t.timestamp)) / 60);
  // epoch consistent with the SYNTHETIC calendar fields (real year, sent month/day + hour/min/sec):
  int64_t synth_local_as_utc = days_from_civil(t.year, use_month, day) * 86400LL +
                               hour * 3600 + minute * 60 + second;
  uint64_t epoch = static_cast<uint64_t>(synth_local_as_utc - static_cast<int64_t>(offset_min) * 60);
  this->send_frame_(gtx2_proto::build_set_time(t.year, use_month, day, hour, minute, second, use_weekday,
                                         epoch, gtx2_proto::tz_field(offset_min), this->next_seq_()));
  ESP_LOGI(TAG, "[%s] set-time-custom: %02d:%02d day=%d month=%d week=%d sec=%d (live-value encode)",
           this->node_(), hour, minute, day, use_month, use_weekday, second);
#else
  ESP_LOGW(TAG, "[%s] set-time-custom: build without `time:` — unavailable", this->node_());
#endif
}

void GTX2Client::push_weather(int temp_current, int temp_max, int temp_min, int condition,
                              const std::string &city) {
  gtx2_proto::WeatherParams w{};
  w.month = w.day = w.hour = w.minute = 0;
#ifdef USE_TIME
  if (this->time_ != nullptr) {
    ESPTime t = this->time_->now();
    if (t.is_valid()) {
      w.month = t.month;
      w.day = t.day_of_month;
      w.hour = t.hour;
      w.minute = t.minute;
    }
  }
#endif
  w.condition = condition;
  w.temp_current = temp_current;
  w.temp_max = temp_max;
  w.temp_min = temp_min;
  w.city = city;
  w.pressure_cpa = 101325;  // 1013.25 hPa default (matches the bridge)
  this->send_frame_(gtx2_proto::build_feature_bitmap(this->next_seq_()));  // 0x04 display-enable gate
  this->send_frame_(gtx2_proto::build_weather(w, this->next_seq_()));      // 0x12
  ESP_LOGI(TAG, "[%s] weather push: %s %dC (hi %d lo %d cond %d)", this->node_(), city.c_str(),
           temp_current, temp_max, temp_min, condition);
}

bool GTX2Client::dp_precheck_(size_t len) {
  // Shared guards for both dial-push entry points (http_request blob + API-direct chunk reassembly).
  if (!this->connected_ || this->write_handle_ == 0) {
    ESP_LOGW(TAG, "[%s] dial-push: not connected — dropped", this->node_());
    return false;
  }
  if (this->dp_state_ != DpState::IDLE) {
    ESP_LOGW(TAG, "[%s] dial-push: a transfer is already in flight — dropped", this->node_());
    return false;
  }
  if (len == 0) {
    ESP_LOGW(TAG, "[%s] dial-push: empty blob — dropped", this->node_());
    return false;
  }
  // Raw D-plane frames must each fit ONE ATT PDU (no C1 fragmentation); the largest is a full D2
  // chunk (2 + 234). A short MTU would silently fragment + corrupt the stream — refuse instead.
  if (gtx2_proto::DP_CHUNK_MAX + 2 > this->mtu_payload_) {
    ESP_LOGW(TAG, "[%s] dial-push: MTU payload %u < D2 frame %u — raise MTU first", this->node_(),
             this->mtu_payload_, (unsigned) (gtx2_proto::DP_CHUNK_MAX + 2));
    return false;
  }
  return true;
}

void GTX2Client::dp_begin_(uint32_t dial_id) {
  // Start the CLOSED-LOOP bulk-plane stream from the ALREADY-populated dp_blob_ (D3→D1→windowed
  // D2*→D4; each window verified against the watch's running-CRC ack). Caller filled dp_blob_ +
  // passed dp_precheck_(). byte-ref: starmax_client dials.plan_dial_push + [CAP] window acks.
  this->dp_dial_id_ = dial_id;
  this->dp_crc_ = gtx2_proto::crc16_xmodem(this->dp_blob_.data(), this->dp_blob_.size());
  this->dp_offset_ = 0;
  this->dp_ctr_ = 0;
  this->dp_win_chunks_ = 0;
  this->dp_last_good_off_ = 0;
  this->dp_resends_ = 0;
  this->dp_congested_ = false;
  this->dp_last_progress_ms_ = millis();
  GTX2Client::radio_push_owner_ = this;   // [#24 quiesce] claim the shared radio for this push
  this->dp_state_ = DpState::SEND_D3;
  ESP_LOGI(TAG, "[%s] dial-push start: dial=%u %u bytes crc16=0x%04x (closed-loop, window=%u)",
           this->node_(), dial_id, (unsigned) this->dp_blob_.size(), this->dp_crc_,
           (unsigned) DP_WINDOW);
  this->set_timeout("dp_pace", 0, [this]() { this->dp_advance_(); });
}

void GTX2Client::push_dial_blob(uint32_t dial_id, const uint8_t *blob, size_t len) {
  // http_request path: the off-node-rendered native container arrives as one buffer — copy it into
  // dp_blob_ and stream verbatim (no re-encode).
  if (blob == nullptr || !this->dp_precheck_(len)) return;
  this->dp_blob_.assign(blob, blob + len);
  this->dp_begin_(dial_id);
}

void GTX2Client::push_dial_chunk(uint32_t dial_id, uint32_t seq, uint32_t total_len,
                                 const std::string &b64) {
  // API-direct ingest: HA pushes the off-node-rendered container in base64 slices (the whole blob
  // exceeds the 32 KB API message cap). Accumulate in order, then MOVE the finished buffer into
  // dp_blob_ (O(1), no copy) and stream. Peak RAM = ONE blob + a per-chunk decode temp (~1x, not
  // 2x) AND no mbedTLS — unlike the http_request path that OOM'd this node (std::bad_alloc on fetch).
  if (this->dp_state_ != DpState::IDLE) {   // a previous blob is still streaming to the watch
    ESP_LOGW(TAG, "[%s] dial-chunk: a push is streaming — dropped (seq=%u)", this->node_(),
             (unsigned) seq);
    return;
  }
  gtx2_proto::Bytes chunk;
  if (!gtx2_proto::base64_decode(b64, chunk)) {
    ESP_LOGW(TAG, "[%s] dial-chunk: bad base64 (seq=%u) — reset; resend from seq 0", this->node_(),
             (unsigned) seq);
    this->dp_chunk_.reset();
    return;
  }
  auto r = this->dp_chunk_.feed(dial_id, seq, total_len, chunk);
  if (r == gtx2_proto::ChunkAssembler::Result::ERROR) {
    ESP_LOGW(TAG, "[%s] dial-chunk: protocol error (dial=%u seq=%u total=%u) — reset; resend from 0",
             this->node_(), dial_id, (unsigned) seq, (unsigned) total_len);
    return;
  }
  if (r == gtx2_proto::ChunkAssembler::Result::COMPLETE) {
    size_t n = this->dp_chunk_.buffer().size();
    if (!this->dp_precheck_(n)) { this->dp_chunk_.reset(); return; }
    ESP_LOGI(TAG, "[%s] dial-chunk: reassembled %u bytes (dial=%u) — streaming", this->node_(),
             (unsigned) n, dial_id);
    this->dp_blob_ = this->dp_chunk_.take();   // MOVE (O(1)) — never two copies of the blob resident
    this->dp_begin_(dial_id);
  } else {
    ESP_LOGD(TAG, "[%s] dial-chunk: seq=%u ok (%u/%u)", this->node_(), (unsigned) seq,
             (unsigned) this->dp_chunk_.buffer().size(), (unsigned) total_len);
  }
}

esp_err_t GTX2Client::dp_write_(const gtx2_proto::Bytes &frame) {
  // Raw NO_RSP Write-Command (0x52) — matches the app/capture; bytes enter the watch's file-install
  // state machine (a Write-Request is ATT-acked but the install handler ignores it → dial never
  // installs). Returns the ENQUEUE result: ESP_OK means the frame was handed to the BT task, NOT that
  // it left the radio (a Write-Command has no ATT ack). The caller (dp_advance_) treats a non-OK
  // return as btc-mailbox back-pressure and retries the same frame; silent on-air drops are caught
  // separately by the ESP_GATTC_CONGEST_EVT gate. Connectivity/MTU are guarded by dp_advance_ +
  // dp_precheck_, so this is a pure write. D-plane progress at DEBUG makes a stall visible per-chunk.
  ESP_LOGD(TAG, "[%s] dp tx 0x%02x off=%u/%u len=%u", this->node_(), frame.empty() ? 0 : frame[0],
           (unsigned) this->dp_offset_, (unsigned) this->dp_blob_.size(), (unsigned) frame.size());
  return esp_ble_gattc_write_char(this->parent()->get_gattc_if(), this->parent()->get_conn_id(),
                                  this->write_handle_, static_cast<uint16_t>(frame.size()),
                                  const_cast<uint8_t *>(frame.data()), ESP_GATT_WRITE_TYPE_NO_RSP,
                                  ESP_GATT_AUTH_REQ_NONE);
}

void GTX2Client::dp_abort_(const char *why) {
  ESP_LOGW(TAG, "[%s] dial-push aborted: %s (dial=%u, %u/%u bytes)", this->node_(), why,
           this->dp_dial_id_, (unsigned) this->dp_offset_, (unsigned) this->dp_blob_.size());
  this->dp_state_ = DpState::IDLE;
  gtx2_proto::Bytes().swap(this->dp_blob_);  // free the buffer
  if (GTX2Client::radio_push_owner_ == this)   // [#24] release the shared radio
    GTX2Client::radio_push_owner_ = nullptr;
}

void GTX2Client::dp_advance_() {
  if (this->dp_state_ == DpState::IDLE)
    return;
  if (!this->connected_ || this->write_handle_ == 0) { this->dp_abort_("link down"); return; }
  // Global stall watchdog: no forward progress at all for DP_STALL_MS → the link is wedged; abort so
  // the dial slot frees.
  if (millis() - this->dp_last_progress_ms_ > DP_STALL_MS) {
    this->dp_abort_("stalled (no forward progress)");
    return;
  }

  // ---- WAIT_* states: block on the watch's D-plane ack (delivered async via dp_on_ack_); here we
  // only enforce the ack timeout, then keep polling. A lost ack/window → retransmit from the anchor.
  switch (this->dp_state_) {
    case DpState::WAIT_D3:
    case DpState::WAIT_D1:
    case DpState::WAIT_ACK:
    case DpState::WAIT_D4:
      if (millis() - this->dp_wait_since_ms_ >= DP_ACK_TIMEOUT_MS) {
        if (this->dp_state_ == DpState::WAIT_D3) {
          ESP_LOGW(TAG, "[%s] dp: D3 ack timeout — resend probe", this->node_());
          this->dp_state_ = DpState::SEND_D3;
        } else if (this->dp_state_ == DpState::WAIT_D1) {
          ESP_LOGW(TAG, "[%s] dp: D1 ack timeout — resend announce", this->node_());
          this->dp_state_ = DpState::SEND_D1;
        } else {  // WAIT_ACK / WAIT_D4 → rewind to the last verified offset + re-stream the window
          if (++this->dp_resends_ > DP_MAX_RESENDS) {
            this->dp_abort_("too many retransmits (watch not acking)");
            return;
          }
          ESP_LOGW(TAG, "[%s] dp: %s timeout — retransmit from anchor %u (resend %u/%u)",
                   this->node_(), this->dp_state_ == DpState::WAIT_D4 ? "D4" : "window",
                   (unsigned) this->dp_last_good_off_, this->dp_resends_, DP_MAX_RESENDS);
          this->dp_offset_ = this->dp_last_good_off_;
          this->dp_ctr_ = (uint8_t) ((this->dp_last_good_off_ / gtx2_proto::DP_CHUNK_MAX) & 0xFF);
          this->dp_win_chunks_ = 0;
          this->dp_state_ = DpState::SEND_WINDOW;
        }
      }
      this->set_timeout("dp_pace", DP_PACE_MS, [this]() { this->dp_advance_(); });
      return;
    default:
      break;
  }

  // ---- SEND_* states: build the next frame, congestion-gate, write, then commit + arm any ack wait.
  gtx2_proto::Bytes frame;
  size_t chunk_n = 0;
  switch (this->dp_state_) {
    case DpState::SEND_D3:
      frame = gtx2_proto::build_d3_query();
      break;
    case DpState::SEND_D1: {
      std::string name = "custom_id_" + std::to_string(this->dp_dial_id_) + ".bin";
      frame = gtx2_proto::build_d1_announce(name, (uint32_t) this->dp_blob_.size());
      break;
    }
    case DpState::SEND_WINDOW:
      if (this->dp_offset_ < this->dp_blob_.size() && this->dp_win_chunks_ < DP_WINDOW) {
        size_t remaining = this->dp_blob_.size() - this->dp_offset_;
        chunk_n = remaining < gtx2_proto::DP_CHUNK_MAX ? remaining : gtx2_proto::DP_CHUNK_MAX;
        // ctr is offset-derived (= chunk index & 0xFF), so a retransmit REGRESSES the ctr to the
        // window start — the watch repositions its write pointer to the anchor and overwrites (the
        // vendor's proven resume, AGPS capture). Do NOT commit offset/ctr until the stack accepts.
        frame = gtx2_proto::build_d2_chunk(this->dp_ctr_, this->dp_blob_.data() + this->dp_offset_,
                                           chunk_n);
      } else {   // blob drained (final partial <DP_WINDOW handled at commit) → finalize
        this->dp_state_ = DpState::SEND_D4;
        this->set_timeout("dp_pace", 0, [this]() { this->dp_advance_(); });
        return;
      }
      break;
    case DpState::SEND_D4:
      frame = gtx2_proto::build_d4_finalize(this->dp_crc_);
      break;
    case DpState::COMPLETE:   // D4 verified by the watch → installed + active immediately [CAP]
      ESP_LOGI(TAG, "[%s] dial-push complete: dial=%u %u bytes INSTALLED (crc16=0x%04x)",
               this->node_(), this->dp_dial_id_, (unsigned) this->dp_blob_.size(), this->dp_crc_);
      this->dp_state_ = DpState::IDLE;
      gtx2_proto::Bytes().swap(this->dp_blob_);
      if (GTX2Client::radio_push_owner_ == this)   // [#24] release the shared radio
        GTX2Client::radio_push_owner_ = nullptr;
      this->set_timeout("gtx2_dp_confirm", 800, [this]() {
        if (this->connected_) this->request_state();
      });
      return;
    default:
      return;
  }

  // GATE: while congested, a Write-Command would be dropped silently — hold + retry the SAME frame
  // (no commit). The decongest CONGEST_EVT also kicks "dp_pace" (coalesced). [7a740e0]
  if (this->dp_congested_) {
    ESP_LOGV(TAG, "[%s] dp hold — congested (off=%u/%u)", this->node_(),
             (unsigned) this->dp_offset_, (unsigned) this->dp_blob_.size());
    this->set_timeout("dp_pace", DP_RETRY_MS, [this]() { this->dp_advance_(); });
    return;
  }
  esp_err_t err = this->dp_write_(frame);
  if (err != ESP_OK) {   // btc mailbox saturated — retry the same frame (no commit)
    ESP_LOGW(TAG, "[%s] dp write rc=%d — back-pressure, retry (off=%u/%u)", this->node_(), (int) err,
             (unsigned) this->dp_offset_, (unsigned) this->dp_blob_.size());
    this->set_timeout("dp_pace", DP_RETRY_MS, [this]() { this->dp_advance_(); });
    return;
  }

  // COMMIT — the stack accepted the frame; advance state + arm the ack wait where the watch replies.
  this->dp_last_progress_ms_ = millis();
  switch (this->dp_state_) {
    case DpState::SEND_D3:
      this->dp_state_ = DpState::WAIT_D3;
      this->dp_wait_since_ms_ = millis();
      break;
    case DpState::SEND_D1:
      this->dp_state_ = DpState::WAIT_D1;
      this->dp_wait_since_ms_ = millis();
      break;
    case DpState::SEND_WINDOW:
      this->dp_offset_ += chunk_n;
      this->dp_ctr_++;
      this->dp_win_chunks_++;
      if (this->dp_win_chunks_ >= DP_WINDOW) {          // full window → watch acks it (verify on arrival)
        this->dp_state_ = DpState::WAIT_ACK;
        this->dp_wait_since_ms_ = millis();
      } else if (this->dp_offset_ >= this->dp_blob_.size()) {  // final PARTIAL window → no ack; D4 covers it
        this->dp_state_ = DpState::SEND_D4;
      }
      // else: stay SEND_WINDOW, emit the next chunk
      break;
    case DpState::SEND_D4:
      this->dp_state_ = DpState::WAIT_D4;
      this->dp_wait_since_ms_ = millis();
      break;
    default:
      break;
  }
  this->set_timeout("dp_pace", DP_PACE_MS, [this]() { this->dp_advance_(); });
}

void GTX2Client::dp_on_ack_(const uint8_t *pdu, size_t len) {
  // Handle a watch->app bulk-plane ack (routed here from ESP_GATTC_NOTIFY_EVT during a push). Only
  // acts in the matching WAIT_* state, so the watch's double-sent notify + any stale ack are ignored.
  gtx2_proto::DpAck ack;
  if (!gtx2_proto::parse_dp_ack(pdu, len, ack))
    return;
  switch (ack.kind) {
    case gtx2_proto::D3:
      if (this->dp_state_ != DpState::WAIT_D3)
        return;
      // staged_off (ack.off): 0 = fresh. We always stream fresh from 0 — a re-announce (D1) resets the
      // watch's staged buffer, and our offset-derived ctr overwrites any stale partial on regression;
      // either way a from-0 stream self-corrects. staged_off is logged for diagnostics only.
      ESP_LOGI(TAG, "[%s] dp: D3 ack (staged_off=%u) → announce", this->node_(), (unsigned) ack.off);
      this->dp_state_ = DpState::SEND_D1;
      this->set_timeout("dp_pace", 0, [this]() { this->dp_advance_(); });
      break;
    case gtx2_proto::D1:
      if (this->dp_state_ != DpState::WAIT_D1)
        return;
      ESP_LOGD(TAG, "[%s] dp: D1 ack → stream first window", this->node_());
      this->dp_win_chunks_ = 0;
      this->dp_state_ = DpState::SEND_WINDOW;
      this->set_timeout("dp_pace", 0, [this]() { this->dp_advance_(); });
      break;
    case gtx2_proto::D2: {
      if (this->dp_state_ != DpState::WAIT_ACK || !ack.has_fields)
        return;
      if (ack.off <= this->dp_last_good_off_)   // stale / duplicate window ack (already verified)
        return;
      if (ack.off > this->dp_blob_.size())      // bogus offset — never index past the blob
        return;
      const uint16_t want = gtx2_proto::crc16_xmodem(this->dp_blob_.data(), ack.off);
      const bool good = (ack.off == this->dp_offset_) && ((ack.val & 0xFFFF) == want);
      if (good) {
        this->dp_last_good_off_ = ack.off;   // advance the retransmit anchor
        this->dp_resends_ = 0;
        this->dp_last_progress_ms_ = millis();
        ESP_LOGD(TAG, "[%s] dp: window ack OK off=%u crc=0x%04x", this->node_(), (unsigned) ack.off,
                 want);
        if (this->dp_offset_ >= this->dp_blob_.size()) {
          this->dp_state_ = DpState::SEND_D4;      // all chunks acked → finalize
        } else {
          this->dp_win_chunks_ = 0;
          this->dp_state_ = DpState::SEND_WINDOW;   // next window
        }
      } else {
        // window CRC/offset mismatch (a chunk was lost/corrupted in this window) → retransmit from
        // the anchor. The offset-derived ctr regresses, so the watch overwrites from dp_last_good_off_.
        if (++this->dp_resends_ > DP_MAX_RESENDS) {
          this->dp_abort_("too many retransmits (window crc mismatch)");
          return;
        }
        ESP_LOGW(TAG, "[%s] dp: window MISMATCH ack_off=%u (sent=%u) ack_crc=0x%04x want=0x%04x — "
                 "retransmit from %u (resend %u/%u)", this->node_(), (unsigned) ack.off,
                 (unsigned) this->dp_offset_, (unsigned) (ack.val & 0xFFFF), want,
                 (unsigned) this->dp_last_good_off_, this->dp_resends_, DP_MAX_RESENDS);
        this->dp_offset_ = this->dp_last_good_off_;
        this->dp_ctr_ = (uint8_t) ((this->dp_last_good_off_ / gtx2_proto::DP_CHUNK_MAX) & 0xFF);
        this->dp_win_chunks_ = 0;
        this->dp_state_ = DpState::SEND_WINDOW;
      }
      this->set_timeout("dp_pace", 0, [this]() { this->dp_advance_(); });
      break;
    }
    case gtx2_proto::D4:
      if (this->dp_state_ != DpState::WAIT_D4)
        return;
      // d4 00 00 = whole-file CRC verified → installed + active immediately [CAP].
      this->dp_last_progress_ms_ = millis();
      this->dp_state_ = DpState::COMPLETE;
      this->set_timeout("dp_pace", 0, [this]() { this->dp_advance_(); });
      break;
    default:
      break;
  }
}

}  // namespace gtx2_client
}  // namespace esphome
#endif  // USE_ESP32
