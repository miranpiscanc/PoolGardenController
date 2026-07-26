from .base import ClimateProvider, EnergyProvider, PoolProvider, WeatherProvider


class GoodWeEnergyProvider(EnergyProvider):
    """Adapter around the existing GoodWe cache readers; it performs no polling."""

    provider_id = "goodwe"

    def __init__(self, status_reader, dashboard_reader=None):
        self._status_reader = status_reader
        self._dashboard_reader = dashboard_reader or status_reader

    def get_status_snapshot(self):
        return dict(self._status_reader() or {})

    def get_dashboard_snapshot(self):
        return dict(self._dashboard_reader() or {})


class NetatmoWeatherProvider(WeatherProvider):
    """Adapter around the existing Netatmo cache; it performs no polling."""

    provider_id = "netatmo"

    def __init__(self, snapshot_reader):
        self._snapshot_reader = snapshot_reader

    def get_snapshot(self):
        return dict(self._snapshot_reader() or {})

    def get_outdoor_temperature(self):
        return self._first_metric("temperature", outdoor=True)

    def get_indoor_temperature(self):
        return self._first_metric("temperature", outdoor=False)

    def get_humidity(self):
        return self._first_metric("humidity", outdoor=True)

    def get_wind(self):
        return self._first_available_metric(("wind_strength", "windstrength", "wind"))

    def get_rain(self):
        return self._first_available_metric(("rain", "rainfall", "sum_rain_24"))

    def _devices(self):
        snapshot = self.get_snapshot()
        stations = snapshot.get("stations") if isinstance(snapshot.get("stations"), dict) else {}
        modules = snapshot.get("modules") if isinstance(snapshot.get("modules"), dict) else {}
        return list(stations.values()) + list(modules.values())

    def _first_metric(self, metric_name, outdoor):
        devices = self._devices()
        ordered = sorted(
            devices,
            key=lambda device: str(device.get("type") or "").upper() != "NAMODULE1" if outdoor else str(device.get("type") or "").upper() == "NAMODULE1",
        )
        for device in ordered:
            is_outdoor = str(device.get("type") or "").upper() == "NAMODULE1"
            if is_outdoor != outdoor:
                continue
            metric = (device.get("metrics") or {}).get(metric_name) or {}
            if metric.get("online") and metric.get("value") is not None:
                return metric.get("value")
        return None

    def _first_available_metric(self, metric_names):
        for device in self._devices():
            metrics = device.get("metrics") or {}
            for name in metric_names:
                metric = metrics.get(name) or {}
                if metric.get("online") and metric.get("value") is not None:
                    return metric.get("value")
        return None


class IntesisClimateProvider(ClimateProvider):
    """Adapter around existing Intesis read functions; it sends no commands."""

    provider_id = "intesis"

    def __init__(self, snapshot_reader):
        self._snapshot_reader = snapshot_reader

    def get_snapshot(self):
        snapshot = self._snapshot_reader() or {}
        return {
            "devices": list(snapshot.get("devices") or []),
            "status": dict(snapshot.get("status") or {}),
        }


class CesclansPoolProvider(PoolProvider):
    """Metadata/status wrapper around the current pool module; no command methods."""

    provider_id = "cesclans_pool"

    def __init__(self, enabled_reader, status_reader):
        self._enabled_reader = enabled_reader
        self._status_reader = status_reader

    def is_enabled(self):
        return bool(self._enabled_reader())

    def get_status_snapshot(self):
        return dict(self._status_reader() or {})
