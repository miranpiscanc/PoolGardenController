from abc import ABC, abstractmethod


class EnergyProvider(ABC):
    """Vendor-neutral read interface for energy telemetry."""

    provider_id = "unknown"

    @abstractmethod
    def get_status_snapshot(self):
        raise NotImplementedError

    def get_dashboard_snapshot(self):
        return self.get_status_snapshot()

    def get_pv_power(self):
        return self.get_status_snapshot().get("pv_production")

    def get_house_consumption(self):
        return self.get_status_snapshot().get("house_consumption")

    def get_battery_soc(self):
        return self.get_status_snapshot().get("battery_soc")

    def get_grid_power(self):
        return self.get_status_snapshot().get("grid_power")

    def get_available_surplus(self):
        pv = _number(self.get_pv_power())
        house = _number(self.get_house_consumption())
        return None if pv is None or house is None else max(0, pv - house)


class WeatherProvider(ABC):
    """Vendor-neutral read interface for environmental telemetry."""

    provider_id = "unknown"

    @abstractmethod
    def get_snapshot(self):
        raise NotImplementedError

    @abstractmethod
    def get_outdoor_temperature(self):
        raise NotImplementedError

    @abstractmethod
    def get_indoor_temperature(self):
        raise NotImplementedError

    @abstractmethod
    def get_humidity(self):
        raise NotImplementedError

    @abstractmethod
    def get_wind(self):
        raise NotImplementedError

    @abstractmethod
    def get_rain(self):
        raise NotImplementedError


class ClimateProvider(ABC):
    """Vendor-neutral read interface for climate devices."""

    provider_id = "unknown"

    @abstractmethod
    def get_snapshot(self):
        raise NotImplementedError

    def get_devices(self):
        return list(self.get_snapshot().get("devices") or [])


class PoolProvider(ABC):
    """House-scoped pool module interface. Commands remain in legacy control code."""

    provider_id = "unknown"

    @abstractmethod
    def is_enabled(self):
        raise NotImplementedError

    @abstractmethod
    def get_status_snapshot(self):
        raise NotImplementedError


class NotificationProvider(ABC):
    """Reserved extension point for future house-specific notifications."""

    provider_id = "unknown"

    @abstractmethod
    def send(self, message):
        raise NotImplementedError


def _number(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
