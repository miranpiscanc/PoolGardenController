import json
import atexit
import shutil
import threading
import time
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import Dict, Any

from flask import Flask, jsonify, render_template, request

from relay_client import HHCRelayClient
from temperature_service import DEFAULT_TEMPERATURE_SENSORS, TemperatureService

BASE = Path(__file__).resolve().parent
DATA_DIR = BASE / "data"
VERSION_PATH = BASE / "VERSION"
CONFIG_DEFAULTS_PATH = BASE / "config.defaults.json"
CONFIG_PATH = DATA_DIR / "config.json"
LANG_DIR = BASE / "lang"
STATE_PATH = DATA_DIR / "state.json"
HEATER_STATS_PATH = DATA_DIR / "heater_statistics.json"
DEVICE_STATS_PATH = DATA_DIR / "device_statistics.json"
EVENT_LOG_PATH = DATA_DIR / "event_log.json"
LOG_DIR = BASE / "logs"
LOG_DIR.mkdir(exist_ok=True)
DATA_DIR.mkdir(exist_ok=True)

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
        "safe_stop_completed_date": None
    },
    "heater_safe_stop": {
        "state": "IDLE",
        "running": False,
        "phase": "Inattivo",
        "started_at": None,
        "cooldown_until": None,
        "completed_at": None,
        "error": None
    }
}
_safe_stop_thread = None
_safe_stop_reset_thread = None
_solar_safe_stop_monitor_thread = None
temperature_service = None
SAFE_STOP_COMPLETED_VISIBLE_SECONDS = 3
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


def now():
    return datetime.now()


def load_config():
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    changed = ensure_config_defaults(cfg)
    if changed:
        save_config(cfg)
    return cfg


def app_version(cfg=None):
    try:
        version = VERSION_PATH.read_text(encoding="utf-8").strip()
        return version or "Unknown Version"
    except Exception:
        return "Unknown Version"


def save_config(cfg):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)


def ensure_config_defaults(cfg):
    changed = False
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
        "forced_stop_time": "17:00"
    }
    for key, value in defaults.items():
        if key not in solar:
            solar[key] = value
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
        try:
            stats = normalize_statistics(json.loads(DEVICE_STATS_PATH.read_text(encoding="utf-8")))
        except Exception:
            stats = {}
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


def save_device_statistics(stats):
    data = {
        "updated_at": now().isoformat(timespec="seconds"),
        "relays": stats
    }
    DEVICE_STATS_PATH.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


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
        save_device_statistics(stats)
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
        save_device_statistics(stats)
        return relay_stats


def log(msg: str):
    line = f"{now().strftime('%Y-%m-%d %H:%M:%S')} | {msg}\n"
    (LOG_DIR / f"{now().strftime('%Y-%m-%d')}.log").open("a", encoding="utf-8").write(line)
    print(line, end="")


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


def read_one(cfg, device_id, relay_number):
    c = client_for(cfg, device_id)
    res = c.read_relay(relay_number)
    active = parse_active(res.response, relay_number) if res.ok else None
    return {
        "ok": res.ok,
        "active": active,
        "response": res.response,
        "elapsed_ms": res.elapsed_ms,
        "error": res.error,
        "updated_at": now().isoformat(timespec="seconds")
    }


def set_one_raw(cfg, device_id, relay_number, active: bool, source="manual"):
    c = client_for(cfg, device_id)
    res = c.set_relay(relay_number, active)
    # Dopo ogni comando leggiamo lo stato reale, come relay_demo.
    status = read_one(cfg, device_id, relay_number)
    status["command_ok"] = res.ok
    status["command_error"] = res.error
    action = "ON" if active else "OFF"
    with _lock:
        _runtime["last_command"] = {
            "device": device_id,
            "relay": relay_number,
            "action": action,
            "source": source,
            "ok": res.ok and status.get("ok"),
            "response": res.response,
            "read_response": status.get("response"),
            "time": now().isoformat(timespec="seconds")
        }
    log(f"{source.upper()} {device_id}.{relay_number} {action} | cmd_ok={res.ok} cmd_resp={res.response!r} | stato={status.get('active')} resp={status.get('response')!r} err={res.error or status.get('error','')}")
    if not status.get("ok"):
        record_event(
            relay_label(cfg, device_id, relay_number),
            f"Relay {relay_number}",
            "Communication error",
            source_label(source),
            status.get("error") or res.error or "Relay read failed after command"
        )
    if status.get("ok") and status.get("active") in (True, False):
        sync_device_statistics(cfg, device_id, relay_number, status.get("active"), source=source)
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
        state = dict(_runtime["heater_safe_stop"])
    remaining = 0
    if state.get("running") and state.get("cooldown_until"):
        try:
            cooldown_until = datetime.fromisoformat(state["cooldown_until"])
            remaining = max(0, int((cooldown_until - now()).total_seconds()))
        except Exception:
            remaining = 0
    state["remaining_seconds"] = remaining
    return state


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


