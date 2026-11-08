"""Store IB 1-second bars in a per-contract SQLite file, following rtt-convert/rtt_to_sqlite.py.

One database per contract, same path scheme as the RTT converter:
    stocks (STK) and contracts without an expiry:  <data-dir>/<SYMBOL>/<SYMBOL>.sqlite         (data/AAPL/AAPL.sqlite)
    futures:                                       <data-dir>/<SYMBOL>/<SYMBOL>-<YYYYMM>.sqlite (data/DAX/DAX-202612.sqlite)
`ticks` holds the base timeframe of 1 second (OHLCV); `compression_M1` ... `compression_H1` are pre-computed
compressions of it. Each (symbol, what-to-show, session) series is one row in `sources`.
"""

import calendar
import json
import sqlite3
from datetime import datetime, timezone
from itertools import groupby
from pathlib import Path
from zoneinfo import ZoneInfo

SOURCES_DDL = """CREATE TABLE IF NOT EXISTS sources (
    source_id INTEGER PRIMARY KEY,  -- referenced by ticks.source
    detail    TEXT NOT NULL CHECK (json_valid(detail)))  -- JSON object: symbol, exchange, instrument_type, expiry,
                                                         -- imported_at (last update time, UTC ISO 8601)"""

# base timeframe: 1 second
TICKS_DDL = """CREATE TABLE IF NOT EXISTS ticks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        source INTEGER NOT NULL,
        ts_no_tz INTEGER NOT NULL,  -- bar start, wall-clock time without timezone; epoch seconds as if UTC
        open REAL NOT NULL,
        high REAL NOT NULL,
        low REAL NOT NULL,
        close REAL NOT NULL,
        volume INTEGER)  -- NULL if unknown"""

# compressions of `ticks`: table name -> bucket length in seconds (buckets are aligned to the wall clock)
TIMEFRAMES = {
    "compression_M1": 60,
    "compression_M15": 15 * 60,
    "compression_M30": 30 * 60,
    "compression_H1": 60 * 60,
    "compression_D1": 24 * 60 * 60,
}

COMPRESSED_DDL = """CREATE TABLE IF NOT EXISTS {table} (
        source INTEGER NOT NULL,
        ts_no_tz INTEGER NOT NULL,  -- bucket start, same encoding as ticks.ts_no_tz
        open REAL NOT NULL,
        high REAL NOT NULL,
        low REAL NOT NULL,
        close REAL NOT NULL,
        volume INTEGER,  -- NULL if all underlying volumes are unknown
        PRIMARY KEY (source, ts_no_tz)) WITHOUT ROWID"""


def default_out_path(
    data_root, symbol: str, instrument_type: str, expiry: str | None
) -> Path:
    """Per-contract SQLite path: <data-root>/<SYMBOL>/<file>.sqlite (STK or no expiry -> no expiry in the name)."""
    fname = (
        f"{symbol}.sqlite"
        if (instrument_type.upper() == "STK" or expiry is None)
        else f"{symbol}-{expiry}.sqlite"
    )
    return Path(data_root) / symbol / fname


def to_epoch(dt: datetime, tz: ZoneInfo) -> int:
    """Wall-clock time of `dt` in `tz` as epoch seconds as if UTC (the ts_no_tz encoding)."""
    return calendar.timegm(dt.astimezone(tz).replace(tzinfo=None).timetuple())


def from_epoch(ts_no_tz: int, tz: ZoneInfo) -> datetime:
    """Inverse of to_epoch: a tz-aware datetime."""
    return datetime.fromtimestamp(ts_no_tz, timezone.utc).replace(tzinfo=tz)


def get_source(con, symbol: str, sec_type: str, expiry: str | None):
    q = "SELECT source_id FROM sources WHERE json_extract(detail,'$.symbol')=? AND json_extract(detail,'$.instrument_type')=?"
    if sec_type == "FUT":
        q += " AND json_extract(detail,'$.expiry')=?"
        param = (
            symbol,
            sec_type,
            expiry,
        )
    else:
        param = (
            symbol,
            sec_type,
        )
    try:
        row = con.execute(q, param).fetchone()
    except sqlite3.OperationalError:  # file without our tables
        row = None
    return row


