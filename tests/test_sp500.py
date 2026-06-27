import json
import sqlite3
from pathlib import Path

import pytest

from sibyl import sp500
from sibyl.db import init_schema


# ---------------- fixtures ----------------

def _cfg(tmp_path):
    from sibyl.config import Config, SecConfig, UniverseConfig, _resolve_paths
    paths = _resolve_paths(tmp_path, None, None)
    return Config(
        paths=paths,
        sec=SecConfig(user_agent="ua@example.com", rate_limit_per_sec=1),
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


def _write_ticker_file(cfg, mapping):
    payload = {
        str(i): {"cik_str": cik, "ticker": tk, "title": f"Co {tk}"}
        for i, (tk, cik) in enumerate(mapping.items())
    }
    cfg.paths.company_tickers.parent.mkdir(parents=True, exist_ok=True)
    cfg.paths.company_tickers.write_text(json.dumps(payload))


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
# As of the wiki.py refactor, refresh_membership consumes Wikipedia HTML.

SAMPLE_WIKI_HTML = b"""
<html><body>
<table id="constituents" class="wikitable sortable">
  <tr>
    <th>Symbol</th><th>Security</th><th>GICSSector</th>
    <th>GICS Sub-Industry</th><th>Headquarters Location</th>
    <th>Date added</th><th>CIK</th><th>Founded</th>
  </tr>
  <tr><td>AAPL</td><td>Apple Inc.</td><td>Information Technology</td>
      <td>Tech HW</td><td>Cupertino</td><td>1982-11-30</td>
      <td>0000320193</td><td>1976</td></tr>
  <tr><td>MSFT</td><td>Microsoft</td><td>Information Technology</td>
      <td>Systems Software</td><td>Redmond</td><td>1994-06-01</td>
      <td>0000789019</td><td>1975</td></tr>
  <tr><td>BRK.B</td><td>Berkshire Hathaway</td><td>Financials</td>
      <td>Multi-Sector Holdings</td><td>Omaha</td><td>2010-02-16</td>
      <td>0001067983</td><td>1839</td></tr>
  <tr><td>JPM</td><td>JPMorgan Chase</td><td>Financials</td>
      <td>Diversified Banks</td><td>New York</td><td>1975-06-30</td>
      <td>0000019617</td><td>1799</td></tr>
  <tr><td>XYZUNKNOWN</td><td>Unknown Co</td><td>Energy</td>
      <td>Integrated Oil &amp; Gas</td><td>Anytown</td><td>2024-01-01</td>
      <td></td><td>2020</td></tr>
</table>
</body></html>
"""


def test_refresh_membership_end_to_end(cfg, conn):
    # SEC ticker file is the *fallback* for any row whose Wikipedia CIK column
    # is blank. Here only XYZUNKNOWN has a blank CIK; we deliberately don't
    # put it in the SEC file so it stays unresolved.
    _write_ticker_file(cfg, {"AAPL": 320193, "MSFT": 789019})
    holdings, cik_map, unresolved, snap = sp500.refresh_membership(
        cfg, conn, raw_bytes=SAMPLE_WIKI_HTML,
    )
    assert len(holdings) == 5
    # All four CIK'd rows come straight off Wikipedia.
    assert cik_map == {"AAPL": 320193, "MSFT": 789019, "BRK-B": 1067983, "JPM": 19617}
    assert unresolved == ["XYZUNKNOWN"]
    assert snap.exists()
    assert snap.name.startswith("wiki_sp500_")
    n_rows = conn.execute("SELECT COUNT(*) FROM sp500_membership").fetchone()[0]
    assert n_rows == 5


def test_members_with_ciks_excludes_unresolved(cfg, conn):
    sp500.refresh_membership(cfg, conn, raw_bytes=SAMPLE_WIKI_HTML)
    members = sp500.members_with_ciks(conn)
    tickers = {t for t, _, _ in members}
    assert "XYZUNKNOWN" not in tickers
    assert {"AAPL", "MSFT", "BRK-B", "JPM"} <= tickers


def test_refresh_membership_falls_back_to_sec_ticker_file(cfg, conn):
    """A blank Wikipedia CIK that the SEC file CAN resolve gets filled in."""
    _write_ticker_file(cfg, {"XYZUNKNOWN": 999_999})
    _, cik_map, unresolved, _ = sp500.refresh_membership(
        cfg, conn, raw_bytes=SAMPLE_WIKI_HTML,
    )
    assert cik_map["XYZUNKNOWN"] == 999_999
    assert unresolved == []


# ---------------- status ----------------

def test_status_reports_counts_and_sectors(cfg, conn):
    sp500.refresh_membership(cfg, conn, raw_bytes=SAMPLE_WIKI_HTML)
    stat = sp500.status(conn)
    assert stat["members"] == 5
    assert stat["members_with_cik"] == 4
    sectors = dict(stat["per_sector"])
    assert sectors["Information Technology"] == 2
    assert sectors["Financials"] == 2