def reset_safe_stop_after_completed(completed_at):
    time.sleep(SAFE_STOP_COMPLETED_VISIBLE_SECONDS)
    with _lock:
        state = _runtime["heater_safe_stop"]
        if (
            state.get("state") == "COMPLETED"
            and state.get("completed_at") == completed_at
            and not state.get("running")
        ):
            state.update({
                "state": "IDLE",
                "phase": "Inattivo",
                "started_at": None,
                "cooldown_until": None,
                "completed_at": None,
                "error": None
            })


def complete_safe_stop(phase="Arresto sicuro completato"):
    global _safe_stop_reset_thread
    completed_at = now().isoformat(timespec="microseconds")
    set_safe_stop_state(
        state="COMPLETED",
        running=False,
        phase=phase,
        cooldown_until=None,
        completed_at=completed_at,
        error=None
    )
    record_event("Heater", "Relay 1", "Safe Stop Completed", "Safe Stop", phase)
    log(f"HEATER_SAFE_STOP COMPLETED | {phase}")
    _safe_stop_reset_thread = threading.Thread(
        target=reset_safe_stop_after_completed,
        args=(completed_at,),
        daemon=True
    )
    _safe_stop_reset_thread.start()


def heater_safe_stop_worker(source="manual"):
    try:
        cfg = load_config()
        heater_dev, heater_no = heater_relay(cfg)
        pump_dev, pump_no = pump_relay(cfg)

        set_safe_stop_state(state="SAFE_STOP_START", phase="Verifica stato riscaldatore", error=None)
        heater_status = read_one(cfg, heater_dev, heater_no)
        if not heater_status.get("ok"):
            fail_safe_stop(heater_status.get("error") or "Riscaldatore non raggiungibile")
            return
        if heater_status.get("active") is False:
            complete_safe_stop("Riscaldatore già spento")
            poll_all()
            return
        if heater_status.get("active") is not True:
            fail_safe_stop("Stato riscaldatore non valido")
            return

        set_safe_stop_state(phase="Verifica stato pompa")
        pump_status = read_one(cfg, pump_dev, pump_no)
        if not pump_status.get("ok"):
            fail_safe_stop(pump_status.get("error") or "Pompa non raggiungibile")
            return

        set_safe_stop_state(phase="Spegnimento riscaldatore")
        heater_off = set_one_raw(cfg, heater_dev, heater_no, False, source=source)
        if not relay_set_confirmed(heater_off, False):
            fail_safe_stop(heater_off.get("command_error") or heater_off.get("error") or "Spegnimento riscaldatore non confermato")
            return

        if pump_status.get("active") is False:
            complete_safe_stop("Riscaldatore spento, pompa già ferma")
            poll_all()
            return
        if pump_status.get("active") is not True:
            fail_safe_stop("Stato pompa non valido")
            return

        cooldown_until = now() + timedelta(seconds=60)
        set_safe_stop_state(
            state="COOLDOWN_RUNNING",
            phase="Raffreddamento in corso",
            cooldown_until=cooldown_until.isoformat(timespec="seconds")
        )
        while now() < cooldown_until:
            time.sleep(min(1, max(0, (cooldown_until - now()).total_seconds())))

        set_safe_stop_state(state="STOP_PUMP", phase="Arresto pompa")
        pump_off = set_one_raw(cfg, pump_dev, pump_no, False, source=source)
        if not relay_set_confirmed(pump_off, False):
            fail_safe_stop(pump_off.get("command_error") or pump_off.get("error") or "Arresto pompa non confermato")
            return

        complete_safe_stop()
        poll_all()
    except Exception as exc:
        fail_safe_stop(str(exc))


