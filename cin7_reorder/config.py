"""Configuration loading.

Secrets come from the environment. Everything else comes from config.yaml.
The two are kept strictly separate so that the config file stays committable.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml

DEFAULT_BASE_URL = "https://inventory.dearsystems.com/ExternalApi/v2"


class ConfigError(RuntimeError):
    """Configuration is missing or unusable."""


@dataclass(frozen=True)
class Credentials:
    account_id: str
    app_key: str
    base_url: str = DEFAULT_BASE_URL

    @classmethod
    def from_env(cls) -> "Credentials":
        account_id = os.environ.get("CIN7_ACCOUNT_ID", "").strip()
        app_key = os.environ.get("CIN7_APP_KEY", "").strip()

        missing = [
            name
            for name, value in (
                ("CIN7_ACCOUNT_ID", account_id),
                ("CIN7_APP_KEY", app_key),
            )
            if not value
        ]
        if missing:
            raise ConfigError(
                "Missing required environment variable(s): "
                + ", ".join(missing)
                + ". Copy .env.example to .env and fill it in, or set them as CI secrets."
            )

        return cls(
            account_id=account_id,
            app_key=app_key,
            base_url=os.environ.get("CIN7_BASE_URL", DEFAULT_BASE_URL).rstrip("/"),
        )


@dataclass(frozen=True)
class SupplierConfig:
    attribute_name: str = "Auto Reorder"
    truthy_values: tuple[str, ...] = ("yes", "true", "y", "1", "on", "enabled")
    pin: tuple[str, ...] = ()

    def is_opted_in(self, attribute_value: Any) -> bool:
        if attribute_value is None:
            return False
        if isinstance(attribute_value, bool):
            return attribute_value
        return str(attribute_value).strip().lower() in {
            v.lower() for v in self.truthy_values
        }


@dataclass(frozen=True)
class SafetyConfig:
    max_line_quantity: Optional[float] = 500.0
    max_reorder_quantity_multiple: Optional[float] = 5.0
    max_total_lines: Optional[int] = 400


@dataclass(frozen=True)
class ApiConfig:
    page_size: int = 500
    daily_call_budget: int = 4000
    timeout_seconds: float = 60.0


@dataclass(frozen=True)
class Config:
    suppliers: SupplierConfig = field(default_factory=SupplierConfig)
    locations_include: tuple[str, ...] = ()
    safety: SafetyConfig = field(default_factory=SafetyConfig)
    moq: dict[str, float] = field(default_factory=dict)
    api: ApiConfig = field(default_factory=ApiConfig)

    def includes_location(self, location: str) -> bool:
        if not self.locations_include:
            return True
        return location in self.locations_include

    @classmethod
    def load(cls, path: Optional[Path] = None) -> "Config":
        if path is None:
            path = Path(__file__).resolve().parent.parent / "config.yaml"
        if not path.exists():
            return cls()

        with path.open("r", encoding="utf-8") as handle:
            data = yaml.safe_load(handle) or {}
        if not isinstance(data, dict):
            raise ConfigError(f"{path} must contain a YAML mapping at the top level.")

        return cls.from_dict(data)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Config":
        suppliers_raw = data.get("suppliers") or {}
        safety_raw = data.get("safety") or {}
        api_raw = data.get("api") or {}
        locations_raw = (data.get("locations") or {}).get("include") or []
        moq_raw = data.get("moq") or {}

        suppliers = SupplierConfig(
            attribute_name=suppliers_raw.get("attribute_name", "Auto Reorder"),
            truthy_values=tuple(
                suppliers_raw.get("truthy_values")
                or ("yes", "true", "y", "1", "on", "enabled")
            ),
            pin=tuple(str(s) for s in (suppliers_raw.get("pin") or [])),
        )

        safety = SafetyConfig(
            max_line_quantity=_optional_float(safety_raw.get("max_line_quantity", 500)),
            max_reorder_quantity_multiple=_optional_float(
                safety_raw.get("max_reorder_quantity_multiple", 5)
            ),
            max_total_lines=_optional_int(safety_raw.get("max_total_lines", 400)),
        )

        api = ApiConfig(
            page_size=int(api_raw.get("page_size", 500)),
            daily_call_budget=int(api_raw.get("daily_call_budget", 4000)),
            timeout_seconds=float(api_raw.get("timeout_seconds", 60)),
        )

        return cls(
            suppliers=suppliers,
            locations_include=tuple(str(loc) for loc in locations_raw),
            safety=safety,
            moq={str(k): float(v) for k, v in moq_raw.items()},
            api=api,
        )


def _optional_float(value: Any) -> Optional[float]:
    return None if value is None else float(value)


def _optional_int(value: Any) -> Optional[int]:
    return None if value is None else int(value)
