import json
import atexit
import asyncio
import inspect
import math
import os
import shutil
import socket
import threading
import time
import traceback
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import Dict, Any

from flask import Flask, abort, g, has_request_context, jsonify, render_template, request

from drivers import intesis
from house_context import HouseRegistry
from notification_service import NotificationService
from providers import (
    CesclansPoolProvider,
    GoodWeEnergyProvider,
    HouseProviders,
    IntesisClimateProvider,
    NetatmoWeatherProvider,
    ProviderRegistry,
)
from relay_client import HHCRelayClient
from temperature_service import DEFAULT_TEMPERATURE_SENSORS, TemperatureService
from weather_service import DEFAULT_WEATHER_CONFIG, WeatherService
from history_service import DEFAULT_HISTORY_CONFIG, HistoryService
from netatmo_service import DEFAULT_NETATMO_CONFIG, NetatmoService
from winter_energy_service import DEFAULT_WINTER_CONFIG, WinterEnergyService

BASE = Path(__file__).resolve().parent
DATA_DIR = BASE / "data"
VERSION_PATH = BASE / "VERSION"
CONFIG_DEFAULTS_PATH = BASE / "config.defaults.json"
CONFIG_PATH = DATA_DIR / "config.json"
HOUSES_CONFIG_PATH = BASE / "config" / "houses.json"
NETATMO_CONFIG_PATH = BASE / "config" / "netatmo_config.json"
NETATMO_TOKENS_PATH = BASE / "config" / "netatmo_tokens.json"
NETATMO_RAIN_HISTORY_PATH = DATA_DIR / "netatmo_rain_history.json"
LANG_DIR = BASE / "lang"
STATE_PATH = DATA_DIR / "state.json"
HISTORY_DB_PATH = DATA_DIR / "history.sqlite3"
HEATER_STATS_PATH = DATA_DIR / "heater_statistics.json"
DEVICE_STATS_PATH = DATA_DIR / "device_statistics.json"
DEVICE_STATS_BACKUP_DIR = BASE / "backups" / "runtime_data"
DEVICE_STATS_BACKUP_PATH = DEVICE_STATS_BACKUP_DIR / "device_statistics.latest.json"
EVENT_LOG_PATH = DATA_DIR / "event_log.json"
LOG_DIR = BASE / "logs"
LOG_DIR.mkdir(exist_ok=True)
DATA_DIR.mkdir(exist_ok=True)

LOG_LEVELS = {"DEBUG": 10, "INFO": 20}
current_log_level = "INFO"

app = Flask(__name__)
app.config["JSON_SORT_KEYS"] = False
if hasattr(app, "json"):
    app.json.sort_keys = False
_lock = threading.RLock()
_runtime: Dict[str, Any] = {
    "devices": {},
    "relays": {},
    "last_poll": None,
    "last_command": None,
    "started_at": datetime.now().isoformat(timespec="seconds"),
    "scheduler": {"last_action_key": None, "manual_run_until": None, "auto_suspended_date": None},
    "solar_heating": {
        "last_start_date": None,
        "auto_started_date": None,
        "safe_stop_started_date": None,
        "safe_stop_completed_date": None,
        "last_temperature_check_at": None,
        "next_temperature_check_at": None,
        "last_pv_running_check_at": None,
        "pv_confirmation_started_at": None,
        "pv_confirmation_due_at": None,
        "early_completion_started_at": None,
        "early_completion_target_temperature": None,
        "early_completion_completed_date": None
    },
    "heater_safe_stop": {
        "state": "IDLE",
        "running": False,
        "phase": "Inattivo",
        "started_at": None,
        "completed_at": None,
        "error": None
    }
}
temperature_service = None
weather_service = None
history_service = None
netatmo_service = None
winter_energy_service = None
notification_service = NotificationService()
MAX_EVENTS = 500
DEFAULT_LANGUAGE = "en"
STATS_DEFAULTS = {
    "daily_date": None,
    "daily_seconds": 0,
    "seasonal_seconds": 0,
    "total_seconds": 0,
    "starts": 0,
    "active_start": None,
    "last_known_active": None,
    "updated_at": None
}

DEFAULT_HOUSES = [
    {
        "id": "cesclans",
        "display_name": "Cesclans",
        "enabled": True,
        "route": "/cesclans",
        "description": "Current configured MeM house.",
        "status_label": "Active / Configured",
        "modules": ["GoodWe", "Netatmo", "Intesis", "Pool", "Winter Energy", "Weather", "History"],
        "configured_devices": {
            "energy": ["goodwe_inverter"],
            "weather": ["netatmo"],
            "climate": ["intesis"],
            "pool": ["pool_controller"],
        },
        "providers": {
            "energy": "goodwe",
            "weather": "netatmo",
            "climate": "intesis",
            "pool": "cesclans_pool",
        },
    },
    {
        "id": "opicina",
        "display_name": "Opicina",
        "enabled": True,
        "route": "/opicina",
        "description": "House prepared for future configuration.",
        "status_label": "Configuration pending",
        "modules": [],
        "configured_devices": {},
        "providers": {},
    },
]


def write_default_json(path, data):
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def migrate_or_create_json(path, legacy_path, default_data):
    if path.exists():
        return
    if legacy_path.exists():
        shutil.move(str(legacy_path), str(path))
        return
    write_default_json(path, default_data)


def initialize_runtime_data():
    DATA_DIR.mkdir(exist_ok=True)
    if not CONFIG_PATH.exists():
        legacy_config_path = BASE / "config.json"
        if legacy_config_path.exists():
            shutil.move(str(legacy_config_path), str(CONFIG_PATH))
        elif CONFIG_DEFAULTS_PATH.exists():
            shutil.copyfile(CONFIG_DEFAULTS_PATH, CONFIG_PATH)
        else:
            write_default_json(CONFIG_PATH, {})
    migrate_or_create_json(DEVICE_STATS_PATH, BASE / "device_statistics.json", {"updated_at": None, "relays": {}})
    migrate_or_create_json(HEATER_STATS_PATH, BASE / "heater_statistics.json", STATS_DEFAULTS)
    migrate_or_create_json(EVENT_LOG_PATH, BASE / "event_log.json", [])


initialize_runtime_data()
house_registry = HouseRegistry(HOUSES_CONFIG_PATH, DEFAULT_HOUSES)
provider_registry = ProviderRegistry()
_provider_registry_lock = threading.RLock()


def load_houses_metadata():
    """Load public house metadata only; operational configuration stays unchanged."""
    houses = [house.to_public_dict() for house in house_registry.all()]
    display_order = {"opicina": 0, "cesclans": 1}
    return sorted(houses, key=lambda house: display_order.get(house["id"], len(display_order)))


def house_metadata(house_id):
    house = house_registry.get(house_id)
    return house.to_public_dict() if house else None


def current_house_context(house_id=None):
    if house_id:
        return house_registry.get(house_id)
    if has_request_context() and getattr(g, "current_house", None) is not None:
        return g.current_house
    return house_registry.default("cesclans")


def now():
    return datetime.now()


def load_config():
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    changed = ensure_config_defaults(cfg)
    if changed:
        save_config(cfg)
    set_runtime_log_level(cfg)
    return cfg


def app_version(cfg=None):
    try:
        version = VERSION_PATH.read_text(encoding="utf-8").strip()
        return version or "Unknown Version"
    except Exception:
        return "Unknown Version"


def save_config(cfg):
    set_runtime_log_level(cfg)
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)


def normalize_log_level(value):
    level = str(value or "INFO").strip().upper()
    return level if level in LOG_LEVELS else "INFO"


def set_runtime_log_level(cfg):
    global current_log_level
    logging_cfg = cfg.get("logging") if isinstance(cfg, dict) else {}
    if not isinstance(logging_cfg, dict):
        logging_cfg = {}
    current_log_level = normalize_log_level(logging_cfg.get("level"))


def log_enabled(level):
    level = normalize_log_level(level)
    return LOG_LEVELS[level] >= LOG_LEVELS.get(current_log_level, LOG_LEVELS["INFO"])


def masked_token(value):
    value = str(value or "")
    if not value:
        return ""
    if len(value) <= 8:
        return "••••"
    return f"{value[:4]}••••{value[-4:]}"


def public_config(cfg):
    public = json.loads(json.dumps(cfg))
    telegram = public.get("notifications", {}).get("telegram")
    if isinstance(telegram, dict):
        token = telegram.get("bot_token")
        telegram["bot_token"] = ""
        telegram["bot_token_masked"] = masked_token(token)
        telegram["bot_token_configured"] = bool(token)
    intesis_config = public.get("intesis")
    if isinstance(intesis_config, dict):
        password = intesis_config.get("password")
        intesis_config["password"] = ""
        intesis_config["password_configured"] = bool(password)
    public.pop("viewer", None)
    return public


GOODWE_STATUS_FIELDS = {
    "status": ("status",),
    "pv_production": ("ppv", "pv_production"),
    "house_consumption": ("house_consumption",),
    "normal_loads": ("load_ptotal", "normal_loads"),
    "backup_loads": ("backup_ptotal", "backup_loads"),
    "battery_soc": ("battery_soc",),
    "pbattery1": ("pbattery1",),
    "battery_power": ("pbattery1",),
    "battery_mode_label": ("battery_mode_label",),
    "battery_temperature": ("battery_temperature",),
    "grid_power": ("meter_active_power_total", "grid_power"),
    "temperature": ("temperature",)
}
GOODWE_HOST = "192.168.200.200"
GOODWE_DAY_POLL_SECONDS = 60
GOODWE_NIGHT_POLL_SECONDS = 60
GOODWE_DAY_START_HOUR = 6
GOODWE_DAY_END_HOUR = 22
GOODWE_RECOVERY_AFTER_FAILURES = 3
GOODWE_RECOVERY_WAIT_SECONDS = 5
GOODWE_RECOVERY_RESUME_WAIT_SECONDS = 2
GOODWE_DISCOVERY_PORT = 48899
GOODWE_DISCOVERY_MESSAGE = b"WIFIKIT-214028-READ"
GOODWE_DISCOVERY_TIMEOUT_SECONDS = 5
goodwe_failure_count = 0
goodwe_last_success = None
goodwe_last_recovery_failure_count = 0
GOODWE_RUNTIME_KEYS = (
    "ppv",
    "house_consumption",
    "load_ptotal",
    "backup_ptotal",
    "battery_soc",
    "pbattery1",
    "battery_mode_label",
    "battery_temperature",
    "meter_active_power_total",
    "temperature"
)


def goodwe_status_snapshot():
    cached = _runtime.get("goodwe")
    if not isinstance(cached, dict):
        return {}
    snapshot = {}
    for output_field, source_fields in GOODWE_STATUS_FIELDS.items():
        for source_field in source_fields:
            if source_field in cached:
                snapshot[output_field] = cached.get(source_field)
                break
    return snapshot


def viewer_config(cfg):
    ensure_config_defaults(cfg)
    return cfg.get("viewer", {})


def viewer_refresh_seconds(cfg):
    viewer = viewer_config(cfg)
    try:
        seconds = int(float(viewer.get("refresh_seconds", 60)))
    except (TypeError, ValueError):
        seconds = 60
    return max(5, min(3600, seconds))


def numeric_or_none(value):
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def require_viewer_access(cfg):
    viewer = viewer_config(cfg)
    if viewer.get("enabled") is False:
        abort(403)
    token = str(viewer.get("token") or "")
    if token and request.args.get("token") != token:
        abort(403)


def viewer_href(path="/viewer"):
    token = request.args.get("token") or ""
    return f"{path}?token={token}" if token else path


def temperature_viewer_snapshot():
    snapshot = temperature_service.snapshot() if temperature_service else {}
    sensors = snapshot.get("sensors", {}) if isinstance(snapshot, dict) else {}
    water = sensors.get("water") or {}
    return {
        "sensors": {
            "water": {
                "value": water.get("value"),
                "online": bool(water.get("online"))
            }
        },
        "communication": snapshot.get("communication", {}),
        "last_update": snapshot.get("last_update")
    }


def goodwe_viewer_snapshot():
    goodwe = goodwe_status_snapshot()
    cached = _runtime.get("goodwe")
    if isinstance(cached, dict):
        goodwe["last_update"] = cached.get("last_successful_poll")
        goodwe["online"] = cached.get("status") == "online"
    else:
        goodwe["last_update"] = None
        goodwe["online"] = False
    pv = numeric_or_none(goodwe.get("pv_production"))
    house = numeric_or_none(goodwe.get("house_consumption"))
    goodwe["available_surplus"] = None if pv is None or house is None else max(0, pv - house)
    return goodwe


def viewer_statistics_snapshot(cfg):
    snapshot = statistics_snapshot(cfg)
    return {
        key: {
            "key": value.get("key"),
            "device": value.get("device"),
            "device_name": value.get("device_name"),
            "relay": value.get("relay"),
            "name": value.get("name"),
            "icon": value.get("icon"),
            "daily_seconds": value.get("daily_seconds", 0),
            "seasonal_seconds": value.get("seasonal_seconds", 0),
            "total_seconds": value.get("total_seconds", 0),
            "starts": value.get("starts", 0)
        }
        for key, value in snapshot.items()
    }


def viewer_relay_state(device_id, relay_number):
    return relay_active_from_runtime(device_id, str(relay_number))


def goodwe_poll_interval_seconds():
    current_hour = now().hour
    if GOODWE_DAY_START_HOUR <= current_hour < GOODWE_DAY_END_HOUR:
        return GOODWE_DAY_POLL_SECONDS
    return GOODWE_NIGHT_POLL_SECONDS


def goodwe_config(cfg=None):
    if cfg is None:
        cfg = load_config()
    value = cfg.get("goodwe", {}) if isinstance(cfg, dict) else {}
    return value if isinstance(value, dict) else {}


def goodwe_recovery_after_failures():
    try:
        return max(1, int(goodwe_config().get("recovery_after_failures", GOODWE_RECOVERY_AFTER_FAILURES)))
    except (TypeError, ValueError):
        return GOODWE_RECOVERY_AFTER_FAILURES


def goodwe_json_value(value):
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    try:
        return float(value)
    except (TypeError, ValueError):
        return str(value)


async def dispose_goodwe_instance(inverter, prefix="GoodWe polling", strict=False):
    if inverter is None:
        return
    cleanup_level = "INFO" if prefix == "GoodWe recovery" else "DEBUG"
    log(f"{prefix}: Destroying temporary inverter instance...", level=cleanup_level)
    for method_name in ("close", "disconnect"):
        method = getattr(inverter, method_name, None)
        if not callable(method):
            continue
        try:
            result = method()
            if inspect.isawaitable(result):
                await result
            log(f"{prefix}: inverter instance closed via {method_name}()", level=cleanup_level)
        except Exception as exc:
            log(f"{prefix}: {method_name}() cleanup failed: {exc}", level=cleanup_level)
            if strict:
                raise RuntimeError(f"resource cleanup via {method_name}() failed: {exc}") from exc
        return
    log(f"{prefix}: no persistent close method exposed; instance released", level=cleanup_level)


def run_goodwe_discovery_probe():
    stage = "UDP discovery probe"
    sock = None
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(GOODWE_DISCOVERY_TIMEOUT_SECONDS)
        log("GoodWe recovery: Sending UDP discovery probe...")
        sock.sendto(GOODWE_DISCOVERY_MESSAGE, (GOODWE_HOST, GOODWE_DISCOVERY_PORT))
        data, address = sock.recvfrom(4096)
        log(
            "GoodWe recovery: Probe reply received "
            f"from {address[0]}:{address[1]} ({len(data)} bytes)."
        )
        return data
    except Exception as exc:
        raise RuntimeError(f"{stage} failed: {exc}") from exc
    finally:
        if sock is not None:
            sock.close()


