import json
import sqlite3
from pathlib import Path

import pytest

from sibyl import sp500
from sibyl.db import init_schema


# ---------------- fixtures ----------------

def _cfg(tmp_path):
    from sibyl.config import Config, SecConfig, UnicornConfig, UniverseConfig, _resolve_paths
    paths = _resolve_paths(tmp_path, None, None)
    return Config(
        paths=paths,
        sec=SecConfig(user_agent="ua@example.com", rate_limit_per_sec=1),
        unicorn=UnicornConfig(base_url="", universe_path="", expected_contract_version="1.0", token=None),
        universe=UniverseConfig(form_types=[], include_amendments=False, history_start="2016"),
        download_gzip=True,
    )


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    init_schema(c)
    return c


@pytest.fixture
def cfg(tmp_path):
    return _cfg(tmp_path)


@pytest.fixture(autouse=True)
def _clear_ticker_cache():
    from sibyl import tickers
    tickers._CACHE.clear()
    yield
    tickers._CACHE.clear()


# A realistic IVV CSV stanza: fund metadata stanza + blank line + header + rows.
SAMPLE_IVV_CSV = b"""iShares Core S&P 500 ETF
Fund Holdings as of,"19-Jun-2026"
Inception Date,"15-May-2000"
Shares Outstanding,"900,000,000"
Stock,-,-,-,-,-

Ticker,Name,Sector,Asset Class,Market Value,Weight (%),Notional Value,Shares,Price,Location,Exchange,Currency,FX Rate,Market Currency,Accrual Date
AAPL,APPLE INC,Information Technology,Equity,"1,500,000,000.00",7.10,"1,500,000,000.00","8,000,000",187.50,United States,NASDAQ,USD,1.0,USD,-
MSFT,MICROSOFT CORP,Information Technology,Equity,"1,400,000,000.00",6.60,"1,400,000,000.00","3,500,000",400.00,United States,NASDAQ,USD,1.0,USD,-
BRK-B,BERKSHIRE HATHAWAY INC CLASS B,Financials,Equity,"700,000,000.00",3.30,"700,000,000.00","1,800,000",388.00,United States,NYSE,USD,1.0,USD,-
JPM,JPMORGAN CHASE & CO,Financials,Equity,"600,000,000.00",2.84,"600,000,000.00","3,000,000",200.00,United States,NYSE,USD,1.0,USD,-
XYZUNKNOWN,SOME OBSCURE NAME,Energy,Equity,"5,000,000.00",0.02,"5,000,000.00","100,000",50.00,United States,NYSE,USD,1.0,USD,-
USD,US DOLLAR,Cash and/or Derivatives,Cash,"100,000,000.00",0.47,"100,000,000.00","100,000,000",1.00,United States,-,USD,1.0,USD,-
SP500F25,S&P 500 EMINI FUT JUN 26,-,Futures,"50,000,000.00",0.24,"50,000,000.00","100",5000.00,United States,CME,USD,1.0,USD,-

Disclosure
"The data is provided for informational purposes only..."
"""


def _write_ticker_file(cfg, mapping):
    payload = {
        str(i): {"cik_str": cik, "ticker": tk, "title": f"Co {tk}"}
        for i, (tk, cik) in enumerate(mapping.items())
    }
    cfg.paths.company_tickers.parent.mkdir(parents=True, exist_ok=True)
    cfg.paths.company_tickers.write_text(json.dumps(payload))


# ---------------- parse_holdings ----------------

def test_parse_holdings_returns_equity_rows():
    holdings = sp500.parse_holdings(SAMPLE_IVV_CSV)
    tickers = {h.ticker for h in holdings}
    # AAPL/MSFT/BRK-B/JPM/XYZ included; USD (Cash) and the future excluded.
    assert tickers == {"AAPL", "MSFT", "BRK-B", "JPM", "XYZUNKNOWN"}


def test_parse_holdings_keeps_sector_and_weight():
    holdings = sp500.parse_holdings(SAMPLE_IVV_CSV)
    by_ticker = {h.ticker: h for h in holdings}
    assert by_ticker["AAPL"].sector == "Information Technology"
    assert by_ticker["BRK-B"].sector == "Financials"
    assert by_ticker["AAPL"].weight_pct == pytest.approx(7.10)


