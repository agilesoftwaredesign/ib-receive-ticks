import argparse
import random
import sys
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from config import Config, Instrument, load_config
from ib_async import IB, ContractDetails, Future, Stock
from store import default_out_path, from_epoch, last_ts, write_bars

# https://github.com/ib-api-reloaded/ib_async


CHUNK_SECONDS = 1800  # IB allows at most 1800 S of 1-second bars per request
DEFAULT_UPDATE_EVERY_MIN = 15
DEFAULT_WAIT_TO_RECONNECT_MIN = 5
MIN_SECONDS = 60  # IB rejects very short durations (error 321); a longer one only overlaps stored bars


def parse_args():
    p = argparse.ArgumentParser(
        description="Receive 1-second bars of the instruments listed in a YAML file from Interactive Brokers and "
        "store them (plus 1 min/15 min/30 min/1 hour compressions) in SQLite. Without options every "
        "listed instrument that already has data is updated up to now."
    )
    p.add_argument(
        "--config",
        default="config.yaml",
        help="YAML config file (default: config.yaml)",
    )
    p.add_argument("--host", help="IB host, overrides the config file")
    p.add_argument(
        "--port",
        type=int,
        help="IB API port, overrides the config file (default: 7496)",
    )
    p.add_argument(
        "--client-id",
        type=int,
        help="IB client id, overrides the config file (default: random 2000-10000)",
    )
    p.add_argument(
        "--only-new-instrument",
        action="store_true",
        help="Only load listed instruments that have no data yet (the others are left alone); "
        "requires --days",
    )
    p.add_argument(
        "--days",
        type=int,
        help="With --only-new-instrument: how many days back to load",
    )
    p.add_argument(
        "--update-every-min",
        type=float,
        help="Update mode only: minutes to wait after an update before the next one; repeats until the "
        f"script is terminated (default: {DEFAULT_UPDATE_EVERY_MIN})",
    )
    p.add_argument(
        "--wait-to-reconnect",
        type=float,
        help="Update mode only: minutes to wait before reconnecting after the connection to IB was lost "
        f"(default: {DEFAULT_WAIT_TO_RECONNECT_MIN}). With --only-new-instrument a lost connection ends the script",
    )
    args = p.parse_args()
    if args.wait_to_reconnect is not None and args.only_new_instrument:
        p.error(
            "--wait-to-reconnect can only be used in update mode, not with --only-new-instrument"
        )
    if args.wait_to_reconnect is not None and args.wait_to_reconnect < 0:
        p.error("--wait-to-reconnect must not be negative")
    if args.wait_to_reconnect is None:
        args.wait_to_reconnect = DEFAULT_WAIT_TO_RECONNECT_MIN
    if args.update_every_min is not None and args.only_new_instrument:
        p.error(
            "--update-every-min can only be used in update mode, not with --only-new-instrument"
        )
    if args.update_every_min is not None and args.update_every_min < 1:
        p.error("--update-every-min must at least 1 minute")
    if args.update_every_min is None:
        args.update_every_min = DEFAULT_UPDATE_EVERY_MIN
    if args.only_new_instrument and args.days is None:
        p.error("--only-new-instrument requires --days")
    if args.days is not None and not args.only_new_instrument:
        p.error("--days can only be used together with --only-new-instrument")
    if args.days is not None and args.days < 0:
        p.error("--days must not be negative")
    return args


def resolve_contract(ib: IB, inst: Instrument) -> ContractDetails:
    """Return the ContractDetails to fetch; raises ValueError if it is missing or ambiguous."""
    if inst.sec_type == "STK":
        details = ib.reqContractDetails(
            Stock(inst.symbol, inst.exchange or "SMART", inst.currency or "USD")
        )
    else:
        details = ib.reqContractDetails(
            Future(
                inst.symbol,
                inst.expiry or "",
                inst.exchange,
                currency=inst.currency,
                tradingClass=inst.trading_class or "",
                includeExpired=bool(inst.expiry),
            )
        )  # expired only on request
        if inst.expiry:
            details = [
                d
                for d in details
                if d.contract.lastTradeDateOrContractMonth.startswith(inst.expiry)
            ]
        classes = sorted({d.contract.tradingClass for d in details})
        if len(classes) > 1:
            raise ValueError(
                f"{inst.symbol} has several trading classes, set trading_class to one of: "
                f"{', '.join(classes)}"
            )
        details.sort(
            key=lambda d: d.contract.lastTradeDateOrContractMonth
        )  # front month first
    if not details:
        raise ValueError(f"Could not resolve contract for {inst.symbol}")
    return details[0]


