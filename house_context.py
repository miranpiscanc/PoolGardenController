import json
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping


@dataclass(frozen=True)
class HouseContext:
    """Immutable, non-operational metadata describing one managed house."""

    id: str
    display_name: str
    enabled: bool
    route: str
    description: str
    status_label: str
    enabled_modules: tuple[str, ...]
    configured_devices: Mapping[str, tuple[str, ...]]
    providers: Mapping[str, str]

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]):
        house_id = str(raw.get("id") or "").strip().lower()
        route = str(raw.get("route") or "").strip()
        if not house_id or not route.startswith("/") or route.startswith("//"):
            raise ValueError("Invalid house metadata")

        modules = raw.get("modules") if isinstance(raw.get("modules"), list) else []
        devices_raw = raw.get("configured_devices") if isinstance(raw.get("configured_devices"), dict) else {}
        providers_raw = raw.get("providers") if isinstance(raw.get("providers"), dict) else {}
        devices = {
            str(module): tuple(str(device) for device in values if str(device).strip())
            for module, values in devices_raw.items()
            if isinstance(values, list)
        }
        providers = {
            str(module): str(provider)
            for module, provider in providers_raw.items()
            if str(module).strip() and str(provider).strip()
        }
        return cls(
            id=house_id,
            display_name=str(raw.get("display_name") or house_id.title()),
            enabled=bool(raw.get("enabled", False)),
            route=route,
            description=str(raw.get("description") or ""),
            status_label=str(raw.get("status_label") or "Configuration pending"),
            enabled_modules=tuple(str(module) for module in modules if str(module).strip()),
            configured_devices=MappingProxyType(devices),
            providers=MappingProxyType(providers),
        )

    def module_enabled(self, module_name):
        normalized = str(module_name or "").strip().lower()
        return self.enabled and any(module.lower() == normalized for module in self.enabled_modules)

    def provider_id(self, capability):
        return self.providers.get(str(capability or "").strip().lower())

    def to_public_dict(self):
        return {
            "id": self.id,
            "display_name": self.display_name,
            "enabled": self.enabled,
            "route": self.route,
            "description": self.description,
            "status_label": self.status_label,
            "modules": list(self.enabled_modules),
            "configured_devices": {key: list(value) for key, value in self.configured_devices.items()},
            "providers": dict(self.providers),
        }


class HouseRegistry:
    """Configuration-backed registry with a safe metadata-only fallback."""

    def __init__(self, config_path, defaults):
        self.config_path = Path(config_path)
        self.defaults = tuple(dict(item) for item in defaults)

    def all(self):
        try:
            document = json.loads(self.config_path.read_text(encoding="utf-8"))
            configured = document.get("houses", [])
        except (OSError, ValueError, AttributeError):
            configured = self.defaults

        contexts = []
        for raw in configured:
            if not isinstance(raw, dict):
                continue
            try:
                contexts.append(HouseContext.from_mapping(raw))
            except ValueError:
                continue
        if contexts:
            return tuple(contexts)
        return tuple(HouseContext.from_mapping(raw) for raw in self.defaults)

    def get(self, house_id):
        normalized = str(house_id or "").strip().lower()
        return next((house for house in self.all() if house.id == normalized), None)

    def default(self, house_id="cesclans"):
        selected = self.get(house_id)
        if selected is not None:
            return selected
        houses = self.all()
        return next((house for house in houses if house.enabled), houses[0] if houses else None)
