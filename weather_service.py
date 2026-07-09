import threading
import time
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime
from typing import Callable, Dict, Optional


class WeatherService:
    def __init__(self, config, event_callback: Optional[Callable] = None, log_callback: Optional[Callable] = None):
        config = config or {}
        self.config = config
        self.poll_seconds = int(config.get("poll_seconds", 30))
        self.timeout_seconds = float(config.get("timeout_seconds", 5))
        self.retry_count = max(1, int(config.get("retry_count", 2)))
        self.event_callback = event_callback
        self.log_callback = log_callback
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread = None
        self._state = self._initial_state(config)

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        self._log("WeatherService started")

    def stop(self):
        self._stop.set()

    def snapshot(self):
        with self._lock:
            locations = {
                key: {
                    **location,
                    "metrics": {metric_key: dict(metric) for metric_key, metric in location.get("metrics", {}).items()}
                }
                for key, location in self._state.get("locations", {}).items()
            }
            sources = {key: dict(value) for key, value in self._state.get("sources", {}).items()}
        online_count = sum(1 for source in sources.values() if source.get("online"))
        total = len(sources)
        if total == 0:
            communication = {"status": "unconfigured", "label": "No weather sensors configured"}
        elif online_count == total:
            communication = {"status": "online", "label": "Weather sensors online"}
        elif online_count == 0:
            communication = {"status": "offline", "label": "Weather sensors offline"}
        else:
            communication = {"status": "partial", "label": "Some weather sensors offline"}
        last_update = max(
            (
                metric.get("last_valid_update")
                for location in locations.values()
                for metric in location.get("metrics", {}).values()
                if metric.get("last_valid_update")
            ),
            default=None
        )
        return {
            "locations": locations,
            "sources": sources,
            "communication": communication,
            "last_update": last_update,
            "poll_seconds": self.poll_seconds
        }

    def _initial_state(self, config):
        sources = {
            key: {
                "label": source.get("label", key),
                "type": source.get("type"),
                "url": self._source_url(source),
                "online": False,
                "initialized": False,
                "last_valid_update": None,
                "last_attempt": None,
                "error": ""
            }
            for key, source in (config.get("sources") or {}).items()
        }
        locations = {}
        for location_key, location in (config.get("locations") or {}).items():
            metrics = {}
            for metric_key, metric in (location.get("metrics") or {}).items():
                metrics[metric_key] = {
                    "label": metric.get("label", metric_key),
                    "unit": metric.get("unit", ""),
                    "source": metric.get("source"),
                    "sensor_id": metric.get("sensor_id"),
                    "placeholder": bool(metric.get("placeholder")),
                    "value": None,
                    "online": False,
                    "initialized": bool(metric.get("placeholder")),
                    "last_valid_update": None,
                    "last_attempt": None,
                    "error": "Not configured" if metric.get("placeholder") else ""
                }
            locations[location_key] = {
                "label": location.get("label", location_key),
                "metrics": metrics
            }
        return {"sources": sources, "locations": locations}

    def _run(self):
        while not self._stop.is_set():
            started = time.monotonic()
            self.poll_once()
            elapsed = time.monotonic() - started
            self._stop.wait(max(0, self.poll_seconds - elapsed))

    def poll_once(self):
        self._log("Polling weather sensors...")
        for source_key, source in (self.config.get("sources") or {}).items():
            if source.get("type") == "hwg_ste":
                self._poll_hwg_ste_source(source_key, source)

    def _poll_hwg_ste_source(self, source_key, source):
        attempted_at = datetime.now().isoformat(timespec="seconds")
        try:
            values = self._read_hwg_values(self._source_url(source))
            with self._lock:
                was_online = self._state["sources"][source_key]["online"]
                initialized = self._state["sources"][source_key]["initialized"]
                self._state["sources"][source_key].update({
                    "online": True,
                    "initialized": True,
                    "last_valid_update": attempted_at,
                    "last_attempt": attempted_at,
                    "error": ""
                })
                parsed_values = self._apply_source_values(source_key, values, attempted_at)
            self._log(
                "Parsed values:\n"
                f"temperature={parsed_values.get('temperature')}\n"
                f"humidity={parsed_values.get('humidity')}"
            )
            self._log("Dashboard update completed.")
            if initialized and was_online is False:
                self._record_event(source_key, "ONLINE", "Weather sensor communication restored")
        except Exception as exc:
            with self._lock:
                was_online = self._state["sources"][source_key]["online"]
                initialized = self._state["sources"][source_key]["initialized"]
                self._state["sources"][source_key].update({
                    "online": False,
                    "initialized": True,
                    "last_attempt": attempted_at,
                    "error": str(exc)
                })
                self._mark_source_metrics_offline(source_key, attempted_at, str(exc))
            if initialized and was_online is True:
                self._record_event(source_key, "OFFLINE", str(exc))

    def _apply_source_values(self, source_key, values, timestamp):
        parsed_values = {}
        for location in self._state["locations"].values():
            for metric_key, metric in location.get("metrics", {}).items():
                if metric.get("source") != source_key or metric.get("placeholder"):
                    continue
                sensor_id = str(metric.get("sensor_id"))
                if sensor_id not in values:
                    metric.update({
                        "online": False,
                        "initialized": True,
                        "last_attempt": timestamp,
                        "error": f"Sensor {sensor_id} not found"
                    })
                    continue
                metric.update({
                    "value": values[sensor_id],
                    "online": True,
                    "initialized": True,
                    "last_valid_update": timestamp,
                    "last_attempt": timestamp,
                    "error": ""
                })
                parsed_values[metric_key] = values[sensor_id]
        return parsed_values

    def _mark_source_metrics_offline(self, source_key, timestamp, error):
        for location in self._state["locations"].values():
            for metric in location.get("metrics", {}).values():
                if metric.get("source") != source_key or metric.get("placeholder"):
                    continue
                metric.update({
                    "online": False,
                    "initialized": True,
                    "last_attempt": timestamp,
                    "error": error
                })

    def _read_hwg_values(self, url):
        last_error = None
        for attempt in range(self.retry_count):
            try:
                self._log(f"Downloading:\n{url}")
                with urllib.request.urlopen(url, timeout=self.timeout_seconds) as response:
                    data = response.read()
                values = self._parse_hwg_values(data)
                self._log(f"Raw values:\n{values}")
                return values
            except Exception as exc:
                last_error = exc
                if attempt + 1 < self.retry_count:
                    time.sleep(0.2)
        raise last_error

    def _parse_hwg_values(self, data):
        root = ET.fromstring(data)
        values = {}
        for entry in root.iter():
            id_node = entry.find("ID")
            value_node = entry.find("Value")
            if id_node is None or value_node is None:
                continue
            sensor_id = (id_node.text or "").strip()
            value_text = (value_node.text or "").strip().replace(",", ".")
            if sensor_id and value_text:
                values[sensor_id] = round(float(value_text), 1)
        if not values:
            raise ValueError("No weather sensor values found")
        return values

    def _source_url(self, source):
        if source.get("url"):
            return source["url"]
        address = str(source.get("address", "")).rstrip("/")
        path = source.get("path", "/values.xml")
        return address + path

    def _record_event(self, source_key, state, reason):
        if not self.event_callback:
            return
        source = (self.config.get("sources") or {}).get(source_key, {})
        self.event_callback(
            source.get("label", source_key),
            source.get("type", "weather"),
            f"{source.get('label', source_key)} weather source {state}",
            "Weather Service",
            reason
        )

    def _log(self, message):
        if self.log_callback:
            self.log_callback(message)
        else:
            print(message)


DEFAULT_WEATHER_CONFIG: Dict[str, object] = {
    "poll_seconds": 30,
    "timeout_seconds": 5,
    "retry_count": 2,
    "sources": {
        "opicina_hwg_ste": {
            "label": "Opicina HWg-STE",
            "type": "hwg_ste",
            "address": "http://192.168.10.122:54200",
            "path": "/values.xml"
        },
        "cesclans_hwg_ste": {
            "label": "Cesclans HWg-STE",
            "type": "hwg_ste",
            "address": "http://192.168.200.111:54200",
            "path": "/values.xml"
        }
    },
    "locations": {
        "opicina": {
            "label": "Opicina",
            "metrics": {
                "temperature": {
                    "label": "Temperature",
                    "unit": "°C",
                    "source": "opicina_hwg_ste",
                    "sensor_id": 215
                },
                "humidity": {
                    "label": "Humidity",
                    "unit": "%",
                    "source": "opicina_hwg_ste",
                    "sensor_id": 216
                }
            }
        },
        "cesclans": {
            "label": "Cesclans",
            "metrics": {
                "temperature": {
                    "label": "Temperature",
                    "unit": "°C",
                    "source": "cesclans_hwg_ste",
                    "sensor_id": 215
                }
            }
        }
    }
}