def sessions(details, all_hours: bool, days: int) -> list[tuple[datetime, datetime]]:
    """Trading sessions (UTC start, end) of the last `days` days up to today.

    IB lists hours only from today onwards (e.g. '20261104:0930-20261104:1600;20261107:CLOSED'), so the most
    common session of a weekday (of all days for Mon-Fri days not listed, e.g. before today) is the
    template for the past days. Holidays and early closes just yield request windows without data.
    """
    tz = ZoneInfo(details.timeZoneId)
    listed = {}  # weekday of the session start -> Counter of (open, close, days from open to close)
    for part in (details.tradingHours if all_hours else details.liquidHours).split(";"):
        if part and "CLOSED" not in part:
            a, b = (datetime.strptime(x, "%Y%m%d:%H%M") for x in part.split("-"))
            listed.setdefault(a.weekday(), Counter())[
                (a.time(), b.time(), (b.date() - a.date()).days)
            ] += 1
    overall = sum(listed.values(), Counter())
    today = datetime.now(tz).date()
    out = []
    for back in range(days, -1, -1):
        day = today - timedelta(days=back)
        template = listed.get(day.weekday()) or (overall if day.weekday() < 5 else None)
        if template:
            open_t, close_t, day_offset = template.most_common(1)[0][0]
            out.append(
                (
                    datetime.combine(day, open_t, tz).astimezone(timezone.utc),
                    datetime.combine(
                        day + timedelta(days=day_offset), close_t, tz
                    ).astimezone(timezone.utc),
                )
            )
    return out


def chunks(sess, until: datetime, since: datetime | None = None):
    """Split the sessions into request windows of at most CHUNK_SECONDS, within [`since`, `until`]."""
    step = timedelta(seconds=CHUNK_SECONDS)
    for start, end in sess:
        end = min(end, until)
        cur = max(start, since) if since else start
        while cur < end:
            nxt = min(cur + step, end)
            yield cur, nxt
            cur = nxt


class Pacer:
    """Keeps `seconds` between IB historical requests, across all instruments."""

    def __init__(self, ib, seconds: float):
        self.ib, self.seconds, self.first = ib, seconds, True

    def wait(self):
        if not self.first:
            self.ib.sleep(self.seconds)
        self.first = False


def fetch_instrument(
    ib: IB, inst: Instrument, cfg: Config, pacer: Pacer, days: int | None
) -> bool:
    """Download one instrument and store it. Returns False if it was skipped.

    days=None: update an instrument that already has data, from its newest stored second up to now.
    days=N:    initial load of the last N days, only for an instrument that has no data yet.
    Raises ValueError if the contract cannot be resolved.
    """
    tz = ZoneInfo(cfg.settings.timezone)
    details = resolve_contract(ib, inst)
    contract = details.contract
    expiry = (
        contract.lastTradeDateOrContractMonth[:6] if contract.secType == "FUT" else None
    )
    print(
        f"{contract.secType} {contract.symbol} {contract.tradingClass} {expiry or ''} -> conId {contract.conId} "
        f"({contract.primaryExchange or contract.exchange}, {contract.currency})"
    )

    name = (inst.name or contract.symbol).upper()
    out = default_out_path(cfg.settings.data, name, contract.secType, expiry)
    symbol_name = (
        contract.tradingClass
        if contract.secType == "FUT" and contract.tradingClass != contract.symbol
        else contract.symbol
    )
    series = "_".join(
        ["IB", contract.symbol]
        + (
            [contract.tradingClass]
            if contract.secType == "FUT" and contract.tradingClass != contract.symbol
            else []
        )
        + ([expiry] if expiry else [])
        + [inst.what_to_show]
        + (["ETH"] if inst.all_hours else [])
    )

    newest = last_ts(out, symbol_name, contract.secType, expiry)
    done_until = (
        from_epoch(newest, tz).astimezone(timezone.utc) if newest is not None else None
    )
    now = datetime.now(timezone.utc)
    since = None
    if days is None:
        if done_until is None:
            print(
                f"Skipped: no data for {series} in {out} yet; load it with --only-new-instrument --days N"
            )
            return False
        since = done_until
        exchange_tz = ZoneInfo(details.timeZoneId)
        # +1: an overnight session may have started on the day before
        days = (
            now.astimezone(exchange_tz).date() - since.astimezone(exchange_tz).date()
        ).days + 1
        print(
            f"Updating from {since.astimezone(tz):%Y-%m-%d %H:%M:%S} ({cfg.settings.timezone}) to now (db-file: {out})"
        )
    elif done_until is not None:
        print(
            f"Skipped: {series} already has data in {out} (newest "
            f"{done_until.astimezone(tz):%Y-%m-%d %H:%M:%S}); run without options to update it"
        )
        return False

    windows = list(chunks(sessions(details, inst.all_hours, days), now, since))
    # the last bar of a window starts 1 s before its end
    todo = [
        w
        for w in windows
        if done_until is None or w[1] - timedelta(seconds=1) > done_until
    ]
    print(
        f"{len(windows)} request windows of up to {CHUNK_SECONDS} s, {len(windows) - len(todo)} already stored, "
        f"{len(todo)} to fetch (~{len(todo) * cfg.settings.pace / 60:.0f} min)"
    )

    for i, (start, end) in enumerate(todo, 1):
        pacer.wait()
        bars = ib.reqHistoricalData(
            contract,
            endDateTime=end,
            durationStr=f"{max(int((end - start).total_seconds()), MIN_SECONDS)} S",
            barSizeSetting="1 secs",
            whatToShow=inst.what_to_show,
            useRTH=not inst.all_hours,
            formatDate=2,  # epoch seconds -> timezone-aware datetimes
            timeout=30,
        )
        write_bars(
            bars,
            out,
            symbol_name,
            contract.primaryExchange or contract.exchange,
            contract.secType,
            expiry,
            contract.conId,
            tz,
        )
        print(
            f"[{i}/{len(todo)}] {start.astimezone(tz):%Y-%m-%d %H:%M} -> {end.astimezone(tz):%H:%M}: "
            f"{len(bars)} bars"
        )
    print(f"Done: {out} (source {series}, timestamps in {cfg.settings.timezone})")
    return True


