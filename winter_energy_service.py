import os
import json
import sqlite3
import threading
import time
import unicodedata
import uuid
from datetime import datetime, timedelta
from pathlib import Path


DEFAULT_WINTER_CONFIG = {
    "enabled": True,
    "mode": "learning",
    "target_temp": 18.0,
    "automation": False,
    "simulation": True,
    "sample_seconds": 300,
}


class WinterEnergyService:
    """Read-only learning collector. This class never issues HVAC commands."""

    CESCLANS_HVAC_DEVICES = {
        "224571441890950": "Cezklanc Salon",
        "224571441736783": "Cezklanc Soba",
    }
    SUPPORTED_HVAC_MODES = {"auto", "cool", "heat", "dry", "fan", "fan_only", "off"}
    HVAC_RATE_MIN_SAMPLES = 6
    HVAC_RATE_MIN_SECONDS = 20 * 60
    DECISION_LABELS = {
        "WAIT_FOR_DATA": "WAIT FOR DATA",
        "SIMULATE_PREHEAT": "WOULD PREHEAT",
        "DEFER_HEATING": "DEFER HEATING",
        "NO_HEATING": "NO HEATING NEEDED",
        "HOLD": "HOLD CURRENT CONDITION",
    }

    HISTORY_COLUMNS = (
        "outdoor_temp", "master_bedroom_temp", "kids_room_temp",
        "indoor_average", "coldest_room_temp", "indoor_outdoor_delta",
        "pv_production", "house_consumption", "battery_soc",
        "battery_power", "grid_power",
    )

    def __init__(self, db_path, config, snapshot_provider, log_callback=None, energy_context_provider=None):
        self.db_path = Path(db_path)
        self.config = self._effective_config(config)
        self.snapshot_provider = snapshot_provider
        self.energy_context_provider = energy_context_provider
        self.log_callback = log_callback
        self.sample_seconds = max(30, int(self.config.get("sample_seconds", 300)))
        self.enabled = bool(self.config.get("enabled", True))
        self._stop = threading.Event()
        self._thread = None
        self._lock = threading.RLock()
        self._last_sample = None
        self._last_error = None
        self._last_energy_context = None
        self._logged_netatmo_metrics = set()
        self._collector_session_id = uuid.uuid4().hex
        self._init_db()

    @staticmethod
    def _effective_config(config):
        result = dict(DEFAULT_WINTER_CONFIG)
        result.update(config or {})
        env_types = {
            "WINTER_ENABLED": ("enabled", "bool"),
            "WINTER_MODE": ("mode", "str"),
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

    def update_target_temperature(self, value):
        """Update simulated-policy configuration only; never samples or commands HVAC."""
        with self._lock:
            self.config["target_temp"] = float(value)

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
        values.append(current["energy_context"])
        columns = (*self.HISTORY_COLUMNS, "energy_context")
        placeholders = ", ".join("?" for _ in range(1 + len(values)))
        with self._lock, self._connect() as conn:
            cursor = conn.execute(
                f"INSERT INTO winter_energy_history (timestamp, {', '.join(columns)}) VALUES ({placeholders})",
                [timestamp, *values],
            )
            winter_sample_id = cursor.lastrowid
            self._store_hvac_samples(conn, winter_sample_id, timestamp, current.get("hvac") or [])
            self._store_decision(conn, winter_sample_id, timestamp, current["decision"])
            self._last_sample = timestamp
            self._last_error = None
        return current

    def current(self):
        try:
            sources = self.snapshot_provider() or {}
        except Exception as exc:
            self._log(f"Winter snapshot provider unavailable: {exc}")
            sources = {}
        temperatures = self._temperature_snapshot(sources.get("weather") or {})
        energy = self._energy_snapshot(sources.get("energy") or {})
        hvac, hvac_source_status = self._hvac_snapshot(sources.get("climate") or {})
        analysis = self._thermal_analysis(temperatures)
        energy_context, energy_context_source = self._energy_context_snapshot()
        return {
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "energy_context": energy_context,
            "energy_context_source": energy_context_source,
            "temperatures": temperatures,
            "energy": energy,
            "hvac": hvac,
            "hvac_source_status": hvac_source_status,
            "thermal_analysis": analysis,
            "hvac_learning": self._hvac_learning_analysis(),
            "decision": self._decision(temperatures, energy, analysis),
        }

    def status(self):
        current = self.current()
        with self._lock:
            last_sample, last_error = self._last_sample, self._last_error
        source_status = {
            "netatmo": current["temperatures"].get("source_status", "offline"),
            "goodwe": "online" if any(current["energy"].get(k) is not None for k in ("pv_production", "house_consumption", "battery_soc")) else "offline",
            "intesis": current.get("hvac_source_status", "offline"),
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
            "energy_context": current["energy_context"],
            "energy_context_source": current["energy_context_source"],
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
        columns = ", ".join(("timestamp", *self.HISTORY_COLUMNS, "energy_context"))
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                f"SELECT {columns} FROM winter_energy_history WHERE timestamp >= ? ORDER BY timestamp DESC LIMIT ?",
                (start, limit),
            ).fetchall()
        return [dict(row) for row in reversed(rows)]

    def decision_history(self, hours=24, limit=100):
        try:
            hours = max(1, min(24 * 365, int(hours)))
            limit = max(1, min(500, int(limit)))
        except (TypeError, ValueError):
            hours, limit = 24, 100
        start = (datetime.now() - timedelta(hours=hours)).isoformat(timespec="seconds")
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, timestamp, collector_session_id, winter_sample_id,
                       decision_code, decision_label, target_temperature,
                       indoor_average, coldest_room_temperature, coldest_room_name,
                       outdoor_temperature, indoor_outdoor_delta, indoor_trend,
                       pv_production, house_consumption, pv_surplus, battery_soc,
                       primary_reason_code, primary_reason_text, supporting_reasons_json,
                       input_data_quality, simulation_only, created_at
                FROM winter_decision_history
                WHERE timestamp >= ?
                ORDER BY timestamp DESC, id DESC
                LIMIT ?
                """,
                (start, limit),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            try:
                item["supporting_reasons"] = json.loads(item.pop("supporting_reasons_json") or "[]")
            except (TypeError, ValueError, json.JSONDecodeError):
                item["supporting_reasons"] = []
            item["simulation_only"] = bool(item["simulation_only"])
            result.append(item)
        return result

    def decision_daily_summary(self, date=None):
        day = str(date or datetime.now().date().isoformat())
        try:
            parsed = datetime.strptime(day, "%Y-%m-%d")
        except (TypeError, ValueError):
            parsed = datetime.now()
            day = parsed.date().isoformat()
        start = parsed.date().isoformat()
        end = (parsed.date() + timedelta(days=1)).isoformat()
        with self._lock, self._connect() as conn:
            row = conn.execute(
                """
                SELECT COUNT(*) AS total,
                       SUM(decision_code = 'WAIT_FOR_DATA') AS wait_for_data,
                       SUM(decision_code = 'SIMULATE_PREHEAT') AS would_preheat,
                       SUM(decision_code = 'DEFER_HEATING') AS defer_heating,
                       SUM(decision_code = 'NO_HEATING') AS no_heating,
                       SUM(decision_code = 'HOLD') AS hold,
                       SUM(input_data_quality = 'fresh') AS valid_inputs
                FROM winter_decision_history
                WHERE timestamp >= ? AND timestamp < ?
                """,
                (start, end),
            ).fetchone()
            hvac = conn.execute(
                """
                SELECT COUNT(*) AS total, SUM(h.data_quality = 'online') AS valid
                FROM winter_hvac_history h
                JOIN winter_decision_history d ON d.winter_sample_id = h.winter_sample_id
                WHERE d.timestamp >= ? AND d.timestamp < ?
                """,
                (start, end),
            ).fetchone()
        total = int(row["total"] or 0)
        hvac_total = int(hvac["total"] or 0)
        return {
            "date": day,
            "total": total,
            "counts": {
                "WAIT_FOR_DATA": int(row["wait_for_data"] or 0),
                "SIMULATE_PREHEAT": int(row["would_preheat"] or 0),
                "DEFER_HEATING": int(row["defer_heating"] or 0),
                "NO_HEATING": int(row["no_heating"] or 0),
                "HOLD": int(row["hold"] or 0),
            },
            "valid_input_percentage": round(100 * int(row["valid_inputs"] or 0) / total, 1) if total else None,
            "valid_hvac_observation_percentage": round(100 * int(hvac["valid"] or 0) / hvac_total, 1) if hvac_total else None,
        }

    def _energy_context_snapshot(self):
        source = "pool season state unavailable"
        try:
            snapshot = self.energy_context_provider() if self.energy_context_provider else None
            if isinstance(snapshot, dict):
                context = snapshot.get("energy_context")
                source = snapshot.get("source") or source
            else:
                context = snapshot
        except Exception:
            context = None
        if context not in {"POOL_ACTIVE", "POOL_DISABLED"}:
            context = "UNKNOWN"
        with self._lock:
            previous = self._last_energy_context
            if previous is None:
                if context == "UNKNOWN":
                    self._log("[WINTER] Pool season state unavailable, using UNKNOWN")
                else:
                    self._log(f"[WINTER] Energy context: {context}")
            elif context != previous:
                if context == "UNKNOWN":
                    self._log("[WINTER] Pool season state unavailable, using UNKNOWN")
                else:
                    self._log(f"[WINTER] Energy context changed: {previous} -> {context}")
            self._last_energy_context = context
        return context, source

    def _temperature_snapshot(self, netatmo):
        stations = netatmo.get("stations") or {}
        modules = netatmo.get("modules") or {}
        self._log_selected_netatmo_metrics(stations, modules)
        cache_fresh = self._cache_is_fresh(
            netatmo.get("last_successful_poll"),
            max(900, int(netatmo.get("poll_seconds") or 300) * 3),
        )
        service_online = bool(netatmo.get("online")) and netatmo.get("status") == "online"
        selected_module_names = {"terasa", "cezklanc (spalnica)", "soba otroci"}
        devices = [
            device for device in list(stations.values()) + list(modules.values())
            if self._normalized(device.get("home_name")) in {"cesclans", "cezklanc"}
            or self._normalized(device.get("name")) in selected_module_names
        ]
        selected = {"outdoor": None, "master_bedroom": None, "kids_room": None}
        names = {"outdoor": None, "master_bedroom": None, "kids_room": None}
        patterns = {
            "outdoor": ("terasa", "outdoor", "outside", "esterno", "esterna", "zunaj"),
            "master_bedroom": ("master", "matrimoniale", "bedroom", "camera letto", "spalnica"),
            "kids_room": ("soba otroci", "otroci", "kids", "kid", "children", "bimbi", "bambini", "ragazzi", "cameretta", "otroska"),
        }
        for device in devices:
            name = str(device.get("name") or "").strip()
            normalized = self._normalized(name)
            metric = (device.get("metrics") or {}).get("temperature") or {}
            # NetatmoService has already normalized the live dashboard value
            # into metrics.temperature.value. Global cache freshness is the
            # authoritative communication state for this shared snapshot.
            value = self._number(metric.get("value")) if cache_fresh and metric.get("online") else None
            if value is None:
                continue
            roles = list(patterns)
            if str(device.get("type") or "").upper() == "NAMODULE1":
                roles = ["outdoor"]
            for key in roles:
                tokens = patterns[key]
                if selected[key] is None and any(token in normalized for token in tokens):
                    selected[key], names[key] = value, name
                    break
                if key == "outdoor" and str(device.get("type") or "").upper() == "NAMODULE1":
                    selected[key], names[key] = value, name
                    break
        indoor = [value for key, value in selected.items() if key != "outdoor" and value is not None]
        average = round(sum(indoor) / len(indoor), 1) if indoor else None
        coldest = min(indoor) if indoor else None
        delta = round(average - selected["outdoor"], 1) if average is not None and selected["outdoor"] is not None else None
        trends = self._temperature_trends(selected)
        found_count = sum(value is not None for value in selected.values())
        if not service_online or not cache_fresh:
            source_status = "offline"
        elif found_count == len(selected):
            source_status = "online"
        else:
            source_status = "partial"
        return {
            **selected,
            "indoor_average": average,
            "coldest_room": names["master_bedroom"] if coldest == selected["master_bedroom"] else names["kids_room"] if coldest is not None else None,
            "coldest_room_temp": coldest,
            "indoor_outdoor_delta": delta,
            "device_names": names,
            "trends_c_per_hour": trends,
            "source_status": source_status,
            "cache_fresh": cache_fresh,
        }

    def _temperature_trends(self, current):
        cutoff = (datetime.now() - timedelta(hours=1)).isoformat(timespec="seconds")
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT timestamp, outdoor_temp, master_bedroom_temp, kids_room_temp FROM winter_energy_history WHERE timestamp >= ? ORDER BY timestamp ASC",
                (cutoff,),
            ).fetchall()
        if len(rows) < 2:
            return {key: None for key in current}
        columns = {"outdoor": "outdoor_temp", "master_bedroom": "master_bedroom_temp", "kids_room": "kids_room_temp"}
        trends = {}
        for key, value in current.items():
            usable = [row for row in rows if row[columns[key]] is not None]
            if value is None or len(usable) < 2:
                trends[key] = None
                continue
            try:
                elapsed = (datetime.now() - datetime.fromisoformat(usable[0]["timestamp"])).total_seconds()
            except (TypeError, ValueError):
                elapsed = 0
            trends[key] = round((value - usable[0][columns[key]]) / (elapsed / 3600), 2) if elapsed >= 240 else None
        return trends

    def _energy_snapshot(self, goodwe):
        return {
            "online": bool(goodwe),
            "pv_production": self._number(goodwe.get("pv_production")),
            "house_consumption": self._number(goodwe.get("house_consumption")),
            "battery_soc": self._number(goodwe.get("battery_soc")),
            "battery_power": self._number(goodwe.get("battery_power")),
            "grid_power": self._number(goodwe.get("grid_power")),
        }

    def _hvac_snapshot(self, intesis_cache):
        if isinstance(intesis_cache, dict):
            devices = intesis_cache.get("devices") or []
            driver = intesis_cache.get("status") or {}
        else:
            devices, driver = intesis_cache if isinstance(intesis_cache, list) else [], {}
        driver_fresh = bool(driver.get("connected")) and self._cache_is_fresh(
            driver.get("last_successful_poll"),
            max(900, int(driver.get("poll_seconds") or 300) * 3),
        )
        devices_by_id = {
            str(device.get("id")): device
            for device in devices
            if isinstance(device, dict) and str(device.get("id") or "") in self.CESCLANS_HVAC_DEVICES
        }
        result = []
        for device_id, configured_name in self.CESCLANS_HVAC_DEVICES.items():
            device = devices_by_id.get(device_id)
            if device is None:
                result.append({
                    "id": device_id,
                    "name": configured_name,
                    "online": False,
                    "state": "unknown",
                    "power": None,
                    "mode": None,
                    "temperature": None,
                    "target_temperature": None,
                    "fan_speed": None,
                    "active_demand": None,
                    "snapshot_timestamp": None,
                    "snapshot_age_seconds": None,
                    "data_quality": "missing_snapshot",
                })
                continue
            name = str(device.get("name") or configured_name).strip()
            has_telemetry = any(device.get(key) is not None for key in ("room_temperature", "target_temperature", "power", "mode"))
            maximum_age = max(900, int(driver.get("poll_seconds") or 300) * 3)
            snapshot_age = self._snapshot_age_seconds(device.get("last_update"))
            device_fresh = snapshot_age is not None and -60 <= snapshot_age <= maximum_age
            communication_active = device_fresh and has_telemetry
            error_code = self._number(device.get("error_code"))
            mode = device.get("mode")
            normalized_mode = str(mode or "").strip().lower()
            if not driver_fresh:
                quality = "driver_offline"
            elif not device_fresh:
                quality = "stale_snapshot"
            elif error_code not in (None, 0):
                quality = "device_error"
            elif not has_telemetry:
                quality = "missing_telemetry"
            elif device.get("power") not in (True, False) or not normalized_mode:
                quality = "unknown_state"
            elif normalized_mode and normalized_mode not in self.SUPPORTED_HVAC_MODES:
                quality = "unsupported_mode"
            else:
                quality = "online"
            if quality != "online":
                state = "offline"
            elif device.get("power") is True:
                state = "on"
            elif device.get("power") is False:
                state = "off"
            elif has_telemetry:
                state = "standby"
            else:
                state = "unknown"
            result.append({
                "id": device_id,
                "name": name,
                "online": state in {"on", "off", "standby"},
                "state": state,
                "power": device.get("power"),
                "mode": mode,
                "temperature": device.get("room_temperature"),
                "target_temperature": device.get("target_temperature"),
                "fan_speed": device.get("fan_speed"),
                "active_demand": self._first_present(
                    device, "active_demand", "working_state", "demand", "operation_state"
                ),
                "snapshot_timestamp": device.get("last_update"),
                "snapshot_age_seconds": snapshot_age,
                "data_quality": quality,
            })
        source_online = driver_fresh or any(device.get("online") for device in result)
        return result, ("online" if source_online else "offline")

    def _store_hvac_samples(self, conn, winter_sample_id, timestamp, devices):
        by_id = {
            str(device.get("id")): device
            for device in devices
            if isinstance(device, dict) and str(device.get("id") or "") in self.CESCLANS_HVAC_DEVICES
        }
        rows = []
        for device_id, configured_name in self.CESCLANS_HVAC_DEVICES.items():
            device = by_id.get(device_id) or {}
            power = device.get("power")
            rows.append((
                winter_sample_id,
                timestamp,
                self._collector_session_id,
                device_id,
                device.get("name") or configured_name,
                device.get("data_quality") or "missing_snapshot",
                self._sqlite_bool(device.get("online")),
                self._sqlite_bool(power),
                device.get("state"),
                device.get("mode"),
                self._number(device.get("target_temperature")),
                device.get("fan_speed"),
                self._number(device.get("temperature")),
                self._text_or_none(device.get("active_demand")),
                device.get("snapshot_timestamp"),
                self._number(device.get("snapshot_age_seconds")),
            ))
        conn.executemany(
            """
            INSERT OR IGNORE INTO winter_hvac_history (
                winter_sample_id, timestamp, collector_session_id,
                device_id, device_name,
                data_quality, online, power, device_state, mode,
                target_setpoint, fan_speed, room_temperature,
                active_demand, snapshot_timestamp, snapshot_age_seconds
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )

    def _hvac_learning_analysis(self, hours=24 * 30):
        cutoff = (datetime.now() - timedelta(hours=hours)).isoformat(timespec="seconds")
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                """
                SELECT timestamp, collector_session_id, device_id, device_name, data_quality,
                       power, mode, room_temperature
                FROM winter_hvac_history
                WHERE timestamp >= ?
                ORDER BY device_id, timestamp
                """,
                (cutoff,),
            ).fetchall()
        grouped = {device_id: [] for device_id in self.CESCLANS_HVAC_DEVICES}
        for row in rows:
            grouped.setdefault(row["device_id"], []).append(row)
        devices = []
        for device_id, configured_name in self.CESCLANS_HVAC_DEVICES.items():
            device_rows = grouped.get(device_id, [])
            valid_count = sum(
                1 for row in device_rows
                if row["data_quality"] == "online" and row["room_temperature"] is not None
            )
            devices.append({
                "device_id": device_id,
                "device_name": next(
                    (row["device_name"] for row in reversed(device_rows) if row["device_name"]),
                    configured_name,
                ),
                "valid_historical_samples": valid_count,
                "observed_rates": {
                    "hvac_off": self._observed_rate(device_rows, "off"),
                    "heating": self._observed_rate(device_rows, "heat"),
                    "cooling": self._observed_rate(device_rows, "cool"),
                },
            })
        return {
            "label": "preliminary_observed_rates",
            "window_hours": hours,
            "minimum_samples": self.HVAC_RATE_MIN_SAMPLES,
            "minimum_duration_seconds": self.HVAC_RATE_MIN_SECONDS,
            "devices": devices,
        }

    def _observed_rate(self, rows, category):
        segments, current_segment = [], []
        previous_time = None
        previous_mode = None
        previous_session = None
        maximum_gap = max(self.sample_seconds * 2.5, 15 * 60)
        for row in rows:
            row_category = self._hvac_rate_category(row)
            try:
                timestamp = datetime.fromisoformat(row["timestamp"])
            except (TypeError, ValueError):
                timestamp = None
            gap = (timestamp - previous_time).total_seconds() if timestamp and previous_time else None
            mode = str(row["mode"] or "").strip().lower()
            session = row["collector_session_id"]
            boundary = (
                row_category != category
                or timestamp is None
                or (gap is not None and gap > maximum_gap)
                or (previous_mode is not None and mode != previous_mode)
                or (previous_session is not None and session != previous_session)
            )
            if boundary:
                if current_segment:
                    segments.append(current_segment)
                current_segment = []
            if row_category == category and timestamp is not None:
                current_segment.append((timestamp, float(row["room_temperature"])))
                previous_mode = mode
                previous_session = session
            else:
                previous_mode = None
                previous_session = None
            previous_time = timestamp
        if current_segment:
            segments.append(current_segment)
        qualifying = []
        for segment in segments:
            elapsed = (segment[-1][0] - segment[0][0]).total_seconds()
            if len(segment) < self.HVAC_RATE_MIN_SAMPLES or elapsed < self.HVAC_RATE_MIN_SECONDS:
                continue
            rate = (segment[-1][1] - segment[0][1]) / (elapsed / 3600)
            qualifying.append((rate, elapsed, len(segment)))
        if not qualifying:
            return {
                "c_per_hour": None,
                "valid_samples": 0,
                "observed_seconds": 0,
                "segments": 0,
                "status": "insufficient_data",
            }
        total_seconds = sum(item[1] for item in qualifying)
        weighted_rate = sum(rate * elapsed for rate, elapsed, _ in qualifying) / total_seconds
        return {
            "c_per_hour": round(weighted_rate, 2),
            "valid_samples": sum(item[2] for item in qualifying),
            "observed_seconds": int(total_seconds),
            "segments": len(qualifying),
            "status": "preliminary",
        }

    @staticmethod
    def _hvac_rate_category(row):
        if row["data_quality"] != "online" or row["room_temperature"] is None:
            return None
        if row["power"] == 0:
            return "off"
        mode = str(row["mode"] or "").strip().lower()
        if row["power"] == 1 and mode in {"heat", "cool"}:
            return mode
        return None

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

    def _log_selected_netatmo_metrics(self, stations, modules):
        selected_names = {
            "terasa": "Terasa",
            "cezklanc (spalnica)": "Čezklanc (Spalnica)",
            "soba otroci": "Soba Otroci",
        }
        for device in list(stations.values()) + list(modules.values()):
            if not isinstance(device, dict):
                continue
            normalized_name = self._normalized(device.get("name"))
            label = selected_names.get(normalized_name)
            if not label or normalized_name in self._logged_netatmo_metrics:
                continue
            metrics = device.get("metrics") if isinstance(device.get("metrics"), dict) else {}
            self._log(f"{label} metrics =\n{json.dumps(metrics, indent=2, ensure_ascii=False, default=str)}")
            self._logged_netatmo_metrics.add(normalized_name)

    @staticmethod
    def _cache_is_fresh(timestamp, maximum_age_seconds):
        if not timestamp:
            return False
        try:
            parsed = datetime.fromisoformat(str(timestamp).replace("Z", "+00:00"))
            current = datetime.now(parsed.tzinfo) if parsed.tzinfo else datetime.now()
            age = (current - parsed).total_seconds()
            return -60 <= age <= maximum_age_seconds
        except (TypeError, ValueError):
            return False

    @staticmethod
    def _normalized(value):
        text = unicodedata.normalize("NFKD", str(value or ""))
        return " ".join("".join(char for char in text if not unicodedata.combining(char)).lower().split())

    def _decision(self, temperatures, energy, analysis):
        target = self._number(self.config.get("target_temp")) or DEFAULT_WINTER_CONFIG["target_temp"]
        indoor = temperatures.get("indoor_average")
        pv = energy.get("pv_production")
        load = energy.get("house_consumption")
        soc = energy.get("battery_soc")
        surplus = pv - load if pv is not None and load is not None else None
        if indoor is None:
            recommendation = "WAIT_FOR_DATA"
            if temperatures.get("source_status") == "offline" and not temperatures.get("cache_fresh"):
                primary = ("INPUT_DATA_STALE", "Indoor temperature data is unavailable because the source data is stale or offline.")
                quality = "stale"
            else:
                primary = ("INPUT_DATA_MISSING", "Required indoor temperature data is currently unavailable.")
                quality = "missing"
        elif indoor < target - 0.5 and surplus is not None and surplus > 1200 and (soc is None or soc >= 50):
            recommendation = "SIMULATE_PREHEAT"
            primary = ("PV_SURPLUS_AVAILABLE", "Indoor temperature is below target and energy conditions would support simulated preheating.")
            quality = self._decision_data_quality(temperatures, energy)
        elif indoor < target - 0.5:
            recommendation = "DEFER_HEATING"
            if surplus is None:
                primary = ("INPUT_DATA_PARTIAL", "Indoor temperature is below target, but photovoltaic surplus cannot currently be calculated.")
            elif surplus <= 1200:
                primary = ("PV_SURPLUS_INSUFFICIENT", "Indoor temperature is below target, but available photovoltaic surplus is insufficient.")
            else:
                primary = ("BATTERY_SOC_LOW", "Indoor temperature is below target, but battery state is below the configured threshold.")
            quality = self._decision_data_quality(temperatures, energy)
        elif indoor > target + 0.5:
            recommendation = "NO_HEATING"
            primary = ("INDOOR_ABOVE_TARGET", "Indoor temperature is already above the configured target.")
            quality = self._decision_data_quality(temperatures, energy)
        else:
            recommendation = "HOLD"
            primary = ("DECISION_RULE_HOLD", "Indoor temperature is within the configured target comfort band.")
            quality = self._decision_data_quality(temperatures, energy)
        supporting = self._decision_supporting_reasons(indoor, target, surplus, soc, analysis)
        alternatives = self._alternative_reasons(recommendation, surplus, soc, analysis)
        trend = analysis.get("indoor_trend_c_per_hour")
        thermal_observation = (
            analysis.get("estimated_thermal_inertia_hours")
            if analysis.get("quality") == "preliminary" and trend is not None and abs(trend) >= 0.02
            else None
        )
        inputs = {
            "indoor_average": indoor,
            "coldest_room_name": temperatures.get("coldest_room"),
            "coldest_room_temperature": temperatures.get("coldest_room_temp"),
            "target_temperature": target,
            "outdoor_temperature": temperatures.get("outdoor"),
            "indoor_outdoor_delta": temperatures.get("indoor_outdoor_delta"),
            "pv_production": pv,
            "house_consumption": load,
            "pv_surplus": surplus,
            "battery_soc": soc,
            "indoor_trend_c_per_hour": trend,
            "thermal_observation_hours": thermal_observation,
            "data_quality": quality,
        }
        return {
            "recommendation": recommendation,
            "decision_code": recommendation,
            "display_label": self.DECISION_LABELS[recommendation],
            "reason": primary[1],
            "primary_reason_code": primary[0],
            "primary_reason_text": primary[1],
            "supporting_reasons": supporting,
            "alternative_reasons": alternatives,
            "inputs": inputs,
            "target_temperature": target,
            "available_surplus": surplus,
            "rule_type": "rule_based_simulation",
            "simulation_only": True,
            "hvac_command_sent": False,
        }

    @staticmethod
    def _decision_data_quality(temperatures, energy):
        if temperatures.get("source_status") == "offline" or not temperatures.get("cache_fresh"):
            return "stale"
        required = (
            temperatures.get("indoor_average"), temperatures.get("outdoor"),
            energy.get("pv_production"), energy.get("house_consumption"),
        )
        return "fresh" if all(value is not None for value in required) else "partial"

    @staticmethod
    def _reason(code, text):
        return {"code": code, "text": text}

    def _decision_supporting_reasons(self, indoor, target, surplus, soc, analysis):
        reasons = []
        if indoor is not None:
            reasons.append(self._reason(
                "INDOOR_BELOW_TARGET" if indoor < target - 0.5 else
                "INDOOR_ABOVE_TARGET" if indoor > target + 0.5 else "INDOOR_WITHIN_TARGET_BAND",
                "Indoor temperature is below target." if indoor < target - 0.5 else
                "Indoor temperature is above target." if indoor > target + 0.5 else
                "Indoor temperature is within the target comfort band.",
            ))
        if surplus is not None and indoor is not None and indoor < target - 0.5:
            reasons.append(self._reason(
                "PV_SURPLUS_AVAILABLE" if surplus > 1200 else "PV_SURPLUS_INSUFFICIENT",
                "Photovoltaic surplus is above the configured 1200 W threshold." if surplus > 1200 else
                "Photovoltaic surplus is not above the configured 1200 W threshold.",
            ))
        if soc is not None and indoor is not None and indoor < target - 0.5 and surplus is not None and surplus > 1200:
            reasons.append(self._reason(
                "BATTERY_SOC_ACCEPTABLE" if soc >= 50 else "BATTERY_SOC_LOW",
                "Battery SOC is at or above the configured 50% threshold." if soc >= 50 else
                "Battery SOC is below the configured 50% threshold.",
            ))
        trend = analysis.get("indoor_trend_c_per_hour")
        if trend is None:
            reasons.append(self._reason("TREND_DATA_INSUFFICIENT", "There is insufficient continuous history for a one-hour indoor trend."))
        elif abs(trend) < 0.02:
            reasons.append(self._reason("TEMPERATURE_STABLE", "The recent indoor temperature trend is approximately stable."))
        return reasons

    def _alternative_reasons(self, recommendation, surplus, soc, analysis):
        reasons = []
        if recommendation == "WAIT_FOR_DATA":
            reasons.append(self._reason("NO_RECOMMENDATION_INPUT_UNAVAILABLE", "No heating recommendation is made because required input data is unavailable or stale."))
        elif recommendation == "NO_HEATING":
            reasons.append(self._reason("NO_PREHEAT_INDOOR_ABOVE_TARGET", "No preheating because indoor temperature is already above target."))
        elif recommendation == "DEFER_HEATING":
            if surplus is None:
                reasons.append(self._reason("NO_PREHEAT_SURPLUS_UNKNOWN", "No preheating because photovoltaic surplus is unavailable."))
            elif surplus <= 1200:
                reasons.append(self._reason("NO_PREHEAT_PV_INSUFFICIENT", "No preheating because photovoltaic surplus is not above 1200 W."))
            elif soc is not None and soc < 50:
                reasons.append(self._reason("NO_PREHEAT_BATTERY_LOW", "No preheating because battery SOC is below 50%."))
        elif recommendation == "HOLD":
            reasons.append(self._reason("NO_ACTION_WITHIN_TARGET_BAND", "No immediate action because indoor temperature is within the target comfort band."))
            trend = analysis.get("indoor_trend_c_per_hour")
            if trend is not None and abs(trend) < 0.02:
                reasons.append(self._reason("NO_ACTION_TEMPERATURE_STABLE", "No immediate action because the recent temperature trend is stable."))
        return reasons

    def _store_decision(self, conn, winter_sample_id, timestamp, decision):
        inputs = decision.get("inputs") or {}
        conn.execute(
            """
            INSERT OR IGNORE INTO winter_decision_history (
                timestamp, collector_session_id, winter_sample_id,
                decision_code, decision_label, target_temperature,
                indoor_average, coldest_room_temperature, coldest_room_name,
                outdoor_temperature, indoor_outdoor_delta, indoor_trend,
                pv_production, house_consumption, pv_surplus, battery_soc,
                primary_reason_code, primary_reason_text, supporting_reasons_json,
                input_data_quality, simulation_only
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                timestamp, self._collector_session_id, winter_sample_id,
                decision["decision_code"], decision["display_label"], inputs.get("target_temperature"),
                inputs.get("indoor_average"), inputs.get("coldest_room_temperature"), inputs.get("coldest_room_name"),
                inputs.get("outdoor_temperature"), inputs.get("indoor_outdoor_delta"), inputs.get("indoor_trend_c_per_hour"),
                inputs.get("pv_production"), inputs.get("house_consumption"), inputs.get("pv_surplus"), inputs.get("battery_soc"),
                decision["primary_reason_code"], decision["primary_reason_text"],
                json.dumps(decision.get("supporting_reasons") or [], ensure_ascii=False, separators=(",", ":")),
                inputs.get("data_quality") or "missing", 1,
            ),
        )

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
                    energy_context TEXT NOT NULL DEFAULT 'UNKNOWN',
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
            """)
            existing_columns = {
                row["name"] for row in conn.execute("PRAGMA table_info(winter_energy_history)").fetchall()
            }
            if "energy_context" not in existing_columns:
                conn.execute(
                    "ALTER TABLE winter_energy_history "
                    "ADD COLUMN energy_context TEXT NOT NULL DEFAULT 'UNKNOWN'"
                )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_winter_energy_timestamp ON winter_energy_history (timestamp)")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS winter_hvac_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    winter_sample_id INTEGER NOT NULL,
                    timestamp TEXT NOT NULL,
                    collector_session_id TEXT NOT NULL,
                    device_id TEXT NOT NULL,
                    device_name TEXT NULL,
                    data_quality TEXT NOT NULL,
                    online INTEGER NULL,
                    power INTEGER NULL,
                    device_state TEXT NULL,
                    mode TEXT NULL,
                    target_setpoint REAL NULL,
                    fan_speed TEXT NULL,
                    room_temperature REAL NULL,
                    active_demand TEXT NULL,
                    snapshot_timestamp TEXT NULL,
                    snapshot_age_seconds REAL NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (winter_sample_id) REFERENCES winter_energy_history(id),
                    UNIQUE (winter_sample_id, device_id)
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_winter_hvac_device_timestamp
                ON winter_hvac_history (device_id, timestamp)
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_winter_hvac_sample
                ON winter_hvac_history (winter_sample_id)
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS winter_decision_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    collector_session_id TEXT NOT NULL,
                    winter_sample_id INTEGER NOT NULL UNIQUE,
                    decision_code TEXT NOT NULL,
                    decision_label TEXT NOT NULL,
                    target_temperature REAL NOT NULL,
                    indoor_average REAL NULL,
                    coldest_room_temperature REAL NULL,
                    coldest_room_name TEXT NULL,
                    outdoor_temperature REAL NULL,
                    indoor_outdoor_delta REAL NULL,
                    indoor_trend REAL NULL,
                    pv_production REAL NULL,
                    house_consumption REAL NULL,
                    pv_surplus REAL NULL,
                    battery_soc REAL NULL,
                    primary_reason_code TEXT NOT NULL,
                    primary_reason_text TEXT NOT NULL,
                    supporting_reasons_json TEXT NOT NULL DEFAULT '[]',
                    input_data_quality TEXT NOT NULL,
                    simulation_only INTEGER NOT NULL DEFAULT 1 CHECK (simulation_only = 1),
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (winter_sample_id) REFERENCES winter_energy_history(id)
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_winter_decision_timestamp
                ON winter_decision_history (timestamp DESC)
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_winter_decision_code_timestamp
                ON winter_decision_history (decision_code, timestamp DESC)
            """)

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
        conn.execute("PRAGMA foreign_keys=ON")
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

    @staticmethod
    def _snapshot_age_seconds(timestamp):
        if not timestamp:
            return None
        try:
            parsed = datetime.fromisoformat(str(timestamp).replace("Z", "+00:00"))
            current = datetime.now(parsed.tzinfo) if parsed.tzinfo else datetime.now()
            return round((current - parsed).total_seconds(), 1)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _first_present(source, *keys):
        for key in keys:
            if source.get(key) is not None:
                return source.get(key)
        return None

    @staticmethod
    def _sqlite_bool(value):
        if value is True:
            return 1
        if value is False:
            return 0
        return None

    @staticmethod
    def _text_or_none(value):
        return None if value is None else str(value)