async def run_goodwe_recovery_et_session():
    stage = "importing GoodWe library"
    inverter = None
    try:
        import goodwe

        stage = "opening ET connection"
        log("GoodWe recovery: Opening ET connection...")
        inverter = await goodwe.connect(GOODWE_HOST, family="ET")
        log("GoodWe recovery: Connected.")

        stage = "reading runtime data"
        log("GoodWe recovery: Reading runtime data...")
        await inverter.read_runtime_data()
        log("GoodWe recovery: Runtime-data read successful.")
    except Exception as exc:
        raise RuntimeError(f"{stage} failed: {exc}") from exc
    finally:
        await dispose_goodwe_instance(inverter, "GoodWe recovery", strict=True)
        inverter = None


async def read_goodwe_runtime_data():
    prefix = "GoodWe polling"
    stage = "importing GoodWe library"
    inverter = None
    try:
        log(f"{prefix}: {stage}...", level="DEBUG")
        import goodwe

        log(f"{prefix}: library loaded from {getattr(goodwe, '__file__', 'unknown')}", level="DEBUG")
        stage = "creating new inverter instance"
        log(f"{prefix}: {stage}...", level="DEBUG")
        stage = "opening fresh connection/session"
        log(f"{prefix}: {stage} to {GOODWE_HOST}...", level="DEBUG")
        inverter = await goodwe.connect(GOODWE_HOST, family="ET")
        log(f"{prefix}: new {type(inverter).__name__} instance connected", level="DEBUG")
        stage = "reading runtime data"
        log(f"{prefix}: {stage}...", level="DEBUG")
        data = await inverter.read_runtime_data()
        log(f"{prefix}: runtime-data read completed ({len(data)} fields)", level="DEBUG")
        return data
    except Exception as exc:
        log(f"{prefix} failed while {stage}: {exc}")
        raise
    finally:
        await dispose_goodwe_instance(inverter, prefix)


def apply_goodwe_runtime_data(data):
    global goodwe_failure_count, goodwe_last_success
    goodwe_last_success = now()
    goodwe_failure_count = 0
    values = {
        key: goodwe_json_value(data.get(key))
        for key in GOODWE_RUNTIME_KEYS
        if key in data
    }
    with _lock:
        cached = _runtime.get("goodwe")
        if not isinstance(cached, dict):
            cached = {}
        cached.update(values)
        cached.update({
            "status": "online",
            "host": GOODWE_HOST,
            "last_successful_poll": goodwe_last_success.isoformat(timespec="seconds"),
            "last_error": None,
            "last_error_traceback": None
        })
        _runtime["goodwe"] = cached


def poll_goodwe_once():
    previous_failures = goodwe_failure_count
    log("GoodWe polling: poll cycle started", level="DEBUG")
    data = asyncio.run(read_goodwe_runtime_data())
    apply_goodwe_runtime_data(data)
    return previous_failures


def recover_goodwe_after_failures():
    log(f"GoodWe recovery started after {goodwe_failure_count} consecutive failures.")
    try:
        run_goodwe_discovery_probe()
        log(f"GoodWe recovery: Waiting {GOODWE_RECOVERY_WAIT_SECONDS} seconds...")
        time.sleep(GOODWE_RECOVERY_WAIT_SECONDS)
        asyncio.run(run_goodwe_recovery_et_session())
        log(f"GoodWe recovery: Waiting {GOODWE_RECOVERY_RESUME_WAIT_SECONDS} seconds...")
        time.sleep(GOODWE_RECOVERY_RESUME_WAIT_SECONDS)
        log("GoodWe recovery successful. Normal polling resumed.")
        return True
    except Exception as exc:
        mark_goodwe_offline(exc)
        log(f"GoodWe recovery failed: {exc}")
        log(f"TRACEBACK GoodWe recovery:\n{traceback.format_exc()}", level="DEBUG")
        return False


def goodwe_last_success_label():
    if goodwe_last_success is None:
        return "never"
    return goodwe_last_success.strftime("%H:%M:%S")


def log_goodwe_poll_success(next_poll_seconds):
    log("GoodWe polling completed successfully.", level="DEBUG")
    log(f"Last successful update: {goodwe_last_success_label()}", level="DEBUG")
    log(f"Next poll in {next_poll_seconds} seconds.", level="DEBUG")


def log_goodwe_poll_failure(next_retry_seconds):
    log("GoodWe polling FAILED.")
    log(f"Consecutive failures: {goodwe_failure_count}")
    log(f"Last successful update: {goodwe_last_success_label()}")
    log(f"Next retry in {next_retry_seconds} seconds.")


def mark_goodwe_offline(error):
    with _lock:
        cached = _runtime.get("goodwe")
        if not isinstance(cached, dict):
            cached = {}
        cached.update({
            "status": "offline",
            "host": GOODWE_HOST,
            "last_error": str(error),
            "last_error_traceback": traceback.format_exc(),
            "last_error_at": now().isoformat(timespec="seconds")
        })
        _runtime["goodwe"] = cached


def goodwe_background_loop():
    global goodwe_failure_count, goodwe_last_recovery_failure_count
    log(f"GoodWe polling service started for {GOODWE_HOST}")
    while True:
        poll_succeeded = False
        recovery_attempted = False
        previous_failures = goodwe_failure_count
        try:
            previous_failures = poll_goodwe_once()
            poll_succeeded = True
        except Exception as exc:
            goodwe_failure_count += 1
            error_traceback = traceback.format_exc()
            mark_goodwe_offline(exc)
            log(f"ERRORE GoodWe polling: {exc}")
            log(f"TRACEBACK GoodWe polling:\n{error_traceback}", level="DEBUG")
            recovery_after = goodwe_recovery_after_failures()
            if (
                goodwe_failure_count >= recovery_after
                and goodwe_failure_count != goodwe_last_recovery_failure_count
            ):
                goodwe_last_recovery_failure_count = goodwe_failure_count
                previous_failures = goodwe_failure_count
                recovery_attempted = True
                poll_succeeded = recover_goodwe_after_failures()
                if poll_succeeded:
                    goodwe_last_recovery_failure_count = 0
                    continue
        next_poll_seconds = goodwe_poll_interval_seconds()
        if poll_succeeded:
            if previous_failures > 0 and not recovery_attempted:
                log(f"GoodWe polling restored after {previous_failures} failures.")
            log_goodwe_poll_success(next_poll_seconds)
        else:
            log_goodwe_poll_failure(next_poll_seconds)
        time.sleep(next_poll_seconds)


def ensure_config_defaults(cfg):
    changed = False
    if "logging" not in cfg or not isinstance(cfg.get("logging"), dict):
        cfg["logging"] = {}
        changed = True
    logging_cfg = cfg["logging"]
    normalized_level = normalize_log_level(logging_cfg.get("level"))
    if logging_cfg.get("level") != normalized_level:
        logging_cfg["level"] = normalized_level
        changed = True
    if "goodwe" not in cfg or not isinstance(cfg.get("goodwe"), dict):
        cfg["goodwe"] = {}
        changed = True
    goodwe = cfg["goodwe"]
    goodwe_defaults = {
        "recovery_after_failures": GOODWE_RECOVERY_AFTER_FAILURES
    }
    for key, value in goodwe_defaults.items():
        if key not in goodwe:
            goodwe[key] = value
            changed = True
    if "intesis" not in cfg or not isinstance(cfg.get("intesis"), dict):
        cfg["intesis"] = {}
        changed = True
    intesis_config = cfg["intesis"]
    for key, value in intesis.DEFAULT_INTESIS_CONFIG.items():
        if key not in intesis_config:
            intesis_config[key] = value
            changed = True
    app_cfg = cfg.get("app")
    if isinstance(app_cfg, dict) and "version" in app_cfg:
        app_cfg.pop("version", None)
        changed = True
    if "solar_heating" not in cfg or not isinstance(cfg.get("solar_heating"), dict):
        cfg["solar_heating"] = {}
        changed = True
    solar = cfg["solar_heating"]
    defaults = {
        "enabled": False,
        "water_temperature_threshold": 29.0,
        "start_time": "08:00",
        "forced_stop_time": "17:00",
        "temperature_check_interval_seconds": 900,
        "early_completion_temperature": 31.0,
        "early_completion_confirmation_minutes": 120,
        "minimum_pv_production_watts": 2700,
        "pv_running_check_interval_minutes": 30,
        "pv_confirmation_delay_minutes": 15
    }
    for key, value in defaults.items():
        if key not in solar:
            solar[key] = value
            changed = True
    if "notifications" not in cfg or not isinstance(cfg.get("notifications"), dict):
        cfg["notifications"] = {}
        changed = True
    notifications = cfg["notifications"]
    if "telegram" not in notifications or not isinstance(notifications.get("telegram"), dict):
        notifications["telegram"] = {}
        changed = True
    telegram = notifications["telegram"]
    telegram_defaults = {
        "enabled": False,
        "bot_token": "",
        "chat_id": ""
    }
    for key, value in telegram_defaults.items():
        if key not in telegram:
            telegram[key] = value
            changed = True
    if "viewer" not in cfg or not isinstance(cfg.get("viewer"), dict):
        cfg["viewer"] = {}
        changed = True
    viewer = cfg["viewer"]
    viewer_defaults = {
        "enabled": True,
        "token": "",
        "refresh_seconds": 60
    }
    for key, value in viewer_defaults.items():
        if key not in viewer:
            viewer[key] = value
            changed = True
    if "weather" not in cfg or not isinstance(cfg.get("weather"), dict):
        cfg["weather"] = json.loads(json.dumps(DEFAULT_WEATHER_CONFIG))
        changed = True
    else:
        weather = cfg["weather"]
        for key in ("poll_seconds", "forecast_poll_seconds", "timeout_seconds", "retry_count"):
            if key not in weather:
                weather[key] = DEFAULT_WEATHER_CONFIG[key]
                changed = True
        for key in ("sources", "locations"):
            if key not in weather or not isinstance(weather.get(key), dict):
                weather[key] = json.loads(json.dumps(DEFAULT_WEATHER_CONFIG[key]))
                changed = True
        for source_key, source in DEFAULT_WEATHER_CONFIG.get("sources", {}).items():
            if source_key not in weather["sources"]:
                weather["sources"][source_key] = json.loads(json.dumps(source))
                changed = True
        for location_key, location in DEFAULT_WEATHER_CONFIG.get("locations", {}).items():
            if location_key not in weather["locations"] or not isinstance(weather["locations"].get(location_key), dict):
                weather["locations"][location_key] = json.loads(json.dumps(location))
                changed = True
                continue
            target_location = weather["locations"][location_key]
            target_location.setdefault("label", location.get("label", location_key))
            if "forecast_location" not in target_location and location.get("forecast_location"):
                target_location["forecast_location"] = location["forecast_location"]
                changed = True
            if "metrics" not in target_location or not isinstance(target_location.get("metrics"), dict):
                target_location["metrics"] = {}
                changed = True
            for metric_key, metric in (location.get("metrics") or {}).items():
                target_metric = target_location["metrics"].get(metric_key)
                if not isinstance(target_metric, dict) or target_metric.get("placeholder"):
                    target_location["metrics"][metric_key] = json.loads(json.dumps(metric))
                    changed = True
        opicina_metrics = (((weather.get("locations") or {}).get("opicina") or {}).get("metrics") or {})
        temperature_metric = opicina_metrics.get("temperature") or {}
        humidity_metric = opicina_metrics.get("humidity") or {}
        if temperature_metric.get("source") == "opicina_hwg_ste" and temperature_metric.get("sensor_id") == 1:
            temperature_metric["sensor_id"] = 215
            changed = True
        if humidity_metric.get("source") == "opicina_hwg_ste" and humidity_metric.get("sensor_id") == 2:
            humidity_metric["sensor_id"] = 216
            changed = True
    if "netatmo" not in cfg or not isinstance(cfg.get("netatmo"), dict):
        cfg["netatmo"] = json.loads(json.dumps(DEFAULT_NETATMO_CONFIG))
        changed = True
    else:
        netatmo = cfg["netatmo"]
        for key, value in DEFAULT_NETATMO_CONFIG.items():
            if key not in netatmo:
                netatmo[key] = value
                changed = True
    if "history" not in cfg or not isinstance(cfg.get("history"), dict):
        cfg["history"] = json.loads(json.dumps(DEFAULT_HISTORY_CONFIG))
        changed = True
    else:
        history = cfg["history"]
        for key, value in DEFAULT_HISTORY_CONFIG.items():
            if key not in history:
                history[key] = value
                changed = True
    if "winter" not in cfg or not isinstance(cfg.get("winter"), dict):
        cfg["winter"] = json.loads(json.dumps(DEFAULT_WINTER_CONFIG))
        changed = True
    else:
        winter = cfg["winter"]
        for key, value in DEFAULT_WINTER_CONFIG.items():
            if key not in winter:
                winter[key] = value
                changed = True
        # MEM-004 is strictly learning/simulation mode.
        if winter.get("automation") is not False:
            winter["automation"] = False
            changed = True
    return changed


def supported_languages():
    languages = {}
    if not LANG_DIR.exists():
        return languages
    for path in sorted(LANG_DIR.glob("*.json")):
        code = path.stem
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        meta = data.get("meta", {}) if isinstance(data, dict) else {}
        languages[code] = {
            "code": code,
            "name": meta.get("name", code),
            "nativeName": meta.get("nativeName", meta.get("name", code)),
            "flag": meta.get("flag", "")
        }
    return languages


def normalize_language(code):
    supported = supported_languages()
    if not supported:
        return DEFAULT_LANGUAGE
    if not code:
        return DEFAULT_LANGUAGE if DEFAULT_LANGUAGE in supported else next(iter(supported))
    lang = str(code).lower().split("-")[0]
    if lang in supported:
        return lang
    return DEFAULT_LANGUAGE if DEFAULT_LANGUAGE in supported else next(iter(supported))


def browser_language():
    supported = supported_languages()
    accepted = request.headers.get("Accept-Language", "")
    for part in accepted.split(","):
        code = part.split(";")[0].strip()
        lang = str(code).lower().split("-")[0]
        if lang in supported:
            return lang
    return normalize_language(DEFAULT_LANGUAGE)


def selected_language(cfg=None, persist_browser=False):
    if cfg is None:
        cfg = load_config()
    ui = cfg.get("ui") if isinstance(cfg.get("ui"), dict) else {}
    lang = ui.get("language")
    if not lang and persist_browser:
        lang = browser_language()
        cfg["ui"] = {**ui, "language": lang}
        save_config(cfg)
        return lang
    return normalize_language(lang)


def set_selected_language(language):
    cfg = load_config()
    lang = normalize_language(language)
    ui = cfg.get("ui") if isinstance(cfg.get("ui"), dict) else {}
    cfg["ui"] = {**ui, "language": lang}
    save_config(cfg)
    return lang


def load_language_file(language):
    lang = normalize_language(language)
    path = LANG_DIR / f"{lang}.json"
    if not path.exists():
        path = LANG_DIR / f"{DEFAULT_LANGUAGE}.json"
        lang = DEFAULT_LANGUAGE
    data = json.loads(path.read_text(encoding="utf-8"))
    return lang, data


def load_state():
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_state(extra=None):
    with _lock:
        data = {
            "scheduler": _runtime.get("scheduler", {}),
            "solar_heating": _runtime.get("solar_heating", {}),
            "saved_at": now().isoformat(timespec="seconds")
        }
        if extra:
            data.update(extra)
        STATE_PATH.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def relay_key(device_id, relay_number):
    return f"{device_id}:{relay_number}"


def relay_label(cfg, device_id, relay_number):
    relay = cfg["devices"][device_id]["relays"][str(relay_number)]
    return relay.get("name") or f"{cfg['devices'][device_id]['name']} relay {relay_number}"


def relay_event_name(cfg, device_id, relay_number, active):
    return f"{relay_label(cfg, device_id, relay_number)} {'ON' if active else 'OFF'}"