def start_heater_safe_stop(source="manual"):
    global _safe_stop_thread
    with _lock:
        if _runtime["heater_safe_stop"].get("running"):
            log(f"HEATER_SAFE_STOP IGNORED | already running | source={source}")
            return False, "Safe Stop already running"
        _runtime["heater_safe_stop"].update({
            "state": "SAFE_STOP_START",
            "running": True,
            "phase": "Avvio arresto sicuro",
            "started_at": now().isoformat(timespec="seconds"),
            "cooldown_until": None,
            "completed_at": None,
            "error": None
        })
        _safe_stop_thread = threading.Thread(
            target=heater_safe_stop_worker,
            kwargs={"source": source},
            daemon=True
        )
        _safe_stop_thread.start()
    log(f"HEATER_SAFE_STOP START | source={source}")
    record_event("Heater", "Relay 1", "Safe Stop Started", source_label(source), "Protection sequence")
    return True, "Safe Stop started"


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
        started, message = start_heater_safe_stop(source=f"{source}_safe_stop")
        return {
            "ok": True,
            "active": _runtime.get("relays", {}).get(f"{heater_dev}:{heater_no}", {}).get("active"),
            "response": message,
            "message": message,
            "ignored": not started,
            "elapsed_ms": 0,
            "error": "",
            "updated_at": now().isoformat(timespec="seconds")
        }
    if not active and is_pump:
        heater_status = read_one(cfg, heater_dev, heater_no)
        if not heater_status.get("ok"):
            return {
                "ok": False,
                "active": pump_status_from_runtime(cfg),
                "response": "",
                "elapsed_ms": heater_status.get("elapsed_ms"),
                "error": heater_status.get("error") or "Impossibile verificare il riscaldatore",
                "updated_at": now().isoformat(timespec="seconds")
            }
        if heater_status.get("active") is True:
            started, message = start_heater_safe_stop(source=f"{source}_interlock")
            return {
                "ok": True,
                "active": pump_status_from_runtime(cfg),
                "response": message,
                "message": message,
                "ignored": not started,
                "elapsed_ms": heater_status.get("elapsed_ms"),
                "error": "",
                "updated_at": now().isoformat(timespec="seconds")
            }
    return set_one_raw(cfg, device_id, relay_number, active, source=source)


