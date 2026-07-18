"""gtx2_client — ESPHome external component: a GTX2 watch client over ble_client.

Attaches to a `ble_client:` (which holds the GATT link to the watch), speaks our custom 0x0FF0 /
C1 protocol, fires `esphome.gtx2_input` events for the LE inputs, and exposes health/state
sensors + SAFE command methods. One entry per watch (multi-watch = multiple entries + ble_clients).
"""
import esphome.codegen as cg
import esphome.config_validation as cv
from esphome.components import (
    binary_sensor,
    ble_client,
    sensor,
    text_sensor,
    time as time_,
)
from esphome.const import CONF_ID, CONF_TIME_ID

CODEOWNERS = ["@jphein"]
DEPENDENCIES = ["ble_client", "api"]
AUTO_LOAD = ["sensor", "binary_sensor", "text_sensor"]
MULTI_CONF = True

gtx2_ns = cg.esphome_ns.namespace("gtx2_client")
GTX2Client = gtx2_ns.class_("GTX2Client", cg.Component, ble_client.BLEClientNode)

CONF_NODE_NAME = "node_name"
CONF_LABEL = "label"
CONF_EVENT = "event"
CONF_HEALTH_INTERVAL = "health_interval"
CONF_HEART_RATE = "heart_rate"
CONF_SPO2 = "spo2"
CONF_STEPS = "steps"
CONF_DISTANCE = "distance"
CONF_CALORIES = "calories"
CONF_CONNECTED = "connected"
CONF_FIRMWARE = "firmware"
CONF_ACTIVE_FACE = "active_face"

CONFIG_SCHEMA = (
    cv.Schema(
        {
            cv.GenerateID(): cv.declare_id(GTX2Client),
            cv.Optional(CONF_NODE_NAME): cv.string,
            # Optional per-instance discriminator appended to the LOG tag only ("node_name/label",
            # e.g. "gtx2-office/daily"). Lets multiple gtx2_client instances on one node be told
            # apart in serial logs. Does NOT touch the wire protocol or the `node` field in the
            # gtx2_input event (that stays node_name = the room HA maps).
            cv.Optional(CONF_LABEL): cv.string,
            cv.Optional(CONF_EVENT, default="esphome.gtx2_input"): cv.string,
            cv.Optional(
                CONF_HEALTH_INTERVAL, default="300s"
            ): cv.positive_time_period_milliseconds,
            cv.Optional(CONF_TIME_ID): cv.use_id(time_.RealTimeClock),
            cv.Optional(CONF_HEART_RATE): sensor.sensor_schema(
                unit_of_measurement="bpm",
                accuracy_decimals=0,
                icon="mdi:heart-pulse",
                state_class="measurement",
            ),
            cv.Optional(CONF_SPO2): sensor.sensor_schema(
                unit_of_measurement="%",
                accuracy_decimals=0,
                icon="mdi:water-percent",
                state_class="measurement",
            ),
            cv.Optional(CONF_STEPS): sensor.sensor_schema(
                unit_of_measurement="steps",
                accuracy_decimals=0,
                icon="mdi:shoe-print",
                state_class="total_increasing",
            ),
            cv.Optional(CONF_DISTANCE): sensor.sensor_schema(
                unit_of_measurement="m",
                accuracy_decimals=0,
                device_class="distance",
                state_class="total_increasing",
            ),
            cv.Optional(CONF_CALORIES): sensor.sensor_schema(
                unit_of_measurement="kcal",
                accuracy_decimals=0,
                icon="mdi:fire",
                state_class="total_increasing",
            ),
            cv.Optional(CONF_CONNECTED): binary_sensor.binary_sensor_schema(
                device_class="connectivity",
                icon="mdi:bluetooth",
            ),
            cv.Optional(CONF_FIRMWARE): text_sensor.text_sensor_schema(
                icon="mdi:chip",
            ),
            cv.Optional(CONF_ACTIVE_FACE): text_sensor.text_sensor_schema(
                icon="mdi:watch",
            ),
        }
    )
    .extend(ble_client.BLE_CLIENT_SCHEMA)
    .extend(cv.COMPONENT_SCHEMA)
)


async def to_code(config):
    var = cg.new_Pvariable(config[CONF_ID])
    await cg.register_component(var, config)
    await ble_client.register_ble_node(var, config)

    if CONF_NODE_NAME in config:
        cg.add(var.set_node_name(config[CONF_NODE_NAME]))
    if CONF_LABEL in config:
        cg.add(var.set_label(config[CONF_LABEL]))
    cg.add(var.set_event_name(config[CONF_EVENT]))
    cg.add(var.set_health_interval(config[CONF_HEALTH_INTERVAL]))

    if CONF_TIME_ID in config:
        cg.add(var.set_time_source(await cg.get_variable(config[CONF_TIME_ID])))

    for key, setter in (
        (CONF_HEART_RATE, var.set_heart_rate_sensor),
        (CONF_SPO2, var.set_spo2_sensor),
        (CONF_STEPS, var.set_steps_sensor),
        (CONF_DISTANCE, var.set_distance_sensor),
        (CONF_CALORIES, var.set_calories_sensor),
    ):
        if key in config:
            cg.add(setter(await sensor.new_sensor(config[key])))

    if CONF_CONNECTED in config:
        cg.add(var.set_connected_sensor(await binary_sensor.new_binary_sensor(config[CONF_CONNECTED])))
    if CONF_FIRMWARE in config:
        cg.add(var.set_firmware_sensor(await text_sensor.new_text_sensor(config[CONF_FIRMWARE])))
    if CONF_ACTIVE_FACE in config:
        cg.add(var.set_active_face_sensor(await text_sensor.new_text_sensor(config[CONF_ACTIVE_FACE])))