def source_label(source):
    source = source or "system"
    if "safe_stop" in source or "interlock" in source:
        return "Safe Stop"
    if source == "scheduler":
        return "Scheduler"
    if source in ("manual", "manual_timer"):
        return "Manual"
    if source == "startup":
        return "Startup"
    if source == "manual_refresh":
        return "Manual refresh"
    return source.replace("_", " ").title()


def event_reason(source, active=None):
    if source == "scheduler":
        return "Automatic schedule"
    if source == "manual_timer":
        return "Manual pump timer expired"
    if "safe_stop" in (source or "") or "interlock" in (source or ""):
        return "Protection sequence"
    if source == "poll":
        return "Backend polling"
    if source == "startup":
        return "Startup check"
    if source == "manual_refresh":
        return "Explicit user refresh"
    if source == "manual":
        if active is True:
            return "Manual ON command"
        if active is False:
            return "Manual OFF command"
        return "Manual command"
    return "Confirmed relay state"


def relay_definitions(cfg):
    relays = []
    for device_id, dev in cfg.get("devices", {}).items():
        for relay_number, relay in dev.get("relays", {}).items():
            relays.append({
                "key": relay_key(device_id, relay_number),
                "device": device_id,
                "device_name": dev.get("name", device_id),
                "relay": str(relay_number),
                "relay_label": f"Relay {relay_number}",
                "name": relay.get("name", f"Relay {relay_number}"),
                "icon": relay.get("icon", "🔌")
            })
    return relays


def default_stats():
    stats = dict(STATS_DEFAULTS)
    stats["daily_date"] = date.today().isoformat()
    return stats


def normalize_statistics(data):
    if not isinstance(data, dict):
        return {}
    if isinstance(data.get("relays"), dict):
        return data["relays"]
    return data


def statistics_runtime_total(stats):
    if not isinstance(stats, dict):
        return 0
    total = 0
    for value in stats.values():
        if not isinstance(value, dict):
            continue
        try:
            total += max(0, int(value.get("total_seconds") or 0))
        except (TypeError, ValueError):
            continue
    return total


def load_statistics_file(path):
    try:
        return normalize_statistics(json.loads(path.read_text(encoding="utf-8")))
    except Exception:
        return {}


def migrate_heater_statistics(cfg, stats):
    if stats or not HEATER_STATS_PATH.exists():
        return stats
    try:
        saved = json.loads(HEATER_STATS_PATH.read_text(encoding="utf-8"))
    except Exception:
        return stats
    if not isinstance(saved, dict):
        return stats
    heater_dev, heater_no = heater_relay(cfg)
    merged = default_stats()
    merged.update(saved)
    stats[relay_key(heater_dev, heater_no)] = merged
    return stats


def load_device_statistics(cfg=None):
    if cfg is None:
        cfg = load_config()
    stats = {}
    if DEVICE_STATS_PATH.exists():
        stats = load_statistics_file(DEVICE_STATS_PATH)
    backup_stats = load_statistics_file(DEVICE_STATS_BACKUP_PATH) if DEVICE_STATS_BACKUP_PATH.exists() else {}
    if statistics_runtime_total(backup_stats) > statistics_runtime_total(stats):
        stats = backup_stats
        log("STATS recovered from persistent backup after runtime counter regression")
    stats = migrate_heater_statistics(cfg, stats)
    for relay in relay_definitions(cfg):
        current = default_stats()
        saved = stats.get(relay["key"])
        if isinstance(saved, dict):
            current.update(saved)
        if not current.get("daily_date"):
            current["daily_date"] = date.today().isoformat()
        stats[relay["key"]] = current
    return stats


def save_device_statistics(stats, allow_regression=False):
    data = {
        "updated_at": now().isoformat(timespec="seconds"),
        "relays": stats
    }
    serialized = json.dumps(data, indent=2, ensure_ascii=False)
    DEVICE_STATS_BACKUP_DIR.mkdir(parents=True, exist_ok=True)

    current_stats = load_statistics_file(DEVICE_STATS_PATH) if DEVICE_STATS_PATH.exists() else {}
    backup_stats = load_statistics_file(DEVICE_STATS_BACKUP_PATH) if DEVICE_STATS_BACKUP_PATH.exists() else {}
    backup_source = stats if allow_regression else current_stats
    if allow_regression or statistics_runtime_total(backup_source) >= statistics_runtime_total(backup_stats):
        backup_tmp = DEVICE_STATS_BACKUP_PATH.with_suffix(".json.tmp")
        backup_tmp.write_text(
            json.dumps(
                {
                    "updated_at": now().isoformat(timespec="seconds"),
                    "relays": backup_source
                },
                indent=2,
                ensure_ascii=False
            ),
            encoding="utf-8"
        )
        os.replace(backup_tmp, DEVICE_STATS_BACKUP_PATH)

    stats_tmp = DEVICE_STATS_PATH.with_suffix(".json.tmp")
    stats_tmp.write_text(serialized, encoding="utf-8")
    os.replace(stats_tmp, DEVICE_STATS_PATH)


def load_event_log():
    if EVENT_LOG_PATH.exists():
        try:
            events = json.loads(EVENT_LOG_PATH.read_text(encoding="utf-8"))
            if isinstance(events, list):
                return events[-MAX_EVENTS:]
        except Exception:
            pass
    return []


def save_event_log(events):
    EVENT_LOG_PATH.write_text(json.dumps(events[-MAX_EVENTS:], indent=2, ensure_ascii=False), encoding="utf-8")


def record_event(device="", relay="", event="", source="", reason=""):
    current = now()
    entry = {
        "date": current.strftime("%Y-%m-%d"),
        "time": current.strftime("%H:%M:%S"),
        "device": device,
        "relay": relay,
        "event": event,
        "source": source,
        "reason": reason
    }
    with _lock:
        events = load_event_log()
        events.append(entry)
        save_event_log(events)
    if "thermometer OFFLINE" in str(event):
        notify_sensor_offline(load_config())
    return entry


def seconds_between(start_dt, end_dt):
    return max(0, int((end_dt - start_dt).total_seconds()))


def add_runtime(stats, start_dt, end_dt):
    elapsed = seconds_between(start_dt, end_dt)
    if elapsed <= 0:
        return
    stats["seasonal_seconds"] = int(stats.get("seasonal_seconds") or 0) + elapsed
    stats["total_seconds"] = int(stats.get("total_seconds") or 0) + elapsed

    current_day = end_dt.date()
    day_start = datetime.combine(current_day, datetime.min.time())
    today_elapsed = seconds_between(max(start_dt, day_start), end_dt)
    stats["daily_date"] = current_day.isoformat()
    stats["daily_seconds"] = int(stats.get("daily_seconds") or 0) + today_elapsed


def roll_daily_if_needed(stats, current):
    today = current.date().isoformat()
    if stats.get("daily_date") == today:
        return
    stats["daily_date"] = today
    stats["daily_seconds"] = 0


def statistics_snapshot(cfg=None):
    if cfg is None:
        cfg = load_config()
    current = now()
    with _lock:
        stats = load_device_statistics(cfg)
        snapshot = {}
        for relay in relay_definitions(cfg):
            key = relay["key"]
            relay_stats = stats[key]
            roll_daily_if_needed(relay_stats, current)
            preview = dict(relay_stats)
            active_start = relay_stats.get("active_start")
            if active_start:
                try:
                    add_runtime(preview, datetime.fromisoformat(active_start), current)
                    preview["active_start"] = active_start
                except Exception:
                    pass
            snapshot[key] = {**relay, **preview}
        save_device_statistics(stats)
        return snapshot


def sync_device_statistics(cfg, device_id, relay_number, active, source="poll", reason=None):
    current = now()
    with _lock:
        stats = load_device_statistics(cfg)
        key = relay_key(device_id, relay_number)
        relay_stats = stats.get(key, default_stats())
        roll_daily_if_needed(relay_stats, current)
        last_known = relay_stats.get("last_known_active")
        active_start = relay_stats.get("active_start")

        if active is True and not active_start:
            relay_stats["active_start"] = current.isoformat(timespec="seconds")
            if last_known is not True:
                relay_stats["starts"] = int(relay_stats.get("starts") or 0) + 1
                record_event(
                    relay_label(cfg, device_id, relay_number),
                    f"Relay {relay_number}",
                    relay_event_name(cfg, device_id, relay_number, True),
                    source_label(source),
                    reason or event_reason(source, True)
                )
        elif active is False and active_start:
            try:
                add_runtime(relay_stats, datetime.fromisoformat(active_start), current)
            except Exception:
                pass
            relay_stats["active_start"] = None
            record_event(
                relay_label(cfg, device_id, relay_number),
                f"Relay {relay_number}",
                relay_event_name(cfg, device_id, relay_number, False),
                source_label(source),
                reason or event_reason(source, False)
            )
        elif active is False and last_known is True:
            record_event(
                relay_label(cfg, device_id, relay_number),
                f"Relay {relay_number}",
                relay_event_name(cfg, device_id, relay_number, False),
                source_label(source),
                reason or event_reason(source, False)
            )

        if active is True or active is False:
            relay_stats["last_known_active"] = active
        stats[key] = relay_stats
        save_device_statistics(stats)


def reset_season_statistics(cfg=None):
    if cfg is None:
        cfg = load_config()
    current = now()
    with _lock:
        stats = load_device_statistics(cfg)
        for relay in relay_definitions(cfg):
            relay_stats = stats[relay["key"]]
            roll_daily_if_needed(relay_stats, current)
            active_start = relay_stats.get("active_start")
            if active_start:
                try:
                    start_dt = datetime.fromisoformat(active_start)
                    elapsed = seconds_between(start_dt, current)
                    if elapsed > 0:
                        relay_stats["total_seconds"] = int(relay_stats.get("total_seconds") or 0) + elapsed
                        day_start = datetime.combine(current.date(), datetime.min.time())
                        relay_stats["daily_date"] = current.date().isoformat()
                        relay_stats["daily_seconds"] = int(relay_stats.get("daily_seconds") or 0) + seconds_between(max(start_dt, day_start), current)
                except Exception:
                    pass
                relay_stats["active_start"] = current.isoformat(timespec="seconds")
            relay_stats["seasonal_seconds"] = 0
        save_device_statistics(stats, allow_regression=True)
        record_event("Application", "", "Seasonal statistics reset", "API", "Seasonal runtime reset for all relays")
        return stats


def heater_statistics_snapshot():
    cfg = load_config()
    heater_dev, heater_no = heater_relay(cfg)
    return statistics_snapshot(cfg).get(relay_key(heater_dev, heater_no), default_stats())


def reset_heater_seasonal_statistics():
    cfg = load_config()
    heater_dev, heater_no = heater_relay(cfg)
    current = now()
    with _lock:
        stats = load_device_statistics(cfg)
        relay_stats = stats.get(relay_key(heater_dev, heater_no), default_stats())
        roll_daily_if_needed(relay_stats, current)
        relay_stats["seasonal_seconds"] = 0
        stats[relay_key(heater_dev, heater_no)] = relay_stats
        save_device_statistics(stats, allow_regression=True)
        return relay_stats


def log(msg: str, level="INFO"):
    level = normalize_log_level(level)
    if not log_enabled(level):
        return
    prefix = "" if level == "INFO" else f"{level} "
    line = f"{now().strftime('%Y-%m-%d %H:%M:%S')} | {msg}\n"
    if prefix:
        line = f"{now().strftime('%Y-%m-%d %H:%M:%S')} | {prefix}| {msg}\n"
    (LOG_DIR / f"{now().strftime('%Y-%m-%d')}.log").open("a", encoding="utf-8").write(line)
    print(line, end="")


def format_runtime(seconds):
    seconds = max(0, int(seconds or 0))
    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    return f"{hours}h {minutes:02d}m"


def notification_time():
    return now().strftime("%H:%M")


def notification_action_suffix(source):
    source = source or ""
    if "safe_stop" in source or "interlock" in source:
        return " by Safe Stop"
    if source == "manual_timer" or source == "manual" or source.startswith("manual_"):
        return " manually"
    if source == "scheduler" or source == "solar_heating" or source.startswith("solar_heating_"):
        return " automatically"
    return ""


def send_notification(message, cfg=None):
    if cfg is None:
        cfg = load_config()
    return notification_service.send_notification(cfg, message)


def telegram_test_message(cfg):
    return (
        "🏊 PoolGardenController\n\n"
        "✅ Telegram connection successful.\n\n"
        f"Software version:\n{app_version(cfg)}\n\n"
        "Controller:\nOnline\n\n"
        f"Date and time:\n{now().strftime('%Y-%m-%d %H:%M:%S')}"
    )


def notify_pump_started(cfg, source=None):
    send_notification(
        "💧 PoolGardenController\n\n"
        f"Pump started{notification_action_suffix(source)}.\n\n"
        f"Time:\n{notification_time()}",
        cfg
    )


def notify_pump_stopped(cfg, source=None):
    send_notification(
        "💧 PoolGardenController\n\n"
        f"Pump stopped{notification_action_suffix(source)}.\n\n"
        f"Time:\n{notification_time()}",
        cfg
    )


def notify_heater_started(cfg, source=None):
    send_notification(
        "🔥 PoolGardenController\n\n"
        f"Heater started{notification_action_suffix(source)}.\n\n"
        f"Time:\n{notification_time()}",
        cfg
    )


def notify_heater_started_automatically(cfg, water_temperature, threshold):
    send_notification(
        "🔥 PoolGardenController\n\n"
        "Solar Heating Automation\n\n"
        "Heater started automatically.\n\n"
        f"Water temperature:\n{water_temperature:.1f} °C\n\n"
        f"Threshold:\n{threshold:.1f} °C\n\n"
        f"Time:\n{notification_time()}",
        cfg
    )


def notify_heater_stopped(cfg, source=None):
    send_notification(
        "🔥 PoolGardenController\n\n"
        f"Heater stopped{notification_action_suffix(source)}.\n\n"
        f"Time:\n{notification_time()}",
        cfg
    )


def notify_automation_started(cfg, water_temperature=None, threshold=None):
    parts = [
        "☀ PoolGardenController",
        "Solar Heating Automation started."
    ]
    if water_temperature is not None:
        parts.append(f"Water temperature:\n{water_temperature:.1f} °C")
    if threshold is not None:
        parts.append(f"Threshold:\n{threshold:.1f} °C")
    parts.append(f"Time:\n{notification_time()}")
    send_notification("\n\n".join(parts), cfg)


def notify_automation_completed(cfg, early=False):
    water_temperature = water_temperature_snapshot()
    heating_today = format_runtime(heater_statistics_snapshot().get("daily_seconds"))
    if early:
        message = (
            "☀ PoolGardenController\n\n"
            "Solar Heating Automation completed early.\n\n"
            "Target temperature maintained.\n\n"
            f"Heating today:\n{heating_today}"
        )
    else:
        final_temperature = "--" if water_temperature is None else f"{water_temperature:.1f} °C"
        message = (
            "☀ PoolGardenController\n\n"
            "Solar Heating Automation completed.\n\n"
            f"Heating today:\n{heating_today}\n\n"
            f"Final water temperature:\n{final_temperature}"
        )
    send_notification(message, cfg)


def notify_sensor_offline(cfg):
    send_notification(
        "⚠ PoolGardenController\n\n"
        "Temperature sensor offline.",
        cfg
    )


def relay_diagnostic_message(cfg, device_id=None, relay_number=None, operation="READ", status=None):
    status = status or {}
    dev = cfg.get("devices", {}).get(device_id, {}) if device_id else {}
    timestamp = status.get("updated_at") or now().isoformat(timespec="seconds")
    response = status.get("response")
    error = status.get("error") or status.get("command_error") or "Unknown communication error"
    return (
        "⚠ PoolGardenController\n\n"
        "Relay communication failure.\n\n"
        f"Device:\n{dev.get('name') or device_id or '-'}\n\n"
        f"IP address:\n{dev.get('ip') or '-'}\n\n"
        f"Relay:\n{relay_number or '-'}\n\n"
        f"Operation:\n{operation or status.get('operation') or '-'}\n\n"
        f"Reason:\n{error}\n\n"
        f"Raw response:\n{response if response else '-'}\n\n"
        f"Timestamp:\n{timestamp}"
    )


