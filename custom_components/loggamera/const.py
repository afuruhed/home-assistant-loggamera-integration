"""Constants for the Loggamera integration."""

DOMAIN = "loggamera"

# API URLs and endpoints
API_URL = "https://platform.loggamera.se/api/v2"
# Heat pump settings (Set*) and writes are only exposed in the v1 snapshot
API_URL_V1 = "https://platform.loggamera.se/Api/v1"
API_ENDPOINT_ORGANIZATIONS = "Organizations"
API_ENDPOINT_DEVICES = "Devices"
API_ENDPOINT_POWER_METER = "PowerMeter"
API_ENDPOINT_ROOM_SENSOR = "RoomSensor"
API_ENDPOINT_GENERIC_DEVICE = "GenericDevice"
API_ENDPOINT_WATER_METER = "WaterMeter"
API_ENDPOINT_COOLING_UNIT = "CoolingUnit"
API_ENDPOINT_HEAT_PUMP = "HeatPump"
API_ENDPOINT_RAW_DATA = "RawData"
API_ENDPOINT_CAPABILITIES = "Capabilities"
API_ENDPOINT_GET_CAPABILITIES = "GetCapabilities"
API_ENDPOINT_SET_PROPERTY = "SetProperty"
API_ENDPOINT_SCENARIOS = "Scenarios"
API_ENDPOINT_EXECUTE_SCENARIO_ASYNC = "ExecuteScenarioAsync"
API_ENDPOINT_USER_ACCESS = "UserAccess"

# Configuration
CONF_API_KEY = "api_key"
CONF_ORGANIZATION_ID = "organization_id"
CONF_DEVICE_ID = "device_id"
CONF_DEVICE_TYPE = "device_type"
DEVICE_TYPES = [
    "HeatPump",
    "PowerMeter",
    "RoomSensor",
    "WaterMeter",
    "CoolingUnit",
    "HeatMeter",
    "ChargingStation",
]
DEFAULT_DEVICE_TYPE = "HeatPump"
CONF_SCAN_INTERVAL = "scan_interval"

# Default 20 minutes (1200 seconds) - optimized for PowerMeter which typically updates every ~30 minutes  # noqa: E501
DEFAULT_SCAN_INTERVAL = 1200

# Attributes
ATTR_DEVICE_TYPE = "device_type"
ATTR_DURATION_MINUTES = "duration_minutes"

# Sensor types for internal use
SENSOR_TEMPERATURE = "temperature"
SENSOR_HUMIDITY = "humidity"
SENSOR_ENERGY = "energy"
SENSOR_POWER = "power"
SENSOR_WATER = "water"
SENSOR_VALUE = "value"

# Platform definitions
PLATFORMS = ["sensor", "binary_sensor", "switch", "number"]

# Writable heat pump property exposed as a number entity
PROPERTY_SET_INDOOR_TEMP = "SetIndoorTemp"

# A failed GetCapabilities lookup is retried after this long, not on every poll
CAPABILITIES_RETRY_SECONDS = 3600
# Minimum time between two writes to the same device. A ComfortZone RX50 dropped a write
# sent 3 s after another one, while writes about 4 min apart both arrived.
WRITE_MIN_INTERVAL_SECONDS = 300
# How long a written value is shown before the device's own reported value wins again
PENDING_TIMEOUT_SECONDS = 900
