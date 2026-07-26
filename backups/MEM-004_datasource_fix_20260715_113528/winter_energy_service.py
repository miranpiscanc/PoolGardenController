import os
import sqlite3
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path


DEFAULT_WINTER_CONFIG = {
    "enabled": True,
    "mode": "learning",
    "target_temp": 21.0,
    "automation": False,
    "simulation": True,
    "sample_seconds": 300,
}


class WinterEnergyService:
    """Read-only learning collector. This class never issues HVAC commands."""

    HISTORY_COLUMNS = (
        "outdoor_temp", "master_bedroom_temp", "kids_room_temp",
        "indoor_average", "coldest_room_temp", "indoor_outdoor_delta",
        "pv_production", "house_consumption", "battery_soc",
        "battery_power", "grid_power",
    )

    def __init__(self, db_path, config, snapshot_provider, log_callback=None):
        self.db_path = Path(db_path)
        self.config = self._effective_config(config)
        self.snapshot_provider = snapshot_provider
        self.log_callback = log_callback
        self.sample_seconds = max(30, int(self.config.get("sample_seconds", 300)))
        self.enabled = bool(self.config.get("enabled", True))
        self._stop = threading.Event()
        self._thread = None
        self._lock = threading.RLock()
        self._last_sample = None
        self._last_error = None
        self._init_db()

    @staticmethod
    def _effective_config(config):
        result = dict(DEFAULT_WINTER_CONFIG)
        result.update(config or {})
        env_types = {
            "WINTER_ENABLED": ("enabled", "bool"),
            "WINTER_MODE": ("mode", "str"),
            "WINTER_TARGET_TEMP": ("target_temp", "float"),
            "WINTER_AUTOMATION": ("automation", "bool"),
            "WINTER_SIMULATION": ("simulation", "bool"),
        }
        for env_name, (key, value_type) in env_types.items():
            raw = os.environ.get(env_name)
            if raw is None:
                continue
            try:
                if value_type == "bool":
                    result[key] = raw.strip().lower() in {"1", "true", "yes", "on"}
                elif value_type == "float":
                    result[key] = float(raw)
                else:
                    result[key] = raw.strip()
            except (TypeError, ValueError):
                pass
        # MEM-004 is learning-only regardless of an unsafe configuration value.
        result["automation"] = False
        return result

    def start(self):
        if not self.enabled:
            self._log("WinterEnergyService disabled")
            return
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="winter-energy")
        self._thread.start()
        self._log("WinterEnergyService started in learning mode")

    def stop(self):
        self._stop.set()

    def _run(self):
        while not self._stop.is_set():
            started = time.monotonic()
            try:
                self.sample_once()
            except Exception as exc:
                with self._lock:
                    self._last_error = str(exc)
                self._log(f"Winter energy sampling failed: {exc}")
            elapsed = time.monotonic() - started
            self._stop.wait(max(0, self.sample_seconds - elapsed))

    def sample_once(self):
        current = self.current()
        timestamp = datetime.now().isoformat(timespec="seconds")
        values = [current["temperatures"].get(key) for key in (
            "outdoor", "master_bedroom", "kids_room", "indoor_average",
            "coldest_room_temp", "indoor_outdoor_delta",
        )]
        values.extend(current["energy"].get(key) for key in (
            "pv_production", "house_consumption", "battery_soc", "battery_power", "grid_power",
        ))
        placeholders = ", ".join("?" for _ in range(1 + len(values)))
        with self._lock, self._connect() as conn:
            conn.execute(
                f"INSERT INTO winter_energy_history (timestamp, {', '.join(self.HISTORY_COLUMNS)}) VALUES ({placeholders})",
                [timestamp, *values],
            )
            self._last_sample = timestamp
            self._last_error = None
        return current

    def current(self):
        try:
            sources = self.snapshot_provider() or {}
        except Exception as exc:
            self._log(f"Winter snapshot provider unavailable: {exc}")
            sources = {}
        temperatures = self._temperature_snapshot(sources.get("netatmo") or {})
        energy = self._energy_snapshot(sources.get("goodwe") or {})
        hvac = self._hvac_snapshot(sources.get("intesis") or [])
        analysis = self._thermal_analysis(temperatures)
        return {
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "temperatures": temperatures,
            "energy": energy,
            "hvac": hvac,
            "thermal_analysis": analysis,
            "decision": self._decision(temperatures, energy, analysis),
        }

    def status(self):
        current = self.current()
        with self._lock:
            last_sample, last_error = self._last_sample, self._last_error
        source_status = {
            "netatmo": any(current["temperatures"].get(k) is not None for k in ("outdoor", "master_bedroom", "kids_room")),
            "goodwe": any(current["energy"].get(k) is not None for k in ("pv_production", "house_consumption", "battery_soc")),
            "intesis": bool(current["hvac"]),
        }
        return {
            "enabled": self.enabled,
            "mode": "learning",
            "automation": False,
            "simulation": bool(self.config.get("simulation", True)),
            "sample_seconds": self.sample_seconds,
            "last_sample": last_sample or self._latest_timestamp(),
            "last_error": last_error,
            "collector_running": bool(self._thread and self._thread.is_alive()),
            "sources": source_status,
            "banner": "SIMULATION ONLY -- NO HVAC COMMANDS ARE SENT.",
        }

    def history(self, hours=24, limit=1000):
        try:
            hours = max(1, min(24 * 365, int(hours)))
            limit = max(1, min(5000, int(limit)))
        except (TypeError, ValueError):
            hours, limit = 24, 1000
        start = (datetime.now() - timedelta(hours=hours)).isoformat(timespec="seconds")
        columns = ", ".join(("timestamp", *self.HISTORY_COLUMNS))
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                f"SELECT {columns} FROM winter_energy_history WHERE timestamp >= ? ORDER BY timestamp DESC LIMIT ?",
                (start, limit),
            ).fetchall()
        return [dict(row) for row in reversed(rows)]

    def _temperature_snapshot(self, netatmo):
        devices = list((netatmo.get("stations") or {}).values()) + list((netatmo.get("modules") or {}).values())
        selected = {"outdoor": None, "master_bedroom": None, "kids_room": None}
        names = {"outdoor": None, "master_bedroom": None, "kids_room": None}
        patterns = {
            "outdoor": ("outdoor", "outside", "esterno", "esterna"),
            "master_bedroom": ("master", "camera matrimoniale", "bedroom"),
            "kids_room": ("kids", "kid", "children", "bambini", "ragazzi", "cameretta"),
        }
        for device in devices:
            name = str(device.get("name") or "").strip()
            normalized = name.lower()
            metric = (device.get("metrics") or {}).get("temperature") or {}
            value = self._number(metric.get("value")) if metric.get("online", metric.get("value") is not None) else None
            if value is None:
                continue
            for key, tokens in patterns.items():
                if selected[key] is None and any(token in normalized for token in tokens):
                    selected[key], names[key] = value, name
                    break
        indoor = [value for key, value in selected.items() if key != "outdoor" and value is not None]
        average = round(sum(indoor) / len(indoor), 1) if indoor else None
        coldest = min(indoor) if indoor else None
        delta = round(average - selected["outdoor"], 1) if average is not None and selected["outdoor"] is not None else None
        trends = self._temperature_trends(selected)
        return {
            **selected,
            "indoor_average": average,
            "coldest_room": names["master_bedroom"] if coldest == selected["master_bedroom"] else names["kids_room"] if coldest is not None else None,
            "coldest_room_temp": coldest,
            "indoor_outdoor_delta": delta,
            "device_names": names,
            "trends_c_per_hour": trends,
        }

    def _temperature_trends(self, current):
        cutoff = (datetime.now() - timedelta(hours=1)).isoformat(timespec="seconds")
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT timestamp, outdoor_temp, master_bedroom_temp, kids_room_temp FROM winter_energy_history WHERE timestamp >= ? ORDER BY timestamp ASC LIMIT 1",
                (cutoff,),
            ).fetchone()
        if not row:
            return {key: None for key in current}
        try:
            hours = max((datetime.now() - datetime.fromisoformat(row["timestamp"])).total_seconds() / 3600, 1 / 60)
        except (TypeError, ValueError):
            return {key: None for key in current}
        columns = {"outdoor": "outdoor_temp", "master_bedroom": "master_bedroom_temp", "kids_room": "kids_room_temp"}
        return {
            key: round((value - row[column]) / hours, 2) if value is not None and row[column] is not None else None
            for key, (column, value) in ((key, (columns[key], current[key])) for key in current)
        }

    def _energy_snapshot(self, goodwe):
        return {
            "online": bool(goodwe),
            "pv_production": self._number(goodwe.get("pv_production")),
            "house_consumption": self._number(goodwe.get("house_consumption")),
            "battery_soc": self._number(goodwe.get("battery_soc")),
            "battery_power": self._number(goodwe.get("battery_power")),
            "grid_power": self._number(goodwe.get("grid_power")),
        }

    def _hvac_snapshot(self, devices):
        result = []
        for device in devices if isinstance(devices, list) else []:
            if not isinstance(device, dict):
                continue
            result.append({
                "id": device.get("id"),
                "name": device.get("name"),
                "online": not bool(device.get("error")),
                "power": device.get("power"),
                "mode": device.get("mode"),
                "temperature": device.get("room_temperature"),
                "target_temperature": device.get("target_temperature"),
                "fan_speed": device.get("fan_speed"),
            })
        return result

    def _thermal_analysis(self, temperatures):
        trends = temperatures.get("trends_c_per_hour") or {}
        indoor_trends = [trends.get(key) for key in ("master_bedroom", "kids_room") if trends.get(key) is not None]
        avg_trend = round(sum(indoor_trends) / len(indoor_trends), 2) if indoor_trends else None
        delta = temperatures.get("indoor_outdoor_delta")
        inertia = None
        if avg_trend is not None and delta not in (None, 0):
            inertia = round(abs(delta / avg_trend), 1) if abs(avg_trend) >= 0.02 else 99.0
        return {
            "indoor_trend_c_per_hour": avg_trend,
            "estimated_thermal_inertia_hours": inertia,
            "learning_samples": self._sample_count(),
            "quality": "learning" if self._sample_count() < 288 else "preliminary",
        }

    def _decision(self, temperatures, energy, analysis):
        target = self._number(self.config.get("target_temp")) or 21.0
        indoor = temperatures.get("indoor_average")
        pv = energy.get("pv_production")
        load = energy.get("house_consumption")
        soc = energy.get("battery_soc")
        surplus = pv - load if pv is not None and load is not None else None
        if indoor is None:
            recommendation, reason = "WAIT_FOR_DATA", "Indoor temperature data unavailable."
        elif indoor < target - 0.5 and surplus is not None and surplus > 1200 and (soc is None or soc >= 50):
            recommendation, reason = "SIMULATE_PREHEAT", "Comfort demand and photovoltaic surplus are available."
        elif indoor < target - 0.5:
            recommendation, reason = "DEFER_HEATING", "Comfort demand exists, but energy conditions do not favor self-consumption."
        elif indoor > target + 0.5:
            recommendation, reason = "NO_HEATING", "Indoor temperature is above the configured target."
        else:
            recommendation, reason = "HOLD", "Indoor temperature is within the target comfort band."
        return {
            "recommendation": recommendation,
            "reason": reason,
            "target_temperature": target,
            "available_surplus": surplus,
            "simulation_only": True,
            "hvac_command_sent": False,
        }

    def _init_db(self):
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        columns = ",\n".join(f"{column} REAL NULL" for column in self.HISTORY_COLUMNS)
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(f"""
                CREATE TABLE IF NOT EXISTS winter_energy_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    {columns},
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_winter_energy_timestamp ON winter_energy_history (timestamp)")

    def _sample_count(self):
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT COUNT(*) AS count FROM winter_energy_history").fetchone()
        return int(row["count"] if row else 0)

    def _latest_timestamp(self):
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT MAX(timestamp) AS timestamp FROM winter_energy_history").fetchone()
        return row["timestamp"] if row else None

    def _connect(self):
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    def _log(self, message):
        if self.log_callback:
            self.log_callback(message)

    @staticmethod
    def _number(value):
        if value is None or isinstance(value, bool):
            return None
        try:
            return round(float(value), 2)
        except (TypeError, ValueError, OverflowError):
            return None
