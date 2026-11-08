"""YAML configuration of ib-receive-ticks (see config.yaml)."""

from dataclasses import dataclass, field, fields
from pathlib import Path

import yaml


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
class Config:
    connection: Connection = field(default_factory=Connection)
    settings: Settings = field(default_factory=Settings)
    instruments: list[Instrument] = field(default_factory=list)


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


def load_config(path) -> Config:
    """Read and validate the YAML file at `path`; raises ValueError with a readable message."""
    path = Path(path)
    if not path.exists():
        raise ValueError(f"config file {path} not found")
    raw = yaml.safe_load(path.read_text()) or {}
    unknown = set(raw) - {"connection", "settings", "instruments"}
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
        _build(Connection, raw.get("connection"), "connection"), settings, instruments
    )