def notify_communication_error(cfg, device_id=None, relay_number=None, operation="READ", status=None):
    if (operation or "").upper() == "READ":
        return None
    send_notification(
        relay_diagnostic_message(cfg, device_id, relay_number, operation, status),
        cfg
    )


def notify_relay_state_changed(cfg, device_id, relay_number, active, source="manual"):
    if source in ("poll", "startup", "manual_refresh"):
        return
    pump_dev, pump_no = pump_relay(cfg)
    heater_dev, heater_no = heater_relay(cfg)
    if same_relay(device_id, relay_number, pump_dev, pump_no):
        if active:
            notify_pump_started(cfg, source)
        else:
            notify_pump_stopped(cfg, source)
    elif same_relay(device_id, relay_number, heater_dev, heater_no):
        if active:
            notify_heater_started(cfg, source)
        else:
            notify_heater_stopped(cfg, source)


def client_for(cfg, device_id):
    d = cfg["devices"][device_id]
    return HHCRelayClient(d["ip"], d["port"], cfg["app"].get("tcp_timeout_seconds", 2.0))


def parse_active(response: str, relay_number: str):
    r = (response or "").lower().replace("\x00", "").strip()
    if f"on{relay_number}" in r:
        return True
    if f"off{relay_number}" in r:
        return False
    if "open" in r:
        return True
    if "close" in r or "closed" in r:
        return False
    return None


def has_controller_identity(response: str):
    return "hhc-net2d" in (response or "").lower()


def read_one(cfg, device_id, relay_number):
    c = client_for(cfg, device_id)
    res = c.read_relay(relay_number)
    active = parse_active(res.response, relay_number) if res.ok else None
    has_response = bool((res.response or "").strip())
    incomplete = bool(res.ok and has_response and active not in (True, False))
    ok = bool(res.ok and (active in (True, False) or incomplete))
    error = res.error
    if res.ok and not has_response:
        ok = False
        error = "No response received"
    return {
        "ok": ok,
        "active": active,
        "response": res.response,
        "elapsed_ms": res.elapsed_ms,
        "error": error,
        "incomplete": incomplete,
        "controller_identified": has_controller_identity(res.response),
        "operation": "READ",
        "updated_at": now().isoformat(timespec="seconds")
    }


def cached_relay_status(device_id, relay_number):
    return _runtime.get("relays", {}).get(relay_key(device_id, relay_number), {})


def refresh_device_status(cfg, device_id):
    dev = cfg["devices"][device_id]
    relays = _runtime.get("relays", {})
    statuses = [
        relays.get(relay_key(device_id, relay_no), {})
        for relay_no in dev.get("relays", {}).keys()
    ]
    has_status = bool(statuses)
    ok = has_status and all(st.get("ok") for st in statuses)
    last_error = next((st.get("error", "") for st in statuses if not st.get("ok")), "")
    max_elapsed = max((st.get("elapsed_ms") or 0 for st in statuses), default=0)
    _runtime["devices"][device_id] = {
        "ok": ok,
        "name": dev["name"],
        "ip": dev["ip"],
        "port": dev["port"],
        "elapsed_ms": max_elapsed,
        "error": last_error,
        "updated_at": now().isoformat(timespec="seconds")
    }


def cache_relay_status(cfg, device_id, relay_number, status, source="operation", notify_on_error=False):
    key = relay_key(device_id, relay_number)
    cached = dict(status)
    previous = cached_relay_status(device_id, relay_number)
    if cached.get("active") not in (True, False) and previous.get("active") in (True, False):
        cached["active"] = previous.get("active")
        cached["state_preserved"] = True
        if not cached.get("response"):
            cached["response"] = previous.get("response", "")
    with _lock:
        _runtime["relays"][key] = cached
        refresh_device_status(cfg, device_id)
    if cached.get("ok") and cached.get("active") in (True, False):
        sync_device_statistics(cfg, device_id, relay_number, cached.get("active"), source=source)
    elif not cached.get("ok"):
        record_event(
            relay_label(cfg, device_id, relay_number),
            f"Relay {relay_number}",
            "Communication error",
            source_label(source),
            cached.get("error") or "Relay read failed"
        )
        if notify_on_error:
            notify_communication_error(
                cfg,
                device_id,
                relay_number,
                cached.get("operation") or "READ",
                cached
            )
    return cached


def read_and_cache_one(cfg, device_id, relay_number, source="operation", notify_on_error=False):
    return cache_relay_status(
        cfg,
        device_id,
        relay_number,
        read_one(cfg, device_id, relay_number),
        source=source,
        notify_on_error=notify_on_error
    )


def set_one_raw(cfg, device_id, relay_number, active: bool, source="manual"):
    c = client_for(cfg, device_id)
    action = "ON" if active else "OFF"
    previous_status = cached_relay_status(device_id, relay_number)
    previous_active = previous_status.get("active")
    res = c.set_relay(relay_number, active)
    if res.ok:
        time.sleep(2)
        status = read_and_cache_one(cfg, device_id, relay_number, source=source, notify_on_error=True)
    else:
        previous = cached_relay_status(device_id, relay_number)
        status = cache_relay_status(
            cfg,
            device_id,
            relay_number,
            {
                "ok": False,
                "active": previous.get("active"),
                "response": res.response,
                "elapsed_ms": res.elapsed_ms,
                "error": res.error,
                "operation": "WRITE",
                "updated_at": now().isoformat(timespec="seconds")
            },
            source=source,
            notify_on_error=True
        )
    status["command_ok"] = res.ok
    status["command_error"] = res.error
    incomplete_status = bool(status.get("incomplete") or status.get("state_preserved"))
    if res.ok and status.get("ok") and status.get("active") is not active and not incomplete_status:
        status["ok"] = False
        status["error"] = f"Relay verification failed: expected {action}, read {status.get('active')}"
        cache_relay_status(cfg, device_id, relay_number, status, source=source, notify_on_error=False)
        record_event(
            relay_label(cfg, device_id, relay_number),
            f"Relay {relay_number}",
            "Command verification failed",
            source_label(source),
            status["error"]
        )
    with _lock:
        _runtime["last_command"] = {
            "device": device_id,
            "relay": relay_number,
            "action": action,
            "source": source,
            "ok": res.ok and status.get("ok") and (status.get("active") is active or incomplete_status),
            "verified": status.get("active") is active,
            "response": res.response,
            "read_response": status.get("response"),
            "time": now().isoformat(timespec="seconds")
        }
    log(f"{source.upper()} {device_id}.{relay_number} {action} | cmd_ok={res.ok} cmd_resp={res.response!r} | stato={status.get('active')} resp={status.get('response')!r} err={res.error or status.get('error','')}")
    notification_ok = bool(
        status.get("command_ok")
        and previous_active is not active
        and (
            status.get("active") is active
            or status.get("active") is None
            or status.get("state_preserved")
            or status.get("incomplete")
        )
    )
    if notification_ok:
        notify_relay_state_changed(cfg, device_id, relay_number, active, source=source)
    return status


def relay_set_confirmed(status, expected_active):
    return status.get("command_ok", True) and status.get("ok") and status.get("active") is expected_active


def heater_relay(cfg):
    if isinstance(cfg.get("heater"), dict):
        h = cfg["heater"]
        return h["device"], str(h["relay"])
    for dev_id, dev in cfg["devices"].items():
        for relay_no, relay in dev["relays"].items():
            name = relay.get("name", "").lower()
            if relay.get("icon") == "🔥" or "riscaldatore" in name:
                return dev_id, str(relay_no)
    return "pool", "1"


def pump_relay(cfg):
    p = cfg["pump"]
    return p["device"], str(p["relay"])


def same_relay(left_dev, left_relay, right_dev, right_relay):
    return left_dev == right_dev and str(left_relay) == str(right_relay)


def safe_stop_running():
    with _lock:
        return bool(_runtime["heater_safe_stop"].get("running"))


def set_safe_stop_state(**updates):
    with _lock:
        _runtime["heater_safe_stop"].update(updates)


def safe_stop_snapshot():
    with _lock:
        return dict(_runtime["heater_safe_stop"])


def fail_safe_stop(message):
    set_safe_stop_state(
        state="ERROR",
        running=False,
        phase="Errore arresto sicuro",
        error=message,
        completed_at=now().isoformat(timespec="seconds")
    )
    record_event("Heater", "Relay 1", "Safe Stop Error", "Safe Stop", message)
    log(f"HEATER_SAFE_STOP ERROR | {message}")


def complete_safe_stop(phase="Arresto sicuro completato"):
    completed_at = now().isoformat(timespec="microseconds")
    set_safe_stop_state(
        state="COMPLETED",
        running=False,
        phase=phase,
        completed_at=completed_at,
        error=None
    )
    record_event("Heater", "Relay 1", "Safe Stop Completed", "Safe Stop", phase)
    log(f"HEATER_SAFE_STOP COMPLETED | {phase}")


def heater_safe_stop_worker(source="manual"):
    try:
        cfg = load_config()
        heater_dev, heater_no = heater_relay(cfg)

        set_safe_stop_state(state="SAFE_STOP_START", phase="Spegnimento riscaldatore", error=None)
        heater_off = set_one_raw(cfg, heater_dev, heater_no, False, source=source)
        if not relay_set_confirmed(heater_off, False):
            fail_safe_stop(heater_off.get("command_error") or heater_off.get("error") or "Spegnimento riscaldatore non confermato")
            return False

        complete_safe_stop("Riscaldatore spento")
        return True
    except Exception as exc:
        fail_safe_stop(str(exc))
        return False


def start_heater_safe_stop(source="manual"):
    with _lock:
        if _runtime["heater_safe_stop"].get("running"):
            log(f"HEATER_SAFE_STOP IGNORED | already running | source={source}")
            return False, "Safe Stop already running"
        _runtime["heater_safe_stop"].update({
            "state": "SAFE_STOP_START",
            "running": True,
            "phase": "Avvio arresto sicuro",
            "started_at": now().isoformat(timespec="seconds"),
            "completed_at": None,
            "error": None
        })
    log(f"HEATER_SAFE_STOP START | source={source}")
    record_event("Heater", "Relay 1", "Safe Stop Started", source_label(source), "Immediate heater shutdown")
    completed = heater_safe_stop_worker(source=source)
    return completed, "Heater stopped" if completed else "Heater stop failed"


def ignored_safe_stop_command_status(cfg, device_id, relay_number, message="Command ignored: Safe Stop in progress"):
    key = f"{device_id}:{relay_number}"
    return {
        "ok": True,
        "active": _runtime.get("relays", {}).get(key, {}).get("active"),
        "response": message,
        "message": message,
        "ignored": True,
        "elapsed_ms": 0,
        "error": "",
        "updated_at": now().isoformat(timespec="seconds")
    }


def set_one(cfg, device_id, relay_number, active: bool, source="manual"):
    pump_dev, pump_no = pump_relay(cfg)
    heater_dev, heater_no = heater_relay(cfg)
    is_heater = same_relay(device_id, relay_number, heater_dev, heater_no)
    is_pump = same_relay(device_id, relay_number, pump_dev, pump_no)
    if safe_stop_running() and (is_heater or is_pump):
        return ignored_safe_stop_command_status(cfg, device_id, relay_number)
    if not active and is_heater:
        return set_one_raw(cfg, device_id, relay_number, False, source=source)
    if not active and is_pump:
        heater_status = read_and_cache_one(cfg, heater_dev, heater_no, source=source, notify_on_error=False)
        if not heater_status.get("ok"):
            notify_communication_error(
                cfg,
                heater_dev,
                heater_no,
                heater_status.get("operation") or "READ",
                heater_status
            )
            return {
                "ok": False,
                "active": pump_status_from_runtime(cfg),
                "response": "",
                "elapsed_ms": heater_status.get("elapsed_ms"),
                "error": heater_status.get("error") or "Impossibile verificare il riscaldatore",
                "operation": heater_status.get("operation") or "READ",
                "updated_at": now().isoformat(timespec="seconds")
            }
        if heater_status.get("active") not in (True, False):
            return {
                "ok": False,
                "active": pump_status_from_runtime(cfg),
                "response": heater_status.get("response", ""),
                "elapsed_ms": heater_status.get("elapsed_ms"),
                "error": "Relay status temporarily unavailable",
                "operation": heater_status.get("operation") or "READ",
                "incomplete": True,
                "updated_at": now().isoformat(timespec="seconds")
            }
        if heater_status.get("active") is True:
            stopped, message = start_heater_safe_stop(source=f"{source}_interlock")
            if not stopped:
                return {
                    "ok": False,
                    "active": pump_status_from_runtime(cfg),
                    "response": message,
                    "elapsed_ms": heater_status.get("elapsed_ms"),
                    "error": message,
                    "updated_at": now().isoformat(timespec="seconds")
                }
    return set_one_raw(cfg, device_id, relay_number, active, source=source)


def refresh_all_relays(source="startup", notify_on_error=True):
    cfg = load_config()
    new_devices = {}
    new_relays = {}
    for dev_id, dev in cfg["devices"].items():
        dev_ok = True
        last_error = ""
        max_elapsed = 0
        for relay_no in dev["relays"].keys():
            st = read_one(cfg, dev_id, relay_no)
            previous = cached_relay_status(dev_id, relay_no)
            if st.get("active") not in (True, False) and previous.get("active") in (True, False):
                st["active"] = previous.get("active")
                st["state_preserved"] = True
                if not st.get("response"):
                    st["response"] = previous.get("response", "")
            new_relays[relay_key(dev_id, relay_no)] = st
            if st.get("ok") and st.get("active") in (True, False):
                sync_device_statistics(cfg, dev_id, relay_no, st.get("active"), source=source)
            elif not st.get("ok"):
                record_event(
                    relay_label(cfg, dev_id, relay_no),
                    f"Relay {relay_no}",
                    "Communication error",
                    source_label(source),
                    st.get("error") or "Relay read failed"
                )
            max_elapsed = max(max_elapsed, st.get("elapsed_ms") or 0)
            if not st.get("ok"):
                dev_ok = False
                last_error = st.get("error", "")
        new_devices[dev_id] = {
            "ok": dev_ok,
            "name": dev["name"],
            "ip": dev["ip"],
            "port": dev["port"],
            "elapsed_ms": max_elapsed,
            "error": last_error,
            "updated_at": now().isoformat(timespec="seconds")
        }
        if dev_ok is False and notify_on_error:
            failed = next(
                (
                    (relay_no, new_relays.get(relay_key(dev_id, relay_no), {}))
                    for relay_no in dev.get("relays", {}).keys()
                    if not new_relays.get(relay_key(dev_id, relay_no), {}).get("ok")
                ),
                (None, {})
            )
            notify_communication_error(
                cfg,
                dev_id,
                failed[0],
                failed[1].get("operation") or "READ",
                failed[1]
            )
    with _lock:
        _runtime["devices"] = new_devices
        _runtime["relays"] = new_relays
        _runtime["last_poll"] = now().isoformat(timespec="seconds")


def parse_today_time(hhmm: str):
    h, m = map(int, hhmm.split(":"))
    n = now()
    return n.replace(hour=h, minute=m, second=0, microsecond=0)


def valid_hhmm(value):
    try:
        parse_today_time(value)
        return True
    except Exception:
        return False


def pump_window(cfg):
    p = cfg["pump"]
    start = parse_today_time(p.get("start_time", "09:00"))
    duration = float(p.get("duration_hours", 6))
    end = start + timedelta(hours=duration)
    return start, end