def test_parse_holdings_raises_on_missing_header():
    with pytest.raises(ValueError, match="Could not find holdings header"):
        sp500.parse_holdings(b"some unrelated content with no header")


# ---------------- snapshot_csv ----------------

def test_snapshot_csv_writes_dated_file(cfg):
    p = sp500.snapshot_csv(cfg, SAMPLE_IVV_CSV, stamp="2026-06-20")
    assert p.exists()
    assert p.name == "ivv_2026-06-20.csv"
    assert p.read_bytes() == SAMPLE_IVV_CSV


# ---------------- upsert_membership ----------------

def test_upsert_membership_writes_rows_and_handles_unresolved(conn):
    holdings = [
        sp500.Holding(ticker="AAPL", name="APPLE INC", sector="Information Technology", weight_pct=7.1),
        sp500.Holding(ticker="MSFT", name="MICROSOFT CORP", sector="Information Technology", weight_pct=6.6),
        sp500.Holding(ticker="XYZUNKNOWN", name="?", sector="Energy", weight_pct=0.02),
    ]
    n = sp500.upsert_membership(
        conn, holdings,
        cik_map={"AAPL": 320193, "MSFT": 789019},  # XYZUNKNOWN unresolved
    )
    assert n == 3
    rows = {r["ticker"]: r for r in conn.execute("SELECT * FROM sp500_membership")}
    assert rows["AAPL"]["cik"] == 320193
    assert rows["XYZUNKNOWN"]["cik"] is None
    assert rows["AAPL"]["sector"] == "Information Technology"


def test_upsert_membership_replaces_previous(conn):
    h1 = [sp500.Holding(ticker="OLD", name="?", sector="?", weight_pct=1.0)]
    h2 = [sp500.Holding(ticker="NEW", name="?", sector="?", weight_pct=1.0)]
    sp500.upsert_membership(conn, h1, cik_map={})
    sp500.upsert_membership(conn, h2, cik_map={})
    tickers = {r["ticker"] for r in conn.execute("SELECT ticker FROM sp500_membership")}
    assert tickers == {"NEW"}


# ---------------- refresh_membership (integration; raw_bytes passed in) ------

def test_refresh_membership_end_to_end(cfg, conn):
    _write_ticker_file(cfg, {"AAPL": 320193, "MSFT": 789019, "BRK-B": 1067983, "JPM": 19617})
    holdings, cik_map, unresolved, snap = sp500.refresh_membership(
        cfg, conn, raw_bytes=SAMPLE_IVV_CSV,
    )
    assert len(holdings) == 5
    assert cik_map == {"AAPL": 320193, "MSFT": 789019, "BRK-B": 1067983, "JPM": 19617}
    assert unresolved == ["XYZUNKNOWN"]
    assert snap.exists()
    n_rows = conn.execute("SELECT COUNT(*) FROM sp500_membership").fetchone()[0]
    assert n_rows == 5


def test_members_with_ciks_excludes_unresolved(cfg, conn):
    _write_ticker_file(cfg, {"AAPL": 320193, "MSFT": 789019, "BRK-B": 1067983, "JPM": 19617})
    sp500.refresh_membership(cfg, conn, raw_bytes=SAMPLE_IVV_CSV)
    members = sp500.members_with_ciks(conn)
    tickers = {t for t, _, _ in members}
    assert "XYZUNKNOWN" not in tickers
    assert {"AAPL", "MSFT", "BRK-B", "JPM"} <= tickers


# ---------------- status ----------------

def test_status_reports_counts_and_sectors(cfg, conn):
    _write_ticker_file(cfg, {"AAPL": 320193, "MSFT": 789019, "BRK-B": 1067983, "JPM": 19617})
    sp500.refresh_membership(cfg, conn, raw_bytes=SAMPLE_IVV_CSV)
    stat = sp500.status(conn)
    assert stat["members"] == 5
    assert stat["members_with_cik"] == 4
    sectors = dict(stat["per_sector"])
    assert sectors["Information Technology"] == 2
    assert sectors["Financials"] == 2
