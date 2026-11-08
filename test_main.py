"""Unit tests for main.py / store.py.

Covers the on-disk layout of the per-contract SQLite files and the front-month
selection of futures (i.e. that on a roll-over date the new contract is used).

Run with the project venv so `ib_async` / `yaml` are importable:

    ./venv/bin/python -m unittest test_main -v
"""

import unittest
from pathlib import Path

from config import Instrument
from ib_async import ContractDetails, Future, Stock
from main import resolve_contract
from store import default_out_path

# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


class FakeIB:
    """Minimal stand-in for `IB`: `reqContractDetails` just returns the details we
    queued, whatever contract it was asked about."""

    def __init__(self, details):
        self._details = details

    def reqContractDetails(self, contract):
        return list(self._details)


def _future_details(month, con_id, symbol="DAX", trading_class="FDAX"):
    """One `ContractDetails` for a futures contract in `month` (YYYYMM)."""
    contract = Future(
        symbol=symbol,
        lastTradeDateOrContractMonth=month,
        exchange="EUREX",
        currency="EUR",
        tradingClass=trading_class,
        conId=con_id,
        primaryExchange="EUREX",
    )
    return ContractDetails(contract=contract, timeZoneId="Europe/Berlin")


def _stock_details(symbol, con_id):
    contract = Stock(symbol, "SMART", "USD", conId=con_id, primaryExchange="NASDAQ")
    return ContractDetails(contract=contract, timeZoneId="America/New_York")


def _fut(symbol="DAX", trading_class="FDAX", expiry=None):
    """A FUT `Instrument` with no expiry set, so `resolve_contract` picks the front month."""
    return Instrument(
        symbol=symbol,
        sec_type="FUT",
        exchange="EUREX",
        currency="EUR",
        trading_class=trading_class,
        expiry=expiry,
    )


# --------------------------------------------------------------------------- #
# 1. folder structure for STK and FUT
# --------------------------------------------------------------------------- #


class TestFolderStructure(unittest.TestCase):
    """Both STK and FUT live under  <data-dir>/<SYMBOL>/  ."""

    def test_stk_folder_structure(self):
        p = default_out_path("data", "AAPL", "STK", None)
        self.assertEqual(p, Path("data") / "AAPL" / "AAPL.sqlite")
        # the folder is the symbol; no expiry folder is created
        self.assertEqual(p.parent, Path("data") / "AAPL")
        self.assertEqual(p.parent.parent, Path("data"))

    def test_fut_folder_structure(self):
        p = default_out_path("data", "DAX", "FUT", "202612")
        # same folder scheme as STK: <data-dir>/<SYMBOL>/
        self.assertEqual(p.parent, Path("data") / "DAX")
        self.assertEqual(p.parent.parent, Path("data"))

    def test_stk_and_fut_share_the_same_folder_layout(self):
        stk = default_out_path("data", "AAPL", "STK", None)
        fut = default_out_path("data", "AAPL", "FUT", "202612")
        self.assertEqual(stk.parent, fut.parent)
        self.assertEqual(stk.parent, Path("data") / "AAPL")


# --------------------------------------------------------------------------- #
# 2. folder structure and file name for FUT
# --------------------------------------------------------------------------- #