def pump_status_from_runtime(cfg):
    p = cfg["pump"]
    key = f"{p['device']}:{p['relay']}"
    return _runtime.get("relays", {}).get(key, {}).get("active")


def water_temperature_snapshot():
    if temperature_service is None:
        return None
    water = temperature_service.snapshot().get("sensors", {}).get("water")
    if not water or not water.get("online"):
        return None
    try:
        return float(water.get("value"))
    except (TypeError, ValueError):
        return None


def solar_heating_config(cfg):
    ensure_config_defaults(cfg)
    return cfg["solar_heating"]


def solar_heating_window(cfg):
    solar = solar_heating_config(cfg)
    return (
        parse_today_time(solar.get("start_time", "08:00")),
        parse_today_time(solar.get("forced_stop_time", "17:00"))
    )


def parse_iso_datetime(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except Exception:
        return None


def solar_temperature_check_interval_seconds(solar):
    try:
        seconds = int(float(solar.get("temperature_check_interval_seconds", 900)))
    except (TypeError, ValueError):
        seconds = 900
    return min(3600, max(300, seconds))


def early_completion_temperature(solar):
    try:
        return float(solar.get("early_completion_temperature", 31.0))
    except (TypeError, ValueError):
        return 31.0


def early_completion_confirmation_minutes(solar):
    try:
        minutes = int(float(solar.get("early_completion_confirmation_minutes", 120)))
    except (TypeError, ValueError):
        minutes = 120
    return min(240, max(30, minutes))


def minimum_pv_production_watts(solar):
    try:
        watts = int(float(solar.get("minimum_pv_production_watts", 2700)))
    except (TypeError, ValueError):
        watts = 2700
    return max(0, watts)


def pv_running_check_interval_minutes(solar):
    try:
        minutes = int(float(solar.get("pv_running_check_interval_minutes", 30)))
    except (TypeError, ValueError):
        minutes = 30
    return max(1, minutes)


def pv_confirmation_delay_minutes(solar):
    try:
        minutes = int(float(solar.get("pv_confirmation_delay_minutes", 15)))
    except (TypeError, ValueError):
        minutes = 15
    return max(1, minutes)


def cached_energy_pv_production():
    provider = energy_provider_for_house("cesclans")
    if provider is None:
        return None
    snapshot = provider.get_status_snapshot()
    if snapshot.get("status") != "online":
        return None
    return numeric_or_none(snapshot.get("pv_production"))


def cached_goodwe_pv_production():
    """Backward-compatible alias for legacy callers."""
    return cached_energy_pv_production()


def pv_production_meets_threshold(solar):
    pv = cached_energy_pv_production()
    if pv is None:
        return None
    return pv >= minimum_pv_production_watts(solar)


def pv_production_available(solar):
    return pv_production_meets_threshold(solar) is True


def heater_reason_snapshot(cfg, heater_active=None, pump_active=None):
    solar = solar_heating_config(cfg)
    pv_adequate = pv_production_meets_threshold(solar)
    if heater_active not in (True, False) or pump_active not in (True, False):
        return {"key": "communication_error", "label": "Communication error", "class": "bad"}
    if not solar.get("enabled"):
        return {"key": "disabled", "label": "Disabled", "class": "gray"}
    if pump_active is False:
        return {"key": "pump_off", "label": "Pool pump OFF", "class": "info"}
    if heater_active is True:
        return {"key": "heating", "label": "Heating", "class": "ok"}
    if pv_adequate is None:
        return {"key": "communication_error", "label": "Communication error", "class": "bad"}
    if pv_adequate is True:
        return {"key": "waiting_check", "label": "Waiting for scheduled check", "class": "info"}
    return {"key": "waiting_pv", "label": "Waiting for PV production", "class": "warn"}


def pool_heater_snapshot(cfg):
    heater_dev, heater_no = heater_relay(cfg)
    pump_dev, pump_no = pump_relay(cfg)
    status = cached_relay_status(heater_dev, heater_no)
    heater_active = status.get("active")
    pump_active = relay_active_from_runtime(pump_dev, pump_no)
    solar = solar_heating_config(cfg)
    return {
        "device": heater_dev,
        "relay": heater_no,
        "active": heater_active,
        "mode": "AUTO" if solar.get("enabled") else "MANUAL",
        "last_update": status.get("updated_at"),
        "response": status.get("response", ""),
        "elapsed_ms": status.get("elapsed_ms", 0),
        "ok": status.get("ok"),
        "reason": heater_reason_snapshot(cfg, heater_active, pump_active)
    }


def set_solar_next_temperature_check(state, value):
    state["next_temperature_check_at"] = value.isoformat(timespec="seconds") if value else None


def reset_early_completion_timer(state):
    state["early_completion_started_at"] = None
    state["early_completion_target_temperature"] = None


def reset_solar_pv_confirmation(state):
    state["pv_confirmation_started_at"] = None
    state["pv_confirmation_due_at"] = None


def early_completion_snapshot(solar, runtime):
    started_at = parse_iso_datetime(runtime.get("early_completion_started_at"))
    target = runtime.get("early_completion_target_temperature")
    if not started_at:
        return {
            "active": False,
            "started_at": None,
            "target_temperature": target,
            "remaining_seconds": 0
        }
    confirmation = timedelta(minutes=early_completion_confirmation_minutes(solar))
    remaining = max(0, int(((started_at + confirmation) - now()).total_seconds()))
    return {
        "active": True,
        "started_at": started_at.isoformat(timespec="seconds"),
        "target_temperature": target,
        "remaining_seconds": remaining
    }


def reconcile_solar_temperature_schedule(cfg, force_due=False, current=None):
    solar = solar_heating_config(cfg)
    if current is None:
        current = now()
    today = date.today().isoformat()
    start, stop = solar_heating_window(cfg)
    interval = timedelta(seconds=solar_temperature_check_interval_seconds(solar))
    state = _runtime["solar_heating"]

    if (
        not solar.get("enabled")
        or state.get("safe_stop_started_date") == today
        or state.get("safe_stop_completed_date") == today
        or state.get("early_completion_completed_date") == today
        or current >= stop
    ):
        set_solar_next_temperature_check(state, None)
        reset_solar_pv_confirmation(state)
        return None

    if current < start:
        set_solar_next_temperature_check(state, start)
        return start

    last_check = parse_iso_datetime(state.get("last_temperature_check_at"))
    if force_due or not last_check or last_check.date() != current.date():
        next_check = current
    else:
        next_check = last_check + interval

    # Keep an overdue check due until solar_heating_tick() consumes it. Moving it
    # to `current` here allows status snapshots to postpone the check forever.

    if next_check >= stop:
        set_solar_next_temperature_check(state, None)
        return None
    set_solar_next_temperature_check(state, next_check)
    return next_check


def schedule_solar_temperature_recheck(cfg, checked_at):
    solar = solar_heating_config(cfg)
    _, stop = solar_heating_window(cfg)
    state = _runtime["solar_heating"]
    next_check = checked_at + timedelta(seconds=solar_temperature_check_interval_seconds(solar))
    set_solar_next_temperature_check(state, next_check if next_check < stop else None)


def log_solar_scheduled_check(checked_at, pv_production, water_temperature, solar, decision):
    def reading(value, unit):
        return "unavailable" if value is None else f"{value:.1f} {unit}"

    log(
        "Solar scheduled check | "
        f"time={checked_at.strftime('%Y-%m-%d %H:%M:%S')} | "
        f"pv={reading(pv_production, 'W')} | "
        f"water={reading(water_temperature, '°C')} | "
        f"pv_threshold={minimum_pv_production_watts(solar)} W | "
        f"water_threshold={float(solar.get('water_temperature_threshold', 29.0)):.1f} °C | "
        f"early_completion_threshold={early_completion_temperature(solar):.1f} °C | "
        f"decision={decision}",
        level="DEBUG"
    )


def solar_heating_snapshot(cfg=None):
    if cfg is None:
        cfg = load_config()
    solar = dict(solar_heating_config(cfg))
    start, stop = solar_heating_window(cfg)
    with _lock:
        reconcile_solar_temperature_schedule(cfg)
    with _lock:
        runtime = dict(_runtime["solar_heating"])
    runtime["early_completion"] = early_completion_snapshot(solar, runtime)
    water_temperature = water_temperature_snapshot()
    enabled = bool(solar.get("enabled"))
    status_label = "ACTIVE" if enabled else "DISABLED"
    status_details = solar_heating_status_details(solar, runtime, start, stop)
    return {
        **solar,
        "status": status_label,
        "status_label": status_label,
        "status_message": status_details["message"],
        "status_message_key": status_details["key"],
        "status_message_vars": status_details["vars"],
        "water_temperature": water_temperature,
        "operating_start_time": start.strftime("%H:%M"),
        "operating_stop_time": stop.strftime("%H:%M"),
        "safe_stop_time": stop.strftime("%H:%M"),
        "runtime": runtime
    }


def solar_heating_status_message(solar, runtime, start, stop):
    return solar_heating_status_details(solar, runtime, start, stop)["message"]


def solar_heating_status_details(solar, runtime, start, stop):
    if not solar.get("enabled"):
        return {"key": "solar.message.disabled", "vars": {}, "message": "Automation disabled"}

    current = now()
    today = date.today().isoformat()
    safe_stop = safe_stop_snapshot()

    if runtime.get("safe_stop_completed_date") == today:
        if runtime.get("early_completion_completed_date") == today:
            return {"key": "solar.message.completedEarly", "vars": {}, "message": "Automation completed early"}
        return {"key": "solar.message.completedToday", "vars": {}, "message": "Automation completed for today"}
    if runtime.get("safe_stop_started_date") == today:
        if safe_stop.get("running"):
            return {"key": "solar.message.safeStopRunning", "vars": {}, "message": "Safe Stop running..."}
        return {"key": "solar.message.completedToday", "vars": {}, "message": "Automation completed for today"}
    pv_confirmation_due = runtime.get("pv_confirmation_due_at")
    if pv_confirmation_due:
        return {
            "key": "solar.message.pvInsufficientConfirmation",
            "vars": {"time": fmt_time_iso(pv_confirmation_due)},
            "message": f"⚠ Produzione FV insufficiente\n\nSecondo controllo alle {fmt_time_iso(pv_confirmation_due)}"
        }
    early = runtime.get("early_completion", {})
    if early.get("active"):
        minutes = int((early.get("remaining_seconds", 0) + 59) / 60)
        if minutes > 0:
            return {
                "key": "solar.message.earlyCompletionTimer",
                "vars": {"minutes": minutes},
                "message": f"Early completion timer:\n{minutes} min remaining"
            }
        return {"key": "solar.message.maintainingTarget", "vars": {}, "message": "Maintaining target temperature..."}
    if runtime.get("auto_started_date") == today:
        next_check = runtime.get("next_temperature_check_at")
        parsed_next_check = parse_iso_datetime(next_check)
        if parsed_next_check and current < parsed_next_check:
            return {
                "key": "solar.message.waitingForNextCheck",
                "vars": {"time": fmt_time_iso(next_check)},
                "message": f"Waiting for next temperature check ({fmt_time_iso(next_check)})"
            }
        return {"key": "solar.message.heatingAlreadyRunning", "vars": {}, "message": "Heating already running"}
    if current < start:
        return {
            "key": "solar.message.waitingForStart",
            "vars": {"time": start.strftime("%H:%M")},
            "message": f"Waiting for start time ({start.strftime('%H:%M')})"
        }
    if current >= stop:
        return {"key": "solar.message.completedToday", "vars": {}, "message": "Automation completed for today"}
    next_check = runtime.get("next_temperature_check_at")
    parsed_next_check = parse_iso_datetime(next_check)
    if parsed_next_check and current < parsed_next_check:
        return {
            "key": "solar.message.waitingForNextCheck",
            "vars": {"time": fmt_time_iso(next_check)},
            "message": f"Waiting for next temperature check ({fmt_time_iso(next_check)})"
        }
    return {"key": "solar.message.checkingWaterTemperature", "vars": {}, "message": "Checking water temperature..."}


def fmt_time_iso(value):
    parsed = parse_iso_datetime(value)
    if parsed:
        return parsed.strftime("%H:%M")
    return str(value or "-")


def latest_solar_heating_event(today):
    for event in reversed(load_event_log()):
        if event.get("date") != today:
            continue
        if event.get("device") == "Solar Heating" and event.get("relay") == "Automation":
            return event
    return None


def record_solar_event(event, reason=""):
    record_event("Solar Heating", "Automation", event, "Solar Heating", reason)
    log(f"SOLAR_HEATING {event} | {reason}")


def relay_active_from_runtime(device_id, relay_number):
    return _runtime.get("relays", {}).get(f"{device_id}:{relay_number}", {}).get("active")


def complete_solar_for_today(state, today, event, reason=""):
    state["safe_stop_completed_date"] = today
    set_solar_next_temperature_check(state, None)
    reset_solar_pv_confirmation(state)
    reset_early_completion_timer(state)
    save_state()
    record_solar_event(event, reason)
    notify_automation_completed(load_config(), early=state.get("early_completion_completed_date") == today)


def start_solar_safe_stop(state, today, event, source, early=False):
    state["safe_stop_started_date"] = today
    if early:
        state["early_completion_completed_date"] = today
    set_solar_next_temperature_check(state, None)
    reset_solar_pv_confirmation(state)
    save_state()
    record_solar_event(event)
    stopped, message = start_heater_safe_stop(source=source)
    if stopped:
        complete_solar_for_today(
            state,
            today,
            "Automation completed early" if early else "Automatic Safe Stop completed"
        )
    else:
        record_solar_event("Automatic heater stop failed", message)


def update_early_completion_timer(solar, state, water_temperature, checked_at):
    target = early_completion_temperature(solar)
    confirmation_seconds = early_completion_confirmation_minutes(solar) * 60
    started_at = parse_iso_datetime(state.get("early_completion_started_at"))
    if water_temperature < target:
        was_running = bool(started_at)
        elapsed_seconds = max(0, int((checked_at - started_at).total_seconds())) if started_at else 0
        remaining_seconds = max(0, confirmation_seconds - elapsed_seconds)
        reset_early_completion_timer(state)
        return {
            "completed": False,
            "status": "cancelled" if was_running else "cancelled",
            "target_temperature": target,
            "elapsed_seconds": elapsed_seconds,
            "remaining_seconds": remaining_seconds
        }

    saved_target = state.get("early_completion_target_temperature")
    if not started_at or saved_target != target:
        started_at = checked_at
        state["early_completion_started_at"] = started_at.isoformat(timespec="seconds")
        state["early_completion_target_temperature"] = target

    elapsed_seconds = max(0, int((checked_at - started_at).total_seconds()))
    remaining_seconds = max(0, confirmation_seconds - elapsed_seconds)
    completed = elapsed_seconds >= confirmation_seconds
    return {
        "completed": completed,
        "status": "completed" if completed else "running",
        "target_temperature": target,
        "elapsed_seconds": elapsed_seconds,
        "remaining_seconds": remaining_seconds
    }


def record_early_completion_debug(water_temperature, details):
    temperature = "unavailable" if water_temperature is None else f"{water_temperature:.1f} °C"
    record_solar_event(
        "Early Completion debug",
        " | ".join([
            f"Current water temperature: {temperature}",
            f"Early Completion Temperature: {details.get('target_temperature', 31.0):.1f} °C",
            f"Confirmation timer elapsed: {format_runtime(details.get('elapsed_seconds', 0))}",
            f"Confirmation timer remaining: {format_runtime(details.get('remaining_seconds', 0))}",
            f"Timer status: {details.get('status', 'cancelled')}"
        ])
    )


def early_completion_deadline_reached(solar, state, current):
    started_at = parse_iso_datetime(state.get("early_completion_started_at"))
    if not started_at:
        return False
    confirmation = timedelta(minutes=early_completion_confirmation_minutes(solar))
    return current >= started_at + confirmation


def handle_solar_running_pv_check(solar, state, today, current, heater_active):
    if state.get("auto_started_date") != today or heater_active is not True:
        reset_solar_pv_confirmation(state)
        return False

    confirmation_due = parse_iso_datetime(state.get("pv_confirmation_due_at"))
    if confirmation_due:
        if current < confirmation_due:
            return True
        state["last_pv_running_check_at"] = current.isoformat(timespec="seconds")
        pv_adequate = pv_production_meets_threshold(solar)
        if pv_adequate is not False:
            reset_solar_pv_confirmation(state)
            save_state()
            if pv_adequate is True:
                record_solar_event("PV production recovered")
            return False
        start_solar_safe_stop(
            state,
            today,
            "Automatic Safe Stop started",
            "solar_heating_pv_confirmation"
        )
        return True

    last_check = parse_iso_datetime(state.get("last_pv_running_check_at"))
    interval = timedelta(minutes=pv_running_check_interval_minutes(solar))
    if last_check and current < last_check + interval:
        return False

    state["last_pv_running_check_at"] = current.isoformat(timespec="seconds")
    pv_adequate = pv_production_meets_threshold(solar)
    if pv_adequate is not False:
        save_state()
        return False

    confirmation_due = current + timedelta(minutes=pv_confirmation_delay_minutes(solar))
    state["pv_confirmation_started_at"] = current.isoformat(timespec="seconds")
    state["pv_confirmation_due_at"] = confirmation_due.isoformat(timespec="seconds")
    save_state()
    record_solar_event("PV production insufficient", f"Second check at {confirmation_due.strftime('%H:%M')}")
    return True


def solar_heating_tick():
    cfg = load_config()
    solar = solar_heating_config(cfg)
    today = date.today().isoformat()
    state = _runtime["solar_heating"]

    if not solar.get("enabled"):
        set_solar_next_temperature_check(state, None)
        reset_solar_pv_confirmation(state)
        reset_early_completion_timer(state)
        return

    current = now()
    start, stop = solar_heating_window(cfg)
    pump_dev, pump_no = pump_relay(cfg)
    heater_dev, heater_no = heater_relay(cfg)

    if state.get("safe_stop_started_date") == today:
        safe_stop = safe_stop_snapshot()
        if (
            state.get("safe_stop_completed_date") != today
            and safe_stop.get("state") == "COMPLETED"
            and not safe_stop.get("running")
        ):
            state["safe_stop_completed_date"] = today
            reset_early_completion_timer(state)
            save_state()
            event = "Automation completed early" if state.get("early_completion_completed_date") == today else "Automatic Safe Stop completed"
            record_solar_event(event)
            notify_automation_completed(cfg, early=state.get("early_completion_completed_date") == today)
        return

    if state.get("safe_stop_completed_date") == today or state.get("early_completion_completed_date") == today:
        set_solar_next_temperature_check(state, None)
        return

    if current < start:
        reconcile_solar_temperature_schedule(cfg)
        return

    if current >= stop:
        heater_active = relay_active_from_runtime(heater_dev, heater_no)
        if heater_active is True:
            start_solar_safe_stop(
                state,
                today,
                "Automatic Safe Stop started",
                "solar_heating_scheduled_stop"
            )
        else:
            complete_solar_for_today(state, today, "Automation completed for today", "Stop time reached")
        return

    heater_active = relay_active_from_runtime(heater_dev, heater_no)
    if handle_solar_running_pv_check(solar, state, today, current, heater_active):
        return

    if early_completion_deadline_reached(solar, state, current):
        water_temperature = water_temperature_snapshot()
        if water_temperature is None:
            details = {
                "status": "cancelled",
                "target_temperature": early_completion_temperature(solar),
                "elapsed_seconds": early_completion_confirmation_minutes(solar) * 60,
                "remaining_seconds": 0
            }
            reset_early_completion_timer(state)
            record_early_completion_debug(water_temperature, details)
            save_state()
            return
        early_details = update_early_completion_timer(solar, state, water_temperature, current)
        record_early_completion_debug(water_temperature, early_details)
        save_state()
        if early_details.get("completed"):
            start_solar_safe_stop(
                state,
                today,
                "Early completion Safe Stop started",
                "solar_heating_early_completion",
                early=True
            )
        return

    next_check = reconcile_solar_temperature_schedule(cfg, current=current)
    if not next_check or current < next_check:
        return

    checked_at = current
    state["last_temperature_check_at"] = checked_at.isoformat(timespec="seconds")
    set_solar_next_temperature_check(state, None)
    pv_production = cached_energy_pv_production()
    water_temperature = water_temperature_snapshot()
    if water_temperature is None:
        reset_early_completion_timer(state)
        record_early_completion_debug(water_temperature, {
            "status": "cancelled",
            "target_temperature": early_completion_temperature(solar),
            "elapsed_seconds": 0,
            "remaining_seconds": early_completion_confirmation_minutes(solar) * 60
        })
        record_solar_event("Automatic heating skipped", "Water temperature unavailable")
        log_solar_scheduled_check(checked_at, pv_production, water_temperature, solar, "skip: water temperature unavailable")
        schedule_solar_temperature_recheck(cfg, checked_at)
        save_state()
        return

    early_details = update_early_completion_timer(solar, state, water_temperature, checked_at)
    record_early_completion_debug(water_temperature, early_details)
    if early_details.get("completed"):
        log_solar_scheduled_check(checked_at, pv_production, water_temperature, solar, "safe stop: early completion threshold confirmed")
        start_solar_safe_stop(
            state,
            today,
            "Early completion Safe Stop started",
            "solar_heating_early_completion",
            early=True
        )
        return

    threshold = float(solar.get("water_temperature_threshold", 29.0))
    if water_temperature >= threshold:
        record_solar_event(f"Temperature OK (Water {water_temperature:.1f}°C)")
        log_solar_scheduled_check(checked_at, pv_production, water_temperature, solar, "heater remains off: water temperature at or above threshold")
        schedule_solar_temperature_recheck(cfg, checked_at)
        save_state()
        return

    pv_threshold = minimum_pv_production_watts(solar)
    if pv_production is None or pv_production < pv_threshold:
        pv_reason = "PV production unavailable" if pv_production is None else f"PV production below {pv_threshold} W"
        record_solar_event(
            "Automatic heating skipped",
            pv_reason
        )
        log_solar_scheduled_check(checked_at, pv_production, water_temperature, solar, f"skip: {pv_reason.lower()}")
        schedule_solar_temperature_recheck(cfg, checked_at)
        save_state()
        return

    pump_active = relay_active_from_runtime(pump_dev, pump_no)
    started_any = False
    if pump_active is not True:
        pump_status = set_one_raw(cfg, pump_dev, pump_no, True, source="solar_heating")
        if not relay_set_confirmed(pump_status, True):
            record_solar_event("Automatic heating skipped", "Pump start was not confirmed")
            log_solar_scheduled_check(checked_at, pv_production, water_temperature, solar, "skip: pump start not confirmed")
            schedule_solar_temperature_recheck(cfg, checked_at)
            save_state()
            return
        started_any = True
    if heater_active is not True:
        heater_status = set_one_raw(cfg, heater_dev, heater_no, True, source="solar_heating")
        if not relay_set_confirmed(heater_status, True):
            record_solar_event("Automatic heating skipped", "Heater start was not confirmed")
            log_solar_scheduled_check(checked_at, pv_production, water_temperature, solar, "skip: heater start not confirmed")
            schedule_solar_temperature_recheck(cfg, checked_at)
            save_state()
            return
        started_any = True
    state["auto_started_date"] = today
    state["safe_stop_started_date"] = None
    state["safe_stop_completed_date"] = None
    state["last_pv_running_check_at"] = checked_at.isoformat(timespec="seconds")
    reset_solar_pv_confirmation(state)
    if started_any:
        record_solar_event(f"Automatic heating started (Water {water_temperature:.1f}°C)")
        log_solar_scheduled_check(checked_at, pv_production, water_temperature, solar, "heater activated")
        notify_automation_started(cfg, water_temperature, threshold)
    else:
        record_solar_event(f"Heating already running (Water {water_temperature:.1f}°C)")
        log_solar_scheduled_check(checked_at, pv_production, water_temperature, solar, "heater already active")
    schedule_solar_temperature_recheck(cfg, checked_at)
    save_state()


def scheduler_tick():
    cfg = load_config()
    p = cfg["pump"]
    today = date.today().isoformat()
    sched = _runtime["scheduler"]

    # A mezzanotte/nuovo giorno: riabilita automatico sospeso e chiudi timer manuali scaduti.
    if sched.get("auto_suspended_date") and sched["auto_suspended_date"] != today:
        sched["auto_suspended_date"] = None
        log("SCHEDULER ripristino automatico giornaliero")
        save_state()

    if not p.get("enabled", True) or p.get("mode") == "disabled":
        return

    current = now()
    pump_active = pump_status_from_runtime(cfg)

    # Timer manuale: se scaduto spegne la pompa.
    manual_until = sched.get("manual_run_until")
    if manual_until:
        try:
            until = datetime.fromisoformat(manual_until)
            if current >= until:
                if pump_active is not False:
                    set_one(cfg, p["device"], p["relay"], False, source="manual_timer")
                sched["manual_run_until"] = None
                sched["auto_suspended_date"] = today
                log("SCHEDULER timer manuale terminato, automatico sospeso fino a domani")
                save_state()
        except Exception:
            sched["manual_run_until"] = None
            save_state()
        return

    if p.get("mode") != "auto":
        return
    if sched.get("auto_suspended_date") == today:
        return

    start, end = pump_window(cfg)
    should_on = start <= current < end
    action_key = f"{today}:{'on' if should_on else 'off'}"

    # Reconcile: se il programma parte a metà fascia, riallinea lo stato reale.
    if should_on and pump_active is not True:
        record_event("Scheduler", "Pump", "Scheduler start", "Scheduler", "Automatic schedule")
        set_one(cfg, p["device"], p["relay"], True, source="scheduler")
        sched["last_action_key"] = action_key
        save_state()
    elif (not should_on) and current >= end and pump_active is True:
        record_event("Scheduler", "Pump", "Scheduler stop", "Scheduler", "Automatic schedule")
        set_one(cfg, p["device"], p["relay"], False, source="scheduler")
        sched["last_action_key"] = action_key
        save_state()


def background_loop():
    cfg = load_config()
    log(f"=== Pool & Garden Controller {app_version(cfg)} avviato ===")
    record_event("Application", "", "Application started", "System", f"Version {app_version(cfg)}")
    st = load_state()
    if isinstance(st.get("scheduler"), dict):
        _runtime["scheduler"].update(st["scheduler"])
    if isinstance(st.get("solar_heating"), dict):
        _runtime["solar_heating"].update(st["solar_heating"])
    refresh_all_relays(source="startup", notify_on_error=True)
    while True:
        try:
            refresh_all_relays(source="poll", notify_on_error=True)
            scheduler_tick()
            solar_heating_tick()
        except Exception as exc:
            log(f"ERRORE background: {exc}")
        time.sleep(load_config()["app"].get("poll_seconds", 30))


def record_application_stopped():
    try:
        record_event("Application", "", "Application stopped", "System", "Process exiting")
    except Exception:
        pass


atexit.register(record_application_stopped)


def start_temperature_service():
    global temperature_service
    if temperature_service is None:
        temperature_service = TemperatureService(
            DEFAULT_TEMPERATURE_SENSORS,
            poll_seconds=30,
            event_callback=record_event
        )
    temperature_service.start()
    return temperature_service


def stop_temperature_service():
    if temperature_service is not None:
        temperature_service.stop()


atexit.register(stop_temperature_service)


def start_weather_service():
    global weather_service
    if weather_service is None:
        cfg = load_config()
        weather_service = WeatherService(
            cfg.get("weather", DEFAULT_WEATHER_CONFIG),
            event_callback=record_event,
            log_callback=log
        )
    weather_service.start()
    return weather_service


def stop_weather_service():
    if weather_service is not None:
        weather_service.stop()


atexit.register(stop_weather_service)


def start_netatmo_service():
    global netatmo_service
    if netatmo_service is None:
        cfg = load_config()
        netatmo_service = NetatmoService(
            cfg.get("netatmo", DEFAULT_NETATMO_CONFIG),
            NETATMO_CONFIG_PATH,
            NETATMO_TOKENS_PATH,
            NETATMO_RAIN_HISTORY_PATH,
            event_callback=record_event,
            log_callback=log
        )
    netatmo_service.start()
    return netatmo_service


def stop_netatmo_service():
    if netatmo_service is not None:
        netatmo_service.stop()


atexit.register(stop_netatmo_service)


def weather_snapshot_for_history():
    snapshot = weather_service.snapshot() if weather_service else {}
    result = dict(snapshot) if isinstance(snapshot, dict) else {}
    locations = dict(result.get("locations") or {})
    netatmo = netatmo_service.snapshot() if netatmo_service else {}
    for device in [
        *(netatmo.get("stations") or {}).values(),
        *(netatmo.get("modules") or {}).values()
    ]:
        metric = (device.get("metrics") or {}).get("temperature")
        device_id = str(device.get("id") or "").strip()
        if not device_id or not metric or metric.get("value") is None:
            continue
        locations[f"netatmo:{device_id}"] = {
            "label": device.get("name") or device_id,
            "metrics": {
                "temperature": {
                    **metric,
                    "source": "netatmo",
                    "online": device.get("online") is not False,
                    "placeholder": False
                }
            }
        }
    result["locations"] = locations
    return result


def netatmo_device_house_id(device):
    home_id = str((device or {}).get("home_id") or "")
    configured = {
        "5984356be6da232ab78b4a22": "opicina",
        "659339314bc6fec2770fff76": "cesclans"
    }.get(home_id)
    if configured:
        return configured
    name = str((device or {}).get("home_name") or "").strip().lower()
    if "cesclans" in name or "cezklanc" in name:
        return "cesclans"
    if "opicina" in name or "opcine" in name:
        return "opicina"
    return None


def start_history_service():
    global history_service
    if history_service is None:
        cfg = load_config()
        history_cfg = cfg.get("history", DEFAULT_HISTORY_CONFIG)
        history_service = HistoryService(
            HISTORY_DB_PATH,
            weather_snapshot_for_history,
            sample_seconds=history_cfg.get("sample_seconds", 300),
            enabled=history_cfg.get("enabled", True),
            log_callback=log
        )
    history_service.start()
    return history_service


def stop_history_service():
    if history_service is not None:
        history_service.stop()


atexit.register(stop_history_service)


def cesclans_weather_snapshot():
    return netatmo_service.snapshot() if netatmo_service else {}


def cesclans_climate_snapshot():
    try:
        hvac_devices = intesis.get_all_devices() if intesis.is_configured() else []
        intesis_driver = getattr(intesis, "_default_driver", None)
        intesis_status = intesis_driver.status() if intesis_driver is not None else {}
        if intesis_driver is not None:
            intesis_status["poll_seconds"] = intesis_driver.poll_seconds
    except Exception as exc:
        log(f"Winter energy Intesis snapshot unavailable: {exc}", level="DEBUG")
        hvac_devices = []
        intesis_status = {}
    return {"devices": hvac_devices, "status": intesis_status}


def cesclans_pool_module_enabled():
    context = house_registry.get("cesclans")
    return bool(context and context.module_enabled("Pool"))


def cesclans_pool_status_snapshot():
    cfg = load_config()
    pump = cfg.get("pump") if isinstance(cfg.get("pump"), dict) else {}
    return {
        "enabled": cesclans_pool_module_enabled(),
        "mode": pump.get("mode"),
        "season_enabled": bool(pump.get("enabled", True)) and pump.get("mode") != "disabled",
    }


def configure_provider_registry():
    with _provider_registry_lock:
        if provider_registry.get("cesclans") is not None:
            return
        context = house_registry.get("cesclans")
        if context is None:
            return
        providers = HouseProviders(
            house_id=context.id,
            energy=GoodWeEnergyProvider(goodwe_status_snapshot, goodwe_viewer_snapshot)
            if context.provider_id("energy") == "goodwe" else None,
            weather=NetatmoWeatherProvider(cesclans_weather_snapshot)
            if context.provider_id("weather") == "netatmo" else None,
            climate=IntesisClimateProvider(cesclans_climate_snapshot)
            if context.provider_id("climate") == "intesis" else None,
            pool=CesclansPoolProvider(cesclans_pool_module_enabled, cesclans_pool_status_snapshot)
            if context.provider_id("pool") == "cesclans_pool" else None,
        )
        provider_registry.register(providers)


def providers_for_house(house_id=None):
    context = current_house_context(house_id)
    if context is None:
        return None
    configure_provider_registry()
    return provider_registry.get(context.id)


def energy_provider_for_house(house_id=None):
    providers = providers_for_house(house_id)
    return providers.energy if providers else None


def winter_snapshot_provider():
    providers = providers_for_house("cesclans")
    return {
        "weather": providers.weather.get_snapshot() if providers and providers.weather else {},
        "energy": providers.energy.get_status_snapshot() if providers and providers.energy else {},
        "climate": providers.climate.get_snapshot() if providers and providers.climate else {},
    }


def winter_energy_context_provider():
    source = "config.pump.mode / config.pump.enabled (pool scheduler)"
    try:
        cfg = load_config()
        pump = cfg.get("pump")
    except Exception:
        return {"energy_context": "UNKNOWN", "source": source}
    if not isinstance(pump, dict):
        return {"energy_context": "UNKNOWN", "source": source}
    # Keep this mapping identical to the existing scheduler disable condition.
    context = "POOL_DISABLED" if not pump.get("enabled", True) or pump.get("mode") == "disabled" else "POOL_ACTIVE"
    return {"energy_context": context, "source": source}


def start_winter_energy_service():
    global winter_energy_service
    if winter_energy_service is None:
        cfg = load_config()
        winter_energy_service = WinterEnergyService(
            HISTORY_DB_PATH,
            cfg.get("winter", DEFAULT_WINTER_CONFIG),
            winter_snapshot_provider,
            energy_context_provider=winter_energy_context_provider,
            log_callback=log,
        )
    winter_energy_service.start()
    return winter_energy_service


def stop_winter_energy_service():
    if winter_energy_service is not None:
        winter_energy_service.stop()


atexit.register(stop_winter_energy_service)


def start_intesis_driver():
    if not intesis.is_configured():
        cfg = load_config()
        intesis.configure(cfg.get("intesis", {}), log_callback=log)
    intesis.connect()
    return intesis


def stop_intesis_driver():
    intesis.disconnect()


atexit.register(stop_intesis_driver)


@app.before_request
def bind_request_house_context():
    first_path_segment = request.path.strip("/").split("/", 1)[0].lower()
    g.current_house = house_registry.get(first_path_segment) or house_registry.default("cesclans")


@app.route("/")
def index():
    return render_template("control_center.html", houses=load_houses_metadata())


def render_house_dashboard(house_id):
    cfg = load_config()
    lang = selected_language(cfg, persist_browser=True)
    context = current_house_context(house_id)
    if context is None:
        abort(404)
    return render_template("index.html", language=lang, house=context.to_public_dict())


@app.route("/cesclans")
def cesclans_dashboard():
    return render_house_dashboard("cesclans")


@app.route("/opicina")
def opicina_dashboard():
    return render_house_dashboard("opicina")


@app.route("/viewer")
def viewer():
    return render_house_viewer("cesclans")


def render_house_viewer(house_id):
    cfg = load_config()
    require_viewer_access(cfg)
    context = current_house_context(house_id)
    if context is None:
        abort(404)
    return render_template(
        "viewer.html",
        refresh_seconds=viewer_refresh_seconds(cfg),
        house=context.to_public_dict()
    )


@app.route("/viewer/cesclans")
def viewer_cesclans():
    return render_house_viewer("cesclans")


@app.route("/viewer/opicina")
def viewer_opicina():
    return render_house_viewer("opicina")


@app.route("/winter")
def winter_dashboard():
    return render_template("winter.html", viewer=False, back_href="/cesclans")


@app.route("/viewer/winter")
def viewer_winter_dashboard():
    cfg = load_config()
    require_viewer_access(cfg)
    return render_template("winter.html", viewer=True, back_href=viewer_href())


@app.route("/viewer/history")
def viewer_history():
    cfg = load_config()
    require_viewer_access(cfg)
    lang = selected_language(cfg, persist_browser=True)
    return render_template(
        "history.html",
        language=lang,
        back_href=viewer_href(),
        back_i18n="nav.viewer",
        initial_location="cesclans",
        locked_location=True,
        viewer=True,
        viewer_nav={
            "cesclans": viewer_href("/viewer/cesclans"),
            "opicina": viewer_href("/viewer/opicina"),
            "comparisons": viewer_href("/viewer/comparisons"),
            "winter": viewer_href("/viewer/winter"),
            "system": viewer_href("/viewer")
        }
    )


def render_viewer_house_history(location, back_path):
    cfg = load_config()
    require_viewer_access(cfg)
    lang = selected_language(cfg, persist_browser=True)
    return render_template(
        "history.html",
        language=lang,
        back_href=viewer_href(back_path),
        back_i18n="nav.viewer",
        initial_location=location,
        locked_location=True,
        viewer=True,
        viewer_nav={
            "cesclans": viewer_href("/viewer/cesclans"),
            "opicina": viewer_href("/viewer/opicina"),
            "comparisons": viewer_href("/viewer/comparisons"),
            "winter": viewer_href("/viewer/winter"),
            "system": viewer_href("/viewer")
        }
    )


@app.route("/viewer/cesclans/history")
def viewer_cesclans_history():
    return render_viewer_house_history("cesclans", "/viewer/cesclans")


@app.route("/viewer/opicina/history")
def viewer_opicina_history():
    return render_viewer_house_history("opicina", "/viewer/opicina")


@app.route("/viewer/comparisons")
def viewer_comparisons():
    return render_viewer_house_history("compare", "/viewer")


@app.route("/viewer/report")
def viewer_report():
    cfg = load_config()
    require_viewer_access(cfg)
    lang = selected_language(cfg, persist_browser=True)
    return render_template("statistics.html", language=lang, back_href=viewer_href(), back_i18n="nav.viewer")


@app.route("/viewer/event-log")
def viewer_event_log():
    cfg = load_config()
    require_viewer_access(cfg)
    lang = selected_language(cfg, persist_browser=True)
    return render_template("event_log.html", language=lang, back_href=viewer_href(), back_i18n="nav.viewer")


@app.route("/history")
def history():
    cfg = load_config()
    lang = selected_language(cfg, persist_browser=True)
    return render_template("history.html", language=lang, back_href="/", initial_location="opicina")


def render_house_history(location, back_href):
    cfg = load_config()
    lang = selected_language(cfg, persist_browser=True)
    return render_template(
        "history.html",
        language=lang,
        back_href=back_href,
        initial_location=location,
        locked_location=True
    )


@app.route("/cesclans/history")
def cesclans_history():
    return render_house_history("cesclans", "/cesclans")


@app.route("/opicina/history")
def opicina_history():
    return render_house_history("opicina", "/opicina")


@app.route("/comparisons")
def comparisons():
    return render_house_history("compare", "/")


@app.route("/api/i18n")
def api_i18n():
    cfg = load_config()
    lang = selected_language(cfg, persist_browser=True)
    loaded_lang, translations = load_language_file(lang)
    return jsonify({
        "ok": True,
        "language": loaded_lang,
        "languages": supported_languages(),
        "translations": translations
    })


@app.route("/api/language", methods=["POST"])
def api_language():
    data = request.get_json(force=True)
    lang = set_selected_language(data.get("language"))
    loaded_lang, translations = load_language_file(lang)
    return jsonify({
        "ok": True,
        "language": loaded_lang,
        "languages": supported_languages(),
        "translations": translations
    })


@app.route("/api/status")
def api_status():
    cfg = load_config()
    house_providers = providers_for_house()
    app_cfg = cfg.get("app", {})
    version = app_version(cfg)
    port = int(app_cfg.get("port", 5000))
    start, end = pump_window(cfg)
    pump = cfg["pump"].copy()
    pump["computed_stop_time"] = end.strftime("%H:%M")
    pump["active"] = pump_status_from_runtime(cfg)
    pump["remaining_seconds"] = max(0, int((end - now()).total_seconds())) if pump["active"] else 0
    with _lock:
        payload = {
            "version": version,
            "port": port,
            "app": {"version": version, "port": port},
            "refresh_seconds": 60,
            "language": selected_language(cfg),
            "languages": supported_languages(),
            "app_meta": f"{version} · server su porta {port}",
            "config": public_config(cfg),
            "devices": _runtime["devices"],
            "relays": _runtime["relays"],
            "temperatures": temperature_service.snapshot() if temperature_service else {},
            "weather": weather_service.snapshot() if weather_service else {},
            "netatmo": house_providers.weather.get_snapshot() if house_providers and house_providers.weather else {},
            "goodwe": house_providers.energy.get_status_snapshot() if house_providers and house_providers.energy else {},
            "heater": pool_heater_snapshot(cfg),
            "heater_statistics": heater_statistics_snapshot(),
            "statistics": statistics_snapshot(cfg),
            "pump": pump,
            "solar_heating": solar_heating_snapshot(cfg),
            "runtime": {
                "started_at": _runtime["started_at"],
                "last_poll": _runtime["last_poll"],
                "last_command": _runtime["last_command"],
                "scheduler": _runtime["scheduler"],
                "solar_heating": _runtime["solar_heating"],
                "heater_safe_stop": safe_stop_snapshot()
            },
            "now": now().isoformat(timespec="seconds")
        }
        try:
            log("/api/status JSON returned:\n" + json.dumps(payload, indent=2, ensure_ascii=False), level="DEBUG")
        except Exception as exc:
            log(f"/api/status JSON logging failed: {exc}", level="DEBUG")
        return jsonify(payload)


@app.route("/api/viewer/status")
def api_viewer_status():
    cfg = load_config()
    house_providers = providers_for_house("cesclans")
    require_viewer_access(cfg)
    version = app_version(cfg)
    pump_cfg = cfg.get("pump", {})
    heater_dev, heater_no = heater_relay(cfg)
    garden_cfg = cfg.get("devices", {}).get("garden", {}).get("relays", {})
    garden_relay_no = next(iter(garden_cfg.keys()), "1")
    start, stop = solar_heating_window(cfg)
    solar = solar_heating_snapshot(cfg)
    heater_stats = heater_statistics_snapshot()
    current = now().isoformat(timespec="seconds")
    pump_active = viewer_relay_state(pump_cfg.get("device", "pool"), str(pump_cfg.get("relay", "2")))
    _, pump_stop = pump_window(cfg)
    pump = {
        "mode": pump_cfg.get("mode", "auto"),
        "duration_hours": pump_cfg.get("duration_hours", 6),
        "computed_stop_time": pump_stop.strftime("%H:%M"),
        "active": pump_active,
        "remaining_seconds": max(0, int((pump_stop - now()).total_seconds())) if pump_active else 0
    }
    return jsonify({
        "ok": True,
        "refresh_seconds": viewer_refresh_seconds(cfg),
        "energy": house_providers.energy.get_dashboard_snapshot() if house_providers and house_providers.energy else {},
        "temperatures": temperature_viewer_snapshot(),
        "weather": weather_service.snapshot() if weather_service else {},
        "netatmo": house_providers.weather.get_snapshot() if house_providers and house_providers.weather else {},
        "pool": {
            "pump": pump,
            "heater_active": viewer_relay_state(heater_dev, heater_no)
        },
        "heater": pool_heater_snapshot(cfg),
        "garden": {
            "lights_active": viewer_relay_state("garden", garden_relay_no)
        },
        "solar_heating": {
            "enabled": bool(solar.get("enabled")),
            "status": solar.get("status"),
            "status_label": solar.get("status_label"),
            "status_message": solar.get("status_message"),
            "status_message_key": solar.get("status_message_key"),
            "status_message_vars": solar.get("status_message_vars"),
            "water_temperature": solar.get("water_temperature"),
            "water_temperature_threshold": solar.get("water_temperature_threshold"),
            "operating_start_time": start.strftime("%H:%M"),
            "operating_stop_time": stop.strftime("%H:%M"),
            "next_temperature_check": (solar.get("runtime") or {}).get("next_temperature_check_at"),
            "heating_today_seconds": heater_stats.get("daily_seconds", 0)
        },
        "statistics": viewer_statistics_snapshot(cfg),
        "general": {
            "version": version,
            "current_time": current,
            "last_dashboard_update": current,
            "last_successful_refresh": _runtime.get("last_poll")
        }
    })


@app.route("/api/relay", methods=["POST"])
def api_relay():
    data = request.get_json(force=True)
    cfg = load_config()
    dev = data["device"]
    relay = str(data["relay"])
    active = bool(data["active"])
    status = set_one(cfg, dev, relay, active, source="manual")
    record_event(
        relay_label(cfg, dev, relay),
        f"Relay {relay}",
        "Manual command",
        "Manual",
        f"Requested {'ON' if active else 'OFF'}"
    )

    # Logica speciale pompa: manuale con durata oppure spegnimento con sospensione fino a domani.
    p = cfg["pump"]
    if dev == p["device"] and relay == str(p["relay"]) and not status.get("ignored"):
        with _lock:
            if active:
                hours = float(p.get("duration_hours", 6))
                _runtime["scheduler"]["manual_run_until"] = (now() + timedelta(hours=hours)).isoformat(timespec="seconds")
                _runtime["scheduler"]["auto_suspended_date"] = None
                log(f"POMPA manuale ON, timer impostato per {hours} ore")
            else:
                _runtime["scheduler"]["manual_run_until"] = None
                _runtime["scheduler"]["auto_suspended_date"] = date.today().isoformat()
                log("POMPA manuale OFF, automatico sospeso fino a domani")
            save_state()
    return jsonify({"ok": status.get("ok"), "status": status})


@app.route("/api/relays/refresh", methods=["POST"])
def api_relays_refresh():
    refresh_all_relays(source="manual_refresh", notify_on_error=True)
    return jsonify({
        "ok": all(d.get("ok") for d in _runtime.get("devices", {}).values()),
        "devices": _runtime["devices"],
        "relays": _runtime["relays"],
        "last_successful_refresh": _runtime.get("last_poll")
    })


@app.route("/api/heater/safe_stop", methods=["POST"])
def api_heater_safe_stop():
    started, message = start_heater_safe_stop(source="manual_safe_stop")
    response = {"ok": started, "message": message, "started": started, "safe_stop": safe_stop_snapshot()}
    return jsonify(response), (200 if started else 502)


@app.route("/api/heater/statistics/reset_seasonal", methods=["POST"])
def api_heater_reset_seasonal():
    stats = reset_heater_seasonal_statistics()
    log("HEATER_STATS seasonal runtime reset")
    return jsonify({"ok": True, "heater_statistics": stats})


@app.route("/api/statistics")
def api_statistics():
    cfg = load_config()
    return jsonify({
        "ok": True,
        "statistics": statistics_snapshot(cfg),
        "last_successful_refresh": _runtime.get("last_poll"),
        "now": now().isoformat(timespec="seconds")
    })


@app.route("/api/statistics/reset_season", methods=["POST"])
def api_reset_statistics_season():
    stats = reset_season_statistics()
    log("STATS seasonal runtime reset for all relays")
    return jsonify({"ok": True, "statistics": stats})


@app.route("/api/history")
def api_history():
    period = request.args.get("period", "24h")
    location = request.args.get("location", "opicina")
    if history_service is None:
        return jsonify({
            "ok": False,
            "error": "History service unavailable",
            "statistics": {},
            "graph": {"series": []}
        }), 503
    return jsonify({
        "ok": True,
        **history_service.snapshot(period=period, location=location),
        "now": now().isoformat(timespec="seconds")
    })


@app.route("/api/netatmo/history")
def api_netatmo_history():
    period = request.args.get("period", "24h")
    house_id = str(request.args.get("house") or "cesclans")
    if history_service is None or netatmo_service is None:
        return jsonify({"ok": False, "error": "History service unavailable", "series": []}), 503
    snapshot = netatmo_service.snapshot() or {}
    labels = {}
    for device in [
        *(snapshot.get("stations") or {}).values(),
        *(snapshot.get("modules") or {}).values()
    ]:
        if netatmo_device_house_id(device) != house_id:
            continue
        if (device.get("metrics") or {}).get("temperature", {}).get("value") is None:
            continue
        device_id = str(device.get("id") or "").strip()
        if device_id:
            labels[device_id] = device.get("name") or device_id
    return jsonify({
        "ok": True,
        **history_service.device_graph_data(period=period, device_labels=labels),
        "now": now().isoformat(timespec="seconds")
    })


@app.route("/api/winter/current")
def api_winter_current():
    if winter_energy_service is None:
        return jsonify({"ok": False, "error": "Winter energy service unavailable"}), 503
    try:
        return jsonify({"ok": True, **winter_energy_service.current()})
    except Exception as exc:
        log(f"Winter current API failed: {exc}")
        return jsonify({"ok": False, "error": "Winter data temporarily unavailable"}), 503


@app.route("/api/winter/history")
def api_winter_history():
    if winter_energy_service is None:
        return jsonify({"ok": False, "error": "Winter energy service unavailable", "samples": []}), 503
    try:
        samples = winter_energy_service.history(
            hours=request.args.get("hours", 24),
            limit=request.args.get("limit", 1000),
        )
        return jsonify({"ok": True, "samples": samples, "count": len(samples)})
    except Exception as exc:
        log(f"Winter history API failed: {exc}")
        return jsonify({"ok": False, "error": "Winter history temporarily unavailable", "samples": []}), 503


@app.route("/api/winter/status")
def api_winter_status():
    if winter_energy_service is None:
        return jsonify({"ok": False, "enabled": False, "error": "Winter energy service unavailable"}), 503
    try:
        return jsonify({"ok": True, **winter_energy_service.status()})
    except Exception as exc:
        log(f"Winter status API failed: {exc}")
        return jsonify({"ok": False, "error": "Winter status temporarily unavailable"}), 503


@app.route("/api/events")
def api_events():
    with _lock:
        events = list(reversed(load_event_log()))
    return jsonify({
        "ok": True,
        "events": events,
        "last_successful_refresh": _runtime.get("last_poll"),
        "now": now().isoformat(timespec="seconds")
    })


@app.route("/api/intesis/status")
def api_intesis_status():
    if not intesis.is_configured():
        start_intesis_driver()
    return jsonify([
        intesis_device_api_status(device)
        for device in intesis.get_all_devices()
    ])


def intesis_capability_values(device, key):
    values = device.get(key)
    if not isinstance(values, (list, tuple, set)):
        return None
    return [str(value).lower() for value in values if value is not None]


def intesis_device_api_status(device):
    status = dict(device) if isinstance(device, dict) else {}
    for key in (
        "supported_modes",
        "supported_fan_speeds",
        "supported_vertical_vanes",
        "supported_horizontal_vanes",
    ):
        status[key] = intesis_capability_values(status, key)
    if not isinstance(status.get("supports_vertical_vane"), bool):
        status["supports_vertical_vane"] = status.get("vertical_vane") is not None
    if not isinstance(status.get("supports_horizontal_vane"), bool):
        status["supports_horizontal_vane"] = status.get("horizontal_vane") is not None
    return status


def intesis_command_payload(value_name):
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return None, None, (jsonify({
            "ok": False,
            "error": "Request body must be a JSON object"
        }), 400)

    device_id = str(data.get("device_id") or "").strip()
    if not device_id:
        return None, None, (jsonify({
            "ok": False,
            "error": "device_id is required"
        }), 400)
    if value_name not in data:
        return None, None, (jsonify({
            "ok": False,
            "error": f"{value_name} is required"
        }), 400)

    if not intesis.is_configured():
        start_intesis_driver()
    if intesis.get_device(device_id) is None:
        return None, None, (jsonify({
            "ok": False,
            "error": f"Unknown Intesis device: {device_id}"
        }), 404)
    return device_id, data.get(value_name), None


def intesis_command_response(device_id, command_name, command_succeeded):
    if not command_succeeded:
        log(f"Intesis API command failed: {command_name} | device={device_id}")
        return jsonify({
            "ok": False,
            "error": f"Intesis command failed: {command_name}",
            "device_id": device_id
        }), 502

    device = intesis.get_device(device_id)
    log(
        f"Intesis API command completed: {command_name} | device={device_id}",
        level="DEBUG"
    )
    return jsonify({
        "ok": True,
        "device": intesis_device_api_status(device)
    })


@app.route("/api/intesis/power", methods=["POST"])
def api_intesis_power():
    device_id, power, error_response = intesis_command_payload("power")
    if error_response:
        return error_response
    if not isinstance(power, bool):
        return jsonify({"ok": False, "error": "power must be a boolean"}), 400
    command_succeeded = (
        intesis.power_on(device_id) if power else intesis.power_off(device_id)
    )
    return intesis_command_response(device_id, "power", command_succeeded)


@app.route("/api/intesis/mode", methods=["POST"])
def api_intesis_mode():
    device_id, mode, error_response = intesis_command_payload("mode")
    if error_response:
        return error_response
    mode = str(mode or "").strip().lower()
    if mode not in {"auto", "cool", "heat", "dry", "fan"}:
        return jsonify({"ok": False, "error": "Invalid mode"}), 400
    return intesis_command_response(
        device_id,
        "mode",
        intesis.set_mode(device_id, mode)
    )


@app.route("/api/intesis/temperature", methods=["POST"])
def api_intesis_temperature():
    device_id, temperature, error_response = intesis_command_payload("temperature")
    if error_response:
        return error_response
    try:
        if isinstance(temperature, bool):
            raise ValueError
        temperature = round(float(temperature), 1)
        if not math.isfinite(temperature):
            raise ValueError
    except (TypeError, ValueError):
        return jsonify({
            "ok": False,
            "error": "temperature must be a finite number"
        }), 400
    device = intesis.get_device(device_id) or {}
    minimum = device.get("minimum_target_temperature")
    maximum = device.get("maximum_target_temperature")
    try:
        minimum = float(minimum) if minimum is not None else None
    except (TypeError, ValueError):
        minimum = None
    try:
        maximum = float(maximum) if maximum is not None else None
    except (TypeError, ValueError):
        maximum = None
    if minimum is not None and temperature < minimum:
        return jsonify({
            "ok": False,
            "error": f"temperature must be at least {minimum}"
        }), 400
    if maximum is not None and temperature > maximum:
        return jsonify({
            "ok": False,
            "error": f"temperature must be at most {maximum}"
        }), 400
    return intesis_command_response(
        device_id,
        "temperature",
        intesis.set_temperature(device_id, temperature)
    )


@app.route("/api/intesis/fan", methods=["POST"])
def api_intesis_fan():
    device_id, speed, error_response = intesis_command_payload("speed")
    if error_response:
        return error_response
    speed = str(speed or "").strip().lower()
    if speed not in {"auto", "quiet", "low", "medium", "high"}:
        return jsonify({"ok": False, "error": "Invalid fan speed"}), 400
    return intesis_command_response(
        device_id,
        "fan",
        intesis.set_fan_speed(device_id, speed)
    )


def intesis_vane_response(value_name, command_name, command):
    device_id, position, error_response = intesis_command_payload(value_name)
    if error_response:
        return error_response
    position = str(position or "").strip().lower()
    if not position:
        return jsonify({"ok": False, "error": "position cannot be empty"}), 400
    return intesis_command_response(
        device_id,
        command_name,
        command(device_id, position)
    )


@app.route("/api/intesis/vertical_vane", methods=["POST"])
def api_intesis_vertical_vane():
    return intesis_vane_response(
        "position",
        "vertical_vane",
        intesis.set_vertical_vane
    )


@app.route("/api/intesis/horizontal_vane", methods=["POST"])
def api_intesis_horizontal_vane():
    return intesis_vane_response(
        "position",
        "horizontal_vane",
        intesis.set_horizontal_vane
    )


@app.route("/statistics")
@app.route("/report")
def statistics_page():
    cfg = load_config()
    lang = selected_language(cfg, persist_browser=True)
    return render_template("statistics.html", language=lang, back_href="/cesclans")


@app.route("/event-log")
def event_log_page():
    cfg = load_config()
    lang = selected_language(cfg, persist_browser=True)
    return render_template("event_log.html", language=lang, back_href="/cesclans")


@app.route("/api/solar_heating/config", methods=["POST"])
def api_solar_heating_config():
    data = request.get_json(force=True)
    cfg = load_config()
    solar = solar_heating_config(cfg)
    was_enabled = bool(solar.get("enabled"))

    if "enabled" in data:
        solar["enabled"] = bool(data["enabled"])
    if "water_temperature_threshold" in data:
        solar["water_temperature_threshold"] = float(data["water_temperature_threshold"])
    if "start_time" in data:
        if not valid_hhmm(data["start_time"]):
            return jsonify({"ok": False, "error": "Invalid start_time"}), 400
        solar["start_time"] = data["start_time"]
    if "forced_stop_time" in data:
        if not valid_hhmm(data["forced_stop_time"]):
            return jsonify({"ok": False, "error": "Invalid forced_stop_time"}), 400
        solar["forced_stop_time"] = data["forced_stop_time"]
    if "temperature_check_interval_seconds" in data:
        try:
            interval_seconds = int(float(data["temperature_check_interval_seconds"]))
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "Invalid temperature_check_interval_seconds"}), 400
        if interval_seconds < 300 or interval_seconds > 3600 or interval_seconds % 300 != 0:
            return jsonify({"ok": False, "error": "Invalid temperature_check_interval_seconds"}), 400
        solar["temperature_check_interval_seconds"] = interval_seconds
    if "early_completion_temperature" in data:
        try:
            solar["early_completion_temperature"] = float(data["early_completion_temperature"])
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "Invalid early_completion_temperature"}), 400
    if "early_completion_confirmation_minutes" in data:
        try:
            confirmation_minutes = int(float(data["early_completion_confirmation_minutes"]))
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "Invalid early_completion_confirmation_minutes"}), 400
        if confirmation_minutes < 30 or confirmation_minutes > 240:
            return jsonify({"ok": False, "error": "Invalid early_completion_confirmation_minutes"}), 400
        solar["early_completion_confirmation_minutes"] = confirmation_minutes
    if "minimum_pv_production_watts" in data:
        try:
            minimum_pv_watts = int(float(data["minimum_pv_production_watts"]))
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "Invalid minimum_pv_production_watts"}), 400
        if minimum_pv_watts < 0:
            return jsonify({"ok": False, "error": "Invalid minimum_pv_production_watts"}), 400
        solar["minimum_pv_production_watts"] = minimum_pv_watts
    if "pv_running_check_interval_minutes" in data:
        try:
            pv_interval_minutes = int(float(data["pv_running_check_interval_minutes"]))
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "Invalid pv_running_check_interval_minutes"}), 400
        if pv_interval_minutes < 1:
            return jsonify({"ok": False, "error": "Invalid pv_running_check_interval_minutes"}), 400
        solar["pv_running_check_interval_minutes"] = pv_interval_minutes
    if "pv_confirmation_delay_minutes" in data:
        try:
            pv_confirmation_minutes = int(float(data["pv_confirmation_delay_minutes"]))
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "Invalid pv_confirmation_delay_minutes"}), 400
        if pv_confirmation_minutes < 1:
            return jsonify({"ok": False, "error": "Invalid pv_confirmation_delay_minutes"}), 400
        solar["pv_confirmation_delay_minutes"] = pv_confirmation_minutes

    save_config(cfg)
    is_enabled = bool(solar.get("enabled"))
    if is_enabled:
        with _lock:
            _runtime["solar_heating"]["last_temperature_check_at"] = None
            reset_solar_pv_confirmation(_runtime["solar_heating"])
            reset_early_completion_timer(_runtime["solar_heating"])
            reconcile_solar_temperature_schedule(cfg, force_due=True)
            save_state()
        solar_heating_tick()
    else:
        with _lock:
            set_solar_next_temperature_check(_runtime["solar_heating"], None)
            reset_solar_pv_confirmation(_runtime["solar_heating"])
            reset_early_completion_timer(_runtime["solar_heating"])
            save_state()
    if is_enabled != was_enabled:
        record_solar_event("Solar Heating enabled" if is_enabled else "Solar Heating disabled")
    else:
        record_event("Configuration", "Solar Heating", "Configuration changed", "Manual", f"Solar Heating configuration updated: {solar}")
    return jsonify({"ok": True, "solar_heating": solar_heating_snapshot(cfg)})


