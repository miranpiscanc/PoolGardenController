from dataclasses import dataclass
from threading import RLock

from .base import ClimateProvider, EnergyProvider, NotificationProvider, PoolProvider, WeatherProvider


class ProviderNotConfiguredError(LookupError):
    pass


@dataclass(frozen=True)
class HouseProviders:
    house_id: str
    energy: EnergyProvider | None = None
    weather: WeatherProvider | None = None
    climate: ClimateProvider | None = None
    pool: PoolProvider | None = None
    notification: NotificationProvider | None = None


class ProviderRegistry:
    """Explicit provider bundles; no provider is shared implicitly between houses."""

    def __init__(self):
        self._providers = {}
        self._lock = RLock()

    def register(self, providers):
        with self._lock:
            self._providers[providers.house_id] = providers

    def get(self, house_id):
        with self._lock:
            return self._providers.get(str(house_id or "").strip().lower())

    def require(self, house_id):
        providers = self.get(house_id)
        if providers is None:
            raise ProviderNotConfiguredError(f"No providers configured for house: {house_id}")
        return providers