class TestFutureFileName(unittest.TestCase):
    """FUT file names embed the contract month:  <data-dir>/<SYMBOL>/<SYMBOL>-<YYYYMM>.sqlite  ."""

    def test_fut_folder_and_filename_with_expiry(self):
        p = default_out_path("data", "DAX", "FUT", "202612")
        self.assertEqual(p, Path("data") / "DAX" / "DAX-202612.sqlite")
        self.assertEqual(p.parent, Path("data") / "DAX")
        self.assertEqual(p.name, "DAX-202612.sqlite")
        self.assertTrue(p.name.endswith(".sqlite"))

    def test_fut_filename_tracks_the_expiry(self):
        # different contract months -> different files, same folder
        dec = default_out_path("data", "DAX", "FUT", "202612")
        mar = default_out_path("data", "DAX", "FUT", "202703")
        self.assertEqual(dec, Path("data") / "DAX" / "DAX-202612.sqlite")
        self.assertEqual(mar, Path("data") / "DAX" / "DAX-202703.sqlite")
        self.assertEqual(dec.parent, mar.parent)
        self.assertNotEqual(dec.name, mar.name)

    def test_fut_without_expiry_uses_plain_filename(self):
        # a FUT with no resolved expiry collapses to the STK scheme (no month in the name)
        p = default_out_path("data", "DAX", "FUT", None)
        self.assertEqual(p, Path("data") / "DAX" / "DAX.sqlite")
        self.assertEqual(p.name, "DAX.sqlite")


# --------------------------------------------------------------------------- #
# 3. on the roll-over date the new future contract is used
# --------------------------------------------------------------------------- #


class TestFutureRollOver(unittest.TestCase):
    """With no expiry set, `resolve_contract` returns the front month (earliest
    `lastTradeDateOrContractMonth`). When the old front month rolls off, the new
    front month -- a genuinely different contract -- is selected."""

    def test_front_month_selected_from_active_contracts(self):
        inst = _fut()
        # deliberately out of chronological order to prove selection is by sort, not list order
        active = [
            _future_details("202609", 3003),
            _future_details("202603", 1001),
            _future_details("202606", 2002),
        ]
        sel = resolve_contract(FakeIB(active), inst)
        self.assertEqual(sel.contract.lastTradeDateOrContractMonth, "202603")
        self.assertEqual(sel.contract.conId, 1001)

    def test_on_rollover_the_new_front_month_is_used(self):
        inst = _fut()  # expiry=None -> follow the front month

        # Before the roll: March is the front month.
        before = [
            _future_details("202603", 1001),
            _future_details("202606", 2002),
            _future_details("202609", 3003),
        ]
        sel_before = resolve_contract(FakeIB(before), inst)
        self.assertEqual(sel_before.contract.lastTradeDateOrContractMonth, "202603")
        self.assertEqual(sel_before.contract.conId, 1001)

        # On the roll-over date the old front month (202603) has rolled off and is no
        # longer active; IB now reports June as the new front month.
        after = [
            _future_details("202609", 3003),
            _future_details("202612", 4004),
            _future_details("202606", 2002),
        ]
        sel_after = resolve_contract(FakeIB(after), inst)
        self.assertEqual(sel_after.contract.lastTradeDateOrContractMonth, "202606")
        self.assertEqual(sel_after.contract.conId, 2002)

        # it is genuinely a new contract ...
        self.assertNotEqual(sel_before.contract.conId, sel_after.contract.conId)
        # ... so it points to a different on-disk file than before the roll-over
        out_before = default_out_path(
            "data",
            "DAX",
            sel_before.contract.secType,
            sel_before.contract.lastTradeDateOrContractMonth[:6],
        )
        out_after = default_out_path(
            "data",
            "DAX",
            sel_after.contract.secType,
            sel_after.contract.lastTradeDateOrContractMonth[:6],
        )
        self.assertEqual(out_before, Path("data") / "DAX" / "DAX-202603.sqlite")
        self.assertEqual(out_after, Path("data") / "DAX" / "DAX-202606.sqlite")
        self.assertNotEqual(out_before, out_after)

    def test_explicit_expiry_is_not_rolled(self):
        # When the user pins an expiry, that exact month must be used and no roll happens.
        inst = _fut(expiry="202609")
        active = [
            _future_details("202603", 1001),
            _future_details("202606", 2002),
            _future_details("202609", 3003),
        ]
        sel = resolve_contract(FakeIB(active), inst)
        self.assertEqual(sel.contract.lastTradeDateOrContractMonth, "202609")
        self.assertEqual(sel.contract.conId, 3003)


if __name__ == "__main__":
    unittest.main()
