"""Configuration loading.

Secrets come from a ``.env`` file or the environment. Everything else comes
from config.yaml. The two are kept strictly separate so that the config file
stays committable and the secrets never are.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml

DEFAULT_BASE_URL = "https://inventory.dearsystems.com/ExternalApi/v2"

#: Where to look for a .env file, in order. The package directory first so
#: the tool works from any working directory, then the current directory.
ENV_FILE_LOCATIONS = (
    Path(__file__).resolve().parent.parent / ".env",
    Path.cwd() / ".env",
)


class ConfigError(RuntimeError):
    """Configuration is missing or unusable."""


def load_env_file(path: Optional[Path] = None) -> dict[str, str]:
    """Read a ``.env`` file into a dict, without touching os.environ.

    Deliberately small and dependency-free. Handles the three things that
    actually trip people up when pasting credentials in: an ``export``
    prefix copied from a shell snippet, surrounding quotes, and trailing
    whitespace.

    Real environment variables always win over the file, so CI secrets are
    never shadowed by a stray .env left in a checkout.
    """
    candidates = [path] if path is not None else list(ENV_FILE_LOCATIONS)

    values: dict[str, str] = {}
    for candidate in candidates:
        if candidate is None or not candidate.is_file():
            continue
        try:
            text = candidate.read_text(encoding="utf-8")
        except OSError:
            continue

        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            if line.startswith("export "):
                line = line[len("export ") :]
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            if key:
                values.setdefault(key, value)

        # First file found wins; don't merge across locations.
        break

    return values


@dataclass(frozen=True)
class Credentials:
    account_id: str
    app_key: str
    base_url: str = DEFAULT_BASE_URL

    @classmethod
    def from_env(cls, env_file: Optional[Path] = None) -> "Credentials":
        from_file = load_env_file(env_file)

        def lookup(name: str) -> str:
            return (os.environ.get(name) or from_file.get(name, "")).strip()

        account_id = lookup("CIN7_ACCOUNT_ID")
        app_key = lookup("CIN7_APP_KEY")

        missing = [
            name
            for name, value in (
                ("CIN7_ACCOUNT_ID", account_id),
                ("CIN7_APP_KEY", app_key),
            )
            if not value
        ]
        if missing:
            # dict.fromkeys keeps order while dropping the duplicate you get
            # when the working directory is the tool directory.
            seen = dict.fromkeys(str(p) for p in ENV_FILE_LOCATIONS)
            searched = "\n".join(f"    {p}" for p in seen)
            raise ConfigError(
                "Missing credentials: "
                + ", ".join(missing)
                + ".\n\nProvide them either way:\n\n"
                "  1. In a .env file — copy .env.example to .env and fill it in.\n"
                "     Looked for one at:\n"
                f"{searched}\n\n"
                "  2. As environment variables:\n"
                "       export CIN7_ACCOUNT_ID='...'\n"
                "       export CIN7_APP_KEY='...'\n\n"
                "Both are created on the Cin7 API setup page at\n"
                "https://inventory.dearsystems.com/ExternalAPI"
            )

        base_url = lookup("CIN7_BASE_URL") or DEFAULT_BASE_URL

        return cls(
            account_id=account_id,
            app_key=app_key,
            base_url=base_url.rstrip("/"),
        )


@dataclass(frozen=True)
class SupplierConfig:
    #: Human-readable attribute label, used only by nested attribute shapes.
    attribute_name: str = "Auto Reorder"
    #: The slot Cin7 actually stores it in, e.g. "AdditionalAttribute1".
    #: Cin7 exposes supplier attributes as ten opaque numbered fields and
    #: keeps the labels in the attribute set definition, so the slot has to
    #: be named directly. Run `probe` to see which slots hold values.
    attribute_field: Optional[str] = None
    truthy_values: tuple[str, ...] = ("yes", "true", "y", "1", "on", "enabled")
    pin: tuple[str, ...] = ()

    @property
    def lookup_key(self) -> str:
        """What to actually read off the supplier record."""
        return self.attribute_field or self.attribute_name

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
    #: How many individual purchase orders one run may read. Purchases are
    #: the only thing fetched one at a time, at one call each — two for
    #: Advanced purchases — so this is what stands between a busy account and
    #: the daily quota. Reaching it understates inbound stock, so `plan` says
    #: so loudly and `apply` refuses to write.
    max_purchase_details: int = 250


@dataclass(frozen=True)
class Config:
    suppliers: SupplierConfig = field(default_factory=SupplierConfig)
    #: Allowlist. Empty means every location.
    locations_include: tuple[str, ...] = ()
    #: Denylist, applied after the allowlist and winning over it.
    #:
    #: Worth having both. An allowlist silently ignores a warehouse opened
    #: later — the run would simply never order for it, and nothing in the
    #: report would say so. A denylist lets a new warehouse in by default,
    #: where the worst case is a draft purchase order somebody reads and
    #: deletes. Visible mistakes beat invisible ones.
    locations_exclude: tuple[str, ...] = ()
    safety: SafetyConfig = field(default_factory=SafetyConfig)
    moq: dict[str, float] = field(default_factory=dict)
    api: ApiConfig = field(default_factory=ApiConfig)

    def includes_location(self, location: str) -> bool:
        """Whether this run should order for ``location``.

        Excluding a location excludes its per-location reorder points with
        it: :func:`cin7_reorder.reorderpoints.resolve` only ever matches a
        location-level minimum against the location being evaluated, so an
        excluded warehouse's levels are never consulted for another one.
        """
        if _matches_location(location, self.locations_exclude):
            return False
        if not self.locations_include:
            return True
        return _matches_location(location, self.locations_include)

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
        locations_cfg = data.get("locations") or {}
        locations_raw = locations_cfg.get("include") or []
        locations_excluded = locations_cfg.get("exclude") or []
        moq_raw = data.get("moq") or {}

        suppliers = SupplierConfig(
            attribute_name=suppliers_raw.get("attribute_name", "Auto Reorder"),
            attribute_field=suppliers_raw.get("attribute_field") or None,
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
            max_purchase_details=int(api_raw.get("max_purchase_details", 250)),
        )

        return cls(
            suppliers=suppliers,
            locations_include=tuple(str(loc) for loc in locations_raw),
            locations_exclude=tuple(str(loc) for loc in locations_excluded),
            safety=safety,
            moq={str(k): float(v) for k, v in moq_raw.items()},
            api=api,
        )


def _matches_location(location: str, names: tuple[str, ...]) -> bool:
    """Case- and whitespace-insensitive match.

    Warehouse names are typed by hand into config and copied out of Cin7's UI,
    and "vic warehouse" failing to match "VIC Warehouse" would fail silently
    in the worst possible way: the run would order for a warehouse somebody
    had written down that they wanted left alone.
    """
    wanted = location.strip().lower()
    return any(name.strip().lower() == wanted for name in names)


def _optional_float(value: Any) -> Optional[float]:
    return None if value is None else float(value)


def _optional_int(value: Any) -> Optional[int]:
    return None if value is None else int(value)
