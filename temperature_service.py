import threading
import time
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime
from typing import Callable, Dict, Optional


class TemperatureService:
    def __init__(self, sensors, poll_seconds=30, timeout_seconds=5, event_callback: Optional[Callable] = None):
        self.sensors = sensors
        self.poll_seconds = poll_seconds
        self.timeout_seconds = timeout_seconds
        self.event_callback = event_callback
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread = None
        self._state = {
            key: {
                "label": sensor["label"],
                "icon": sensor["icon"],
                "url": sensor["url"],
                "sensor_id": sensor["sensor_id"],
                "value": None,
                "unit": "°C",
                "online": False,
                "initialized": False,
                "last_valid_update": None,
                "last_attempt": None,
                "error": ""
            }
            for key, sensor in sensors.items()
        }

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()

    def snapshot(self):
        with self._lock:
            sensors = {key: dict(value) for key, value in self._state.items()}
        online_count = sum(1 for sensor in sensors.values() if sensor.get("online"))
        total = len(sensors)
        if online_count == total:
            communication = {"status": "online", "label": "Both thermometers online"}
        elif online_count == 0:
            communication = {"status": "offline", "label": "Both thermometers offline"}
        else:
            communication = {"status": "partial", "label": "One thermometer offline"}
        last_update = max(
            (sensor.get("last_valid_update") for sensor in sensors.values() if sensor.get("last_valid_update")),
            default=None
        )
        return {
            "sensors": sensors,
            "communication": communication,
            "last_update": last_update,
            "poll_seconds": self.poll_seconds
        }

    def _run(self):
        while not self._stop.is_set():
            started = time.monotonic()
            self.poll_once()
            elapsed = time.monotonic() - started
            self._stop.wait(max(0, self.poll_seconds - elapsed))

    def poll_once(self):
        for key, sensor in self.sensors.items():
            self._poll_sensor(key, sensor)

    def _poll_sensor(self, key, sensor):
        attempted_at = datetime.now().isoformat(timespec="seconds")
        try:
            value = self._read_temperature(sensor["url"], sensor["sensor_id"])
            with self._lock:
                was_online = self._state[key]["online"]
                initialized = self._state[key]["initialized"]
                self._state[key].update({
                    "value": value,
                    "online": True,
                    "initialized": True,
                    "last_valid_update": attempted_at,
                    "last_attempt": attempted_at,
                    "error": ""
                })
            if initialized and was_online is False:
                self._record_event(key, "ONLINE", "Thermometer communication restored")
        except Exception as exc:
            with self._lock:
                was_online = self._state[key]["online"]
                initialized = self._state[key]["initialized"]
                self._state[key].update({
                    "online": False,
                    "initialized": True,
                    "last_attempt": attempted_at,
                    "error": str(exc)
                })
            if initialized and was_online is True:
                self._record_event(key, "OFFLINE", str(exc))

    def _read_temperature(self, url, sensor_id):
        with urllib.request.urlopen(url, timeout=self.timeout_seconds) as response:
            data = response.read()
        root = ET.fromstring(data)
        target_id = str(sensor_id)
        for entry in root.iter():
            id_node = entry.find("ID")
            value_node = entry.find("Value")
            if id_node is None or value_node is None:
                continue
            if (id_node.text or "").strip() != target_id:
                continue
            value_text = (value_node.text or "").strip().replace(",", ".")
            if not value_text:
                raise ValueError(f"Sensor {target_id} has no temperature value")
            return round(float(value_text), 1)
        raise ValueError(f"Sensor {target_id} not found")

    def _record_event(self, key, state, reason):
        if not self.event_callback:
            return
        sensor = self.sensors[key]
        self.event_callback(
            sensor["label"],
            f"Sensor ID {sensor['sensor_id']}",
            f"{sensor['label']} thermometer {state}",
            "Temperature Service",
            reason
        )


DEFAULT_TEMPERATURE_SENSORS: Dict[str, Dict[str, object]] = {
    "water": {
        "label": "Water",
        "icon": "💧",
        "url": "http://192.168.200.112:54200/values.xml",
        "sensor_id": 216
    },
    "outside": {
        "label": "Outside",
        "icon": "🌤",
        "url": "http://192.168.200.111:54200/values.xml",
        "sensor_id": 215
    }
}
