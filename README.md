# IB receive ticks

Connects to Interactive Brokers (TWS/Gateway) and downloads **1-second bars** of the stocks (`STK`) and futures
(`FUT`) listed in a YAML file into SQLite, together with pre-computed 1 min / 15 min / 30 min / 1 hour / 1 day compressions.
Instruments are processed one after another.

```bash
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt

python main.py                                      # update all listed instruments that already have data
python main.py --only-new-instrument --days 7       # initial load of listed instruments without data yet
python main.py --config other.yaml --port 4001 --client-id 42
```

| option | meaning |
|---|---|
| *(none)* | update every listed instrument from its newest stored second up to now; instruments without data are skipped |
| `--only-new-instrument --days N` | load only the listed instruments that have no data yet, the last N days; existing ones are left alone |
| `--config FILE` | YAML file (default `config.yaml`) |
| `--wait-to-reconnect MIN` | update mode only: minutes to wait before reconnecting after a lost IB connection (default 5); with `--only-new-instrument` a lost connection just exits with code 1 |
| `--log-file FILE`, `--log-level LEVEL` | log to stderr with timestamps, also appended to FILE (default `ib-receive-ticks.log`, `--log-file ""` disables it); `ib_async`'s own log records go to FILE only, not to the console (level DEBUG/INFO/WARNING/ERROR, default INFO) |
| `--host`, `--port`, `--client-id` | override `connection:` of the YAML (defaults: 127.0.0.1, 7496, random 2000-10000) |

`--days` is only valid with `--only-new-instrument`, and `--only-new-instrument` requires `--days`.

## config.yaml

See `config.yaml` for the commented example: `connection`, `settings` (timezone, pace, data directory,
what_to_show, all_hours) and the `instruments` list (`symbol`, `sec_type`, `exchange`, `currency`, `expiry`,
`trading_class`, `name`, plus per-instrument `what_to_show` / `all_hours`). Unknown keys are rejected.
For futures `exchange` and `currency` are required; some symbols (e.g. `DAX`) have several trading classes and must
be disambiguated with `trading_class` — see [Multiple trading classes](#multiple-trading-classes). `name` sets the
folder/file symbol, e.g. `FDXM` for the mini DAX (see [Folder structure](#folder-structure)).
Futures use the front month unless `expiry: YYYYMM` is set; an update keeps following the front month, so pin
`expiry` to keep updating an older contract after a roll.

## Multiple trading classes

A single IB symbol can carry several trading classes; EUREX `DAX` has `FDAX` (standard), `FDXM` (mini) and
`FDXS` (small). Because IB returns contract details for every class of the symbol, a bare `symbol: DAX` is
ambiguous: `resolve_contract` reports

```
DAX has several trading classes, set trading_class to one of: FDAX, FDXM, FDXS
```

and the instrument is skipped (counted as failed). Disambiguate by picking a class with `trading_class` (the
standard DAX is `FDAX`) and, for the mini, using its own symbol `FDXM` — one entry per contract to download:

```yaml
instruments:
  - symbol: DAX
    sec_type: FUT
    exchange: EUREX
    currency: EUR
    trading_class: FDAX          # standard DAX -> data/DAX/DAX-YYYYMM.sqlite
  - symbol: FDXM
    sec_type: FUT
    exchange: EUREX
    currency: EUR
    trading_class: FDXM
    name: FDXM                  # store the mini under data/FDXM/ instead of data/DAX/
    # expiry: 202609            # pin a contract month; front month if unset
```

Each entry is a separate instrument and a separate `sources` row, even when two share the same folder. `name`
sets the folder/file symbol (see [Folder structure](#folder-structure)), so the mini DAX above lands in
`data/FDXM/FDXM-202609.sqlite` rather than mixing with the standard contract.

## Fetching

IB returns at most 1800 s of 1-second bars per request and allows 60 historical requests per 10 minutes, so
`settings.pace` (default 10 s) is kept between all requests, also across instruments. A week of a US stock is ~65
requests (~11 min), a 21-hour DAX day ~42 requests. Every window is committed on arrival, so a run can be
interrupted and repeated. IB's 1-second history for futures can trail real time by a few minutes up to ~15 min.

## Folder structure

Each contract gets its own SQLite file under `settings.data` (default `data/`), in one directory per
instrument and one file per expiry:

```text
data/
  AAPL/
    AAPL.sqlite                 # STK: a single file, no expiry in the name
  DAX/
    DAX-202612.sqlite           # FUT: one file per contract month, YYYYMM
    DAX-202609.sqlite
  FDXM/
    FDXM-202609.sqlite          # mini DAX: folder set via `name: FDXM`
```

- **Folder** — the instrument's `name` if set, otherwise the IB `symbol`, upper-cased
  (`data/AAPL`, `data/DAX`, `data/FDXM`).
- **File name**
  - **STK** (and any contract without an expiry): `<SYMBOL>.sqlite` — a single file that accumulates
    history, e.g. `data/AAPL/AAPL.sqlite`.
  - **FUT**: `<SYMBOL>-<YYYYMM>.sqlite` — one file per contract month, since a symbol rolls to a new front
    month on each roll, e.g. `data/DAX/DAX-202612.sqlite`.

A file may hold several *series* (rows in `sources`, below) that share the folder/expiry but differ in
`what_to_show` or `all_hours`; each is a separate source in the same file.

## Database

The schema is derived from `rtt-convert/rtt_to_sqlite.py` with OHLCV instead of `price`:

| table | content |
|---|---|
| `sources` | one row per series, keyed by `source_id` (referenced by `ticks.source` and the `compression_*` tables); `detail` is a JSON object with `symbol`, `exchange`, `instrument_type`, `expiry`, `contract-id` and `imported_at` (last update, UTC ISO 8601) |
| `ticks` | 1-second bars: `source, ts_no_tz, open, high, low, close, volume` |
| `compression_M1`, `compression_M15`, `compression_M30`, `compression_H1`, `compression_D1` | compressions of `ticks`, same columns, keyed by `(source, ts_no_tz)` |

`ts_no_tz` is the bar start as wall-clock time in `settings.timezone`, epoch seconds as if UTC. Buckets are aligned
to the wall clock, so with a US session opening at 15:30 the first hourly bar (15:00) holds only 30 minutes.

## Concurrent access

The databases are written in SQLite WAL mode (set on every write, stored in the file), so other processes can read
them while a download is running; readers see a consistent snapshot and never block the writer. WAL adds
`<file>.sqlite-wal` and `-shm` next to each file, which belong to the database: keep them together when copying, and
open it from a local disk (WAL does not work on network file systems). A reader normally also needs write access
to the directory (for the `-shm` file); read-only access works while the `-wal`/`-shm` files exist (SQLite 3.22+).
Only one process may write at a time; a second writer waits
up to 30 s.