def last_ts(db_path, symbol: str, sec_type: str, expiry: str | None) -> int | None:
    """Newest stored 1-second ts_no_tz of `series`, or None. Does not create the DB."""
    db_path = Path(db_path)
    if not db_path.exists():
        return None
    con = sqlite3.connect(db_path)
    source_id = get_source(con, symbol, sec_type, expiry)
    if source_id:
        try:
            q = "SELECT MAX(ts_no_tz) FROM ticks WHERE source=?"
            row = con.execute(q, (source_id[0],)).fetchone()
        except sqlite3.OperationalError:  # file without our tables
            row = None
    else:
        row = None
    con.close()
    return row[0] if row else None


def compress(con: sqlite3.Connection, source: int, from_ts: int) -> None:
    """Recompute all compression tables for `source` from the bucket containing `from_ts` onwards."""
    for table, secs in TIMEFRAMES.items():
        start = from_ts - from_ts % secs
        rows = con.execute(
            "SELECT ts_no_tz, open, high, low, close, volume FROM ticks "
            "WHERE source=? AND ts_no_tz>=? ORDER BY ts_no_tz",
            (source, start),
        )
        out = []
        for bucket, grp in groupby(rows, key=lambda r: r[0] - r[0] % secs):
            g = list(grp)
            vols = [r[5] for r in g if r[5] is not None]
            out.append(
                (
                    source,
                    bucket,
                    g[0][1],
                    max(r[2] for r in g),
                    min(r[3] for r in g),
                    g[-1][4],
                    sum(vols) if vols else None,
                )
            )
        con.execute(
            f"DELETE FROM {table} WHERE source=? AND ts_no_tz>=?", (source, start)
        )
        con.executemany(f"INSERT INTO {table} VALUES (?,?,?,?,?,?,?)", out)


def write_bars(
    bars,
    db_path,
    symbol: str,
    exchange: str | None,
    instrument_type: str,
    expiry: str | None,
    contract_id: int,
    tz: ZoneInfo,
):
    """Merge 1-second `bars` into the SQLite at `db_path` as source `series`; returns the stored tick count.

    Stored ticks between the first and last bar are replaced, everything else is kept, so overlapping or
    repeated fetches are idempotent and history accumulates. The compression tables are refreshed from the
    first affected bucket, in the same transaction.
    """
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    rows = [
        (
            to_epoch(b.date, tz),
            b.open,
            b.high,
            b.low,
            b.close,
            None if b.volume < 0 else int(round(b.volume)),
        )
        for b in bars
    ]
    con = sqlite3.connect(db_path, timeout=30)
    # WAL lets other processes read while we write (readers never block the writer and vice versa); the mode is
    # stored in the file, so it also applies to readers and to later runs
    con.execute("PRAGMA journal_mode=WAL")
    cols = [r[1] for r in con.execute("PRAGMA table_info(ticks)")]
    if cols and "open" not in cols:
        con.close()
        raise RuntimeError(
            f"{db_path} has an old `ticks` schema (price instead of OHLCV); move or delete it"
        )
    src_cols = [r[1] for r in con.execute("PRAGMA table_info(sources)")]
    if src_cols and src_cols != ["source_id", "detail"]:
        con.close()
        raise RuntimeError(
            f"{db_path} has an old `sources` schema (no JSON `detail` column); move or delete it"
        )
    con.execute(TICKS_DDL)
    con.execute(SOURCES_DDL)
    con.execute("CREATE INDEX IF NOT EXISTS ix_ticks_src_ts ON ticks(ts_no_tz)")
    for table in TIMEFRAMES:
        con.execute(COMPRESSED_DDL.format(table=table))
    existing = get_source(con, symbol, instrument_type, expiry)
    source = (
        existing[0]
        if existing
        else con.execute("SELECT COALESCE(MAX(source_id),0)+1 FROM sources").fetchone()[
            0
        ]
    )
    with con:  # single transaction -> rollback on error
        if rows:
            rows.sort()
            con.execute(
                "DELETE FROM ticks WHERE source=? AND ts_no_tz BETWEEN ? AND ?",
                (source, rows[0][0], rows[-1][0]),
            )
            con.executemany(
                "INSERT INTO ticks(source,ts_no_tz,open,high,low,close,volume) VALUES (?,?,?,?,?,?,?)",
                ((source, *r) for r in rows),
            )
            compress(con, source, rows[0][0])
        now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        detail_json = {
            "symbol": symbol,
            "exchange": exchange,
            "instrument_type": instrument_type,
            "expiry": expiry,
            "contract-id": contract_id,
            "imported_at": now,
        }
        detail = json.dumps(detail_json)
        con.execute(
            "INSERT OR REPLACE INTO sources (source_id, detail) VALUES (?,?)",
            (source, detail),
        )
    con.close()