def poll_all():
    cfg = load_config()
    new_devices = {}
    new_relays = {}
    for dev_id, dev in cfg["devices"].items():
        dev_ok = True
        last_error = ""
        max_elapsed = 0
        for relay_no in dev["relays"].keys():
            st = read_one(cfg, dev_id, relay_no)
            new_relays[f"{dev_id}:{relay_no}"] = st
            if st.get("ok") and st.get("active") in (True, False):
                sync_device_statistics(cfg, dev_id, relay_no, st.get("active"), source="poll")
            elif not st.get("ok"):
                record_event(
                    relay_label(cfg, dev_id, relay_no),
                    f"Relay {relay_no}",
                    "Communication error",
                    "Polling",
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


def solar_heating_snapshot(cfg=None):
    if cfg is None:
        cfg = load_config()
    solar = dict(solar_heating_config(cfg))
    start, stop = solar_heating_window(cfg)
    with _lock:
        runtime = dict(_runtime["solar_heating"])
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
        "safe_stop_time": (stop - timedelta(minutes=2)).strftime("%H:%M"),
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
        return {"key": "solar.message.completedToday", "vars": {}, "message": "Automation completed for today"}
    if runtime.get("safe_stop_started_date") == today:
        if safe_stop.get("running"):
            return {"key": "solar.message.safeStopRunning", "vars": {}, "message": "Safe Stop running..."}
        return {"key": "solar.message.completedToday", "vars": {}, "message": "Automation completed for today"}
    if runtime.get("auto_started_date") == today:
        if current < start + timedelta(minutes=5):
            return {
                "key": "solar.message.heatingStartedAt",
                "vars": {"time": start.strftime("%H:%M")},
                "message": f"Heating started automatically at {start.strftime('%H:%M')}"
            }
        return {"key": "solar.message.safeStopScheduled", "vars": {}, "message": "Safe Stop scheduled"}
    if runtime.get("last_start_date") == today:
        recent_event = latest_solar_heating_event(today)
        if recent_event and "Water already" in recent_event.get("event", ""):
            return {
                "key": "solar.message.temperatureOk",
                "vars": {},
                "message": "Temperature OK\n(Water already above configured threshold)"
            }
        return {"key": "solar.message.completedToday", "vars": {}, "message": "Automation completed for today"}
    if current < start:
        return {
            "key": "solar.message.waitingForStart",
            "vars": {"time": start.strftime("%H:%M")},
            "message": f"Waiting for start time ({start.strftime('%H:%M')})"
        }
    if current >= stop:
        return {"key": "solar.message.completedToday", "vars": {}, "message": "Automation completed for today"}
    return {"key": "solar.message.checkingWaterTemperature", "vars": {}, "message": "Checking water temperature..."}


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


def monitor_solar_safe_stop_completion(target_date):
    while True:
        safe_stop = safe_stop_snapshot()
        if not safe_stop.get("running"):
            if safe_stop.get("state") == "COMPLETED":
                with _lock:
                    state = _runtime["solar_heating"]
                    if state.get("safe_stop_completed_date") != target_date:
                        state["safe_stop_completed_date"] = target_date
                        save_state()
                        record_solar_event("Automatic Safe Stop completed")
            return
        time.sleep(1)


def start_solar_safe_stop_monitor(target_date):
    global _solar_safe_stop_monitor_thread
    if _solar_safe_stop_monitor_thread and _solar_safe_stop_monitor_thread.is_alive():
        return
    _solar_safe_stop_monitor_thread = threading.Thread(
        target=monitor_solar_safe_stop_completion,
        args=(target_date,),
        daemon=True
    )
    _solar_safe_stop_monitor_thread.start()


def solar_heating_tick():
    cfg = load_config()
    solar = solar_heating_config(cfg)
    today = date.today().isoformat()
    state = _runtime["solar_heating"]

    if not solar.get("enabled"):
        return

    current = now()
    start, stop = solar_heating_window(cfg)
    safe_stop_at = stop - timedelta(minutes=2)

    if state.get("auto_started_date") == today:
        safe_stop = safe_stop_snapshot()
        if (
            state.get("safe_stop_started_date") == today
            and state.get("safe_stop_completed_date") != today
            and safe_stop.get("state") == "COMPLETED"
            and not safe_stop.get("running")
        ):
            state["safe_stop_completed_date"] = today
            save_state()
            record_solar_event("Automatic Safe Stop completed")

        if current >= safe_stop_at and state.get("safe_stop_started_date") != today:
            state["safe_stop_started_date"] = today
            save_state()
            record_solar_event("Automatic Safe Stop started")
            start_heater_safe_stop(source="solar_heating_safe_stop")
            start_solar_safe_stop_monitor(today)
        return

    if current < start or current >= stop or state.get("last_start_date") == today:
        return

    state["last_start_date"] = today
    water_temperature = water_temperature_snapshot()
    if water_temperature is None:
        save_state()
        record_solar_event("Automatic heating skipped", "Water temperature unavailable")
        return

    threshold = float(solar.get("water_temperature_threshold", 29.0))
    if water_temperature > threshold:
        save_state()
        record_solar_event(
            f"Automatic heating skipped (Water already {water_temperature:.1f}°C)"
        )
        return

    pump_dev, pump_no = pump_relay(cfg)
    heater_dev, heater_no = heater_relay(cfg)
    pump_status = set_one_raw(cfg, pump_dev, pump_no, True, source="solar_heating")
    if not relay_set_confirmed(pump_status, True):
        save_state()
        record_solar_event("Automatic heating skipped", "Pump start was not confirmed")
        return
    heater_status = set_one_raw(cfg, heater_dev, heater_no, True, source="solar_heating")
    if not relay_set_confirmed(heater_status, True):
        save_state()
        record_solar_event("Automatic heating skipped", "Heater start was not confirmed")
        return
    state["auto_started_date"] = today
    state["safe_stop_started_date"] = None
    state["safe_stop_completed_date"] = None
    save_state()
    record_solar_event(f"Automatic heating started (Water {water_temperature:.1f}°C)")


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
    while True:
        try:
            poll_all()
            scheduler_tick()
            solar_heating_tick()
        except Exception as exc:
            log(f"ERRORE background: {exc}")
        time.sleep(load_config()["app"].get("poll_seconds", 4))


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


@app.route("/")
def index():
    cfg = load_config()
    lang = selected_language(cfg, persist_browser=True)
    return render_template("index.html", language=lang)


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
    app_cfg = cfg.get("app", {})
    version = app_version(cfg)
    port = int(app_cfg.get("port", 5000))
    start, end = pump_window(cfg)
    pump = cfg["pump"].copy()
    pump["computed_stop_time"] = end.strftime("%H:%M")
    pump["active"] = pump_status_from_runtime(cfg)
    pump["remaining_seconds"] = max(0, int((end - now()).total_seconds())) if pump["active"] else 0
    with _lock:
        return jsonify({
            "version": version,
            "port": port,
            "app": {"version": version, "port": port},
            "language": selected_language(cfg),
            "languages": supported_languages(),
            "app_meta": f"{version} · server su porta {port}",
            "config": cfg,
            "devices": _runtime["devices"],
            "relays": _runtime["relays"],
            "temperatures": temperature_service.snapshot() if temperature_service else {},
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
    poll_all()
    return jsonify({"ok": status.get("ok"), "status": status})


@app.route("/api/heater/safe_stop", methods=["POST"])
def api_heater_safe_stop():
    started, message = start_heater_safe_stop(source="manual_safe_stop")
    return jsonify({"ok": True, "message": message, "started": started, "safe_stop": safe_stop_snapshot()})


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


@app.route("/statistics")
def statistics_page():
    cfg = load_config()
    lang = selected_language(cfg, persist_browser=True)
    return render_template("statistics.html", language=lang)


@app.route("/event-log")
def event_log_page():
    cfg = load_config()
    lang = selected_language(cfg, persist_browser=True)
    return render_template("event_log.html", language=lang)


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

    save_config(cfg)
    is_enabled = bool(solar.get("enabled"))
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


if __name__ == "__main__":
    start_temperature_service()
    threading.Thread(target=background_loop, daemon=True).start()
    cfg = load_config()
    app.run(host=cfg["app"].get("host", "0.0.0.0"), port=int(cfg["app"].get("port", 5000)), debug=False)
