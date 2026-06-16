import json
import sqlite3
from pathlib import Path

import pytest

from sibyl import edgar
from sibyl.db import init_schema
from sibyl.universe import (
    _generated_date,
    resolve_ciks,
    snapshot_universe,
    upsert_membership,
)

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def payload() -> dict:
    return json.loads((FIXTURES / "universe_sample.json").read_text())


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    init_schema(c)
    return c


def test_generated_date_parses_utc(payload):
    assert _generated_date(payload) == "2026-06-13"


def test_snapshot_universe_writes_snapshot_and_working(tmp_path, payload):
    snapshots = tmp_path / "snapshots"
    working = tmp_path / "universe.json"
    snap_path = snapshot_universe(payload, snapshots, working)

    assert snap_path == snapshots / "universe_2026-06-13.json"
    assert snap_path.exists()
    assert working.exists()
    assert json.loads(snap_path.read_text())["count"] == 4
    assert json.loads(working.read_text()) == json.loads(snap_path.read_text())


def test_upsert_membership_inserts_and_normalizes(conn, payload):
    as_of, n = upsert_membership(conn, payload)
    assert as_of == "2026-06-13"
    assert n == 4

    rows = list(conn.execute(
        "SELECT ticker, sector, market_cap, name, exchange, in_universe, cik "
        "FROM universe_membership ORDER BY ticker"
    ))
    tickers = [r["ticker"] for r in rows]
    assert tickers == ["AAPL", "ABCD", "MSFT", "ZZZZQ"]  # 'abcd' upper-cased

    # nullable fields preserved as NULL where missing
    zz = next(r for r in rows if r["ticker"] == "ZZZZQ")
    assert zz["sector"] is None
    assert zz["market_cap"] is None
    assert zz["exchange"] is None

    # all rows are members on insert; cik populated later
    assert all(r["in_universe"] == 1 for r in rows)
    assert all(r["cik"] is None for r in rows)


def test_upsert_membership_is_idempotent(conn, payload):
    upsert_membership(conn, payload)
    upsert_membership(conn, payload)
    n = conn.execute("SELECT COUNT(*) FROM universe_membership").fetchone()[0]
    assert n == 4


def test_upsert_membership_updates_on_conflict(conn, payload):
    upsert_membership(conn, payload)
    # mutate a field; same (ticker, as_of_date) should update, not duplicate
    payload["universe"][0]["sector"] = "PHRM"
    upsert_membership(conn, payload)
    sector = conn.execute(
        "SELECT sector FROM universe_membership WHERE ticker = 'AAPL'"
    ).fetchone()[0]
    assert sector == "PHRM"


def test_resolve_ciks(conn, payload):
    upsert_membership(conn, payload)
    ticker_map = edgar.load_ticker_to_cik(FIXTURES / "company_tickers_sample.json")
    unresolved = resolve_ciks(conn, ticker_map, "2026-06-13")

    assert unresolved == ["ZZZZQ"]
    by_ticker = {
        r["ticker"]: r["cik"]
        for r in conn.execute("SELECT ticker, cik FROM universe_membership")
    }
    assert by_ticker["AAPL"] == 320193
    assert by_ticker["MSFT"] == 789019
    assert by_ticker["ABCD"] == 123456
    assert by_ticker["ZZZZQ"] is None


def test_load_ticker_to_cik_uppercases():
    m = edgar.load_ticker_to_cik(FIXTURES / "company_tickers_sample.json")
    assert "AAPL" in m
    assert m["AAPL"] == 320193


def test_cik_padded():
    assert edgar.cik_padded(320193) == "0000320193"


def test_rate_limiter_enforces_throughput():
    """At rate=4, 5 calls should take >=1.0s wall-clock."""
    import time
    limiter = edgar.RateLimiter(4)
    start = time.monotonic()
    for _ in range(5):
        limiter.acquire()
    elapsed = time.monotonic() - start
    assert elapsed >= 0.9  # small slack for clock jitter
