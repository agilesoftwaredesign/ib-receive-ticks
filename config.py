"""YAML configuration of ib-receive-ticks (see config.yaml)."""

import os
from dataclasses import dataclass, field, fields
from datetime import datetime, time
from pathlib import Path

import yaml

ENV_SMTP_PASSWORD = "IB_RECEIVE_TICKS_SMTP_PASSWORD"


@dataclass
class Connection:
    host: str = "127.0.0.1"
    port: int = 7496
    client_id: int | None = None  # random if not set


@dataclass
class Settings:
    timezone: str = (
        "Europe/Berlin"  # timezone of the stored wall-clock timestamps ts_no_tz
    )
    pace: float = (
        10.0  # seconds between IB requests; IB allows 60 historical requests per 10 min
    )
    data: str = "data"  # root directory for the per-contract SQLite files
    what_to_show: str = "TRADES"  # TRADES, MIDPOINT, BID, ASK, ...
    all_hours: bool = False  # include data outside regular trading hours


@dataclass
class Instrument:
    symbol: str  # IB symbol, e.g. AAPL or DAX
    skip: bool | None = False
    sec_type: str = "STK"  # STK or FUT
    exchange: str | None = None  # STK: default SMART; FUT: required, e.g. EUREX
    currency: str | None = None  # STK: default USD; FUT: required, e.g. EUR
    expiry: str | None = None  # FUT: contract month YYYYMM (default: front month)
    trading_class: str | None = (
        None  # FUT: needed if the symbol has several, e.g. FDAX/FDXM/FDXS for DAX
    )
    name: str | None = (
        None  # symbol for folder and file name if it differs from the IB symbol (e.g. FDXM)
    )
    what_to_show: str = "TRADES"  # taken from `settings` unless set here
    all_hours: bool = False  # taken from `settings` unless set here


@dataclass
class Smtp:
    host: str = ""
    port: int = 587
    user: str | None = None  # no login if not set
    password: str | None = None  # the environment variable ENV_SMTP_PASSWORD takes precedence
    security: str = "starttls"  # starttls, ssl or none


@dataclass
class Alerts:
    smtp: Smtp = field(default_factory=Smtp)
    email_to: list[str] = field(default_factory=list)  # one address or a list
    email_from: str = ""
    email_subject: str = "IB receive ticks: TWS is down"
    down_after_min: float = 30  # alert once if the connection is down this long
    daily_check_time: time = time(1, 0)  # alert once if still down then, HH:MM in settings.timezone


@dataclass
class Config:
    connection: Connection = field(default_factory=Connection)
    settings: Settings = field(default_factory=Settings)
    instruments: list[Instrument] = field(default_factory=list)
    alerts: Alerts | None = None  # no TWS-is-up check if the section is missing


def _build(cls, raw, where: str, **defaults):
    """Instantiate dataclass `cls` from the mapping `raw`, rejecting unknown keys (typos)."""
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ValueError(f"{where}: expected a mapping, got {type(raw).__name__}")
    unknown = set(raw) - {f.name for f in fields(cls)}
    if unknown:
        raise ValueError(f"{where}: unknown key(s): {', '.join(sorted(unknown))}")
    return cls(**{**defaults, **raw})


def _load_alerts(raw) -> Alerts:
    """Build and validate the `alerts:` section; the SMTP password may come from the environment."""
    if raw is not None and not isinstance(raw, dict):
        raise ValueError(f"alerts: expected a mapping, got {type(raw).__name__}")
    raw = dict(raw or {})
    smtp = _build(Smtp, raw.pop("smtp", None), "alerts.smtp")
    alerts = _build(Alerts, raw, "alerts", smtp=smtp)
    if not smtp.host:
        raise ValueError("alerts.smtp.host is required")
    smtp.password = os.environ.get(ENV_SMTP_PASSWORD) or smtp.password
    if smtp.user and not smtp.password:
        raise ValueError(
            f"alerts.smtp: user is set but no password (set smtp.password or the environment variable {ENV_SMTP_PASSWORD})"
        )
    if smtp.security not in ("starttls", "ssl", "none"):
        raise ValueError(
            f"alerts.smtp.security must be starttls, ssl or none, not {smtp.security!r}"
        )
    if isinstance(alerts.email_to, str):
        alerts.email_to = [alerts.email_to]
    if not alerts.email_to or not all(
        isinstance(a, str) and a for a in alerts.email_to
    ):
        raise ValueError("alerts.email_to is required (an address or a list of them)")
    if not alerts.email_from:
        raise ValueError("alerts.email_from is required")
    if (
        isinstance(alerts.down_after_min, bool)
        or not isinstance(alerts.down_after_min, (int, float))
        or alerts.down_after_min <= 0
    ):
        raise ValueError("alerts.down_after_min must be a positive number of minutes")
    if isinstance(alerts.daily_check_time, str):
        try:
            alerts.daily_check_time = datetime.strptime(
                alerts.daily_check_time, "%H:%M"
            ).time()
        except ValueError:
            raise ValueError(
                f"alerts.daily_check_time must be HH:MM, not {alerts.daily_check_time!r}"
            ) from None
    elif not isinstance(alerts.daily_check_time, time):
        # an unquoted 1:00 is read by YAML as a number (sexagesimal), so the time has to be quoted
        raise ValueError('alerts.daily_check_time must be a quoted HH:MM string, e.g. "01:00"')
    return alerts


def load_config(path) -> Config:
    """Read and validate the YAML file at `path`; raises ValueError with a readable message."""
    path = Path(path)
    if not path.exists():
        raise ValueError(f"config file {path} not found")
    raw = yaml.safe_load(path.read_text()) or {}
    unknown = set(raw) - {"connection", "settings", "instruments", "alerts"}
    if unknown:
        raise ValueError(
            f"{path}: unknown top-level key(s): {', '.join(sorted(unknown))}"
        )
    settings = _build(Settings, raw.get("settings"), "settings")
    instruments = []
    for i, item in enumerate(raw.get("instruments") or [], 1):
        where = f"instruments[{i}]"
        inst = _build(
            Instrument,
            item,
            where,
            what_to_show=settings.what_to_show,
            all_hours=settings.all_hours,
        )
        if not inst.symbol:
            raise ValueError(f"{where}: symbol is required")
        inst.symbol = str(inst.symbol)
        inst.expiry = (
            None if inst.expiry is None else str(inst.expiry)
        )  # YAML reads 202612 as a number
        if inst.sec_type not in ("STK", "FUT"):
            raise ValueError(
                f"{where} ({inst.symbol}): sec_type must be STK or FUT, not {inst.sec_type!r}"
            )
        if inst.sec_type == "FUT" and not (inst.exchange and inst.currency):
            raise ValueError(
                f"{where} ({inst.symbol}): exchange and currency are required for FUT"
            )
        instruments.append(inst)
    if not instruments:
        raise ValueError(f"{path}: no instruments listed")
    return Config(
        _build(Connection, raw.get("connection"), "connection"),
        settings,
        instruments,
        _load_alerts(raw["alerts"]) if "alerts" in raw else None,
    )