@app.route("/api/pump/config", methods=["POST"])
def api_pump_config():
    data = request.get_json(force=True)
    cfg = load_config()
    p = cfg["pump"]
    if "start_time" in data:
        p["start_time"] = data["start_time"]
    if "duration_hours" in data:
        p["duration_hours"] = float(data["duration_hours"])
    if "mode" in data:
        p["mode"] = data["mode"]
    if "enabled" in data:
        p["enabled"] = bool(data["enabled"])
    save_config(cfg)
    record_event("Configuration", "Pump", "Configuration changed", "Manual", f"Pump configuration updated: {p}")
    log(f"CONFIG pompa aggiornata: {p}")
    return jsonify({"ok": True, "pump": p})


@app.route("/api/pump/reset_auto", methods=["POST"])
def api_reset_auto():
    with _lock:
        _runtime["scheduler"]["manual_run_until"] = None
        _runtime["scheduler"]["auto_suspended_date"] = None
        save_state()
    log("POMPA automatico riattivato manualmente")
    record_event("Configuration", "Pump", "Configuration changed", "Manual", "Automatic pump schedule reactivated for today")
    return jsonify({"ok": True})


@app.route("/api/notifications/telegram/config", methods=["POST"])
def api_telegram_config():
    data = request.get_json(force=True)
    cfg = load_config()
    ensure_config_defaults(cfg)
    telegram = cfg["notifications"]["telegram"]
    if "enabled" in data:
        telegram["enabled"] = bool(data["enabled"])
    if "bot_token" in data and str(data.get("bot_token") or "").strip():
        telegram["bot_token"] = str(data.get("bot_token") or "").strip()
    if "chat_id" in data:
        telegram["chat_id"] = str(data.get("chat_id") or "").strip()
    save_config(cfg)
    record_event("Configuration", "Telegram", "Configuration changed", "Manual", "Telegram notification configuration updated")
    return jsonify({"ok": True, "telegram": public_config(cfg)["notifications"]["telegram"]})


@app.route("/api/notifications/telegram/test", methods=["POST"])
def api_telegram_test():
    cfg = load_config()
    result = notification_service.test_connection(cfg, telegram_test_message(cfg))
    if result.get("ok"):
        return jsonify({"ok": True, "message": "Telegram connection successful."})
    return jsonify({"ok": False, "error": result.get("error") or "Telegram connection failed."}), 400


if __name__ == "__main__":
    start_temperature_service()
    start_weather_service()
    start_netatmo_service()
    start_history_service()
    start_intesis_driver()
    start_winter_energy_service()
    threading.Thread(target=background_loop, daemon=True).start()
    threading.Thread(target=goodwe_background_loop, daemon=True).start()
    cfg = load_config()
    app.run(host=cfg["app"].get("host", "0.0.0.0"), port=int(cfg["app"].get("port", 5000)), debug=False)