def run_once(ib: IB, cfg: Config, pacer: Pacer, days: int | None) -> int:
    """Process every configured instrument once. Returns the number of failed instruments."""
    done, skipped, failed = 0, 0, 0
    for inst in cfg.instruments:  # in sequence, one after the other
        print(f"\n=== {inst.symbol} ===")
        if inst.skip:
            print("-- skip --")
        try:
            if (not inst.skip) and fetch_instrument(ib, inst, cfg, pacer, days):
                done += 1
            else:
                skipped += 1
        except ValueError as e:
            print(f"{inst.symbol}: {e}", file=sys.stderr)
            failed += 1
    print(f"\n{done} instrument(s) loaded, {skipped} skipped, {failed} failed")
    return failed


def main() -> int:
    args = parse_args()
    try:
        cfg = load_config(args.config)
    except ValueError as e:
        print(f"Config error: {e}", file=sys.stderr)
        return 1
    host = args.host or cfg.connection.host
    port = args.port or cfg.connection.port
    client_id = (
        args.client_id or cfg.connection.client_id or random.randint(2_000, 10_000)
    )

    ib = IB()
    # IB reports request errors only via this event; without it they are silent
    # 2100-2199 are informational status messages (data farm connection OK etc.)
    ib.errorEvent += lambda reqId, code, msg, contract: (
        None
        if 2100 <= code < 2200
        else print(f"IB error {code} (reqId {reqId}): {msg}", file=sys.stderr)
    )

    print(f"Connecting to {host}:{port} (client-id {client_id}) ...")
    try:
        ib.connect(host, port, clientId=client_id)
    except Exception as e:  # noqa: BLE001
        print(f"Connection failed: {e}", file=sys.stderr)
        return 1

    try:
        if args.only_new_instrument:
            # a lost connection raises ConnectionError, which ends the script (handler below)
            return 1 if run_once(ib, cfg, Pacer(ib, cfg.settings.pace), args.days) else 0
        while True:  # update mode: repeat until terminated, reconnecting when the connection is lost
            try:
                update_loop(ib, cfg, args.update_every_min)
            except ConnectionError as e:
                print(f"Connection lost: {e}", file=sys.stderr)
                reconnect(ib, host, port, client_id, args.wait_to_reconnect)
    except ConnectionError as e:  # only reached in --only-new-instrument mode: no reconnect, just exit
        print(f"Connection lost: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nStopped")
        return 0
    finally:
        ib.disconnect()


def update_loop(ib: IB, cfg: Config, every_min: float) -> None:
    """Update all instruments repeatedly; returns only by raising (ConnectionError if the connection is lost)."""
    pacer = Pacer(ib, cfg.settings.pace)
    while True:
        # a drop while idle in ib.sleep() does not raise by itself, so check before every run
        if not ib.isConnected():
            raise ConnectionError("not connected to IB")
        run_once(ib, cfg, pacer, None)
        print(f"Next update in {every_min:g} min (Ctrl-C to stop)")
        ib.sleep(every_min * 60)  # keeps the IB connection serviced while waiting


def reconnect(ib: IB, host: str, port: int, client_id: int, wait_min: float) -> None:
    """Block until the connection to IB is re-established, retrying every `wait_min` minutes."""
    while True:
        print(f"Reconnecting in {wait_min:g} min (Ctrl-C to stop)")
        ib.disconnect()  # drop the dead socket so connect() starts clean
        time.sleep(wait_min * 60)
        print(f"Connecting to {host}:{port} (client-id {client_id}) ...")
        try:
            ib.connect(host, port, clientId=client_id)
            print("Reconnected")
            return
        except Exception as e:  # noqa: BLE001
            print(f"Reconnect failed: {e}", file=sys.stderr)


if __name__ == "__main__":
    sys.exit(main())
