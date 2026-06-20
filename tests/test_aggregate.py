import sqlite3

import pytest

from sibyl import aggregate as agg
from sibyl.db import init_schema


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    init_schema(c)
    return c


def _membership(conn, ticker, cik, sector):
    conn.execute(
        "INSERT INTO sp500_membership(ticker, cik, name, sector, weight_pct, updated_at) "
        "VALUES (?, ?, '-', ?, 1.0, '2026-06-20')",
        (ticker, cik, sector),
    )


def _filing(conn, accession, cik, *, stack="sp500", form_type="10-K",
            acceptance_dt="2024-11-02T18:00:00Z", period_of_report="2024-09-30"):
    conn.execute(
        "INSERT INTO filings(accession, cik, form_type, period_of_report, "
        "acceptance_dt, raw_path, parse_status, stack, downloaded_at) "
        "VALUES (?, ?, ?, ?, ?, '/p', 'ok', ?, ?)",
        (accession, cik, form_type, period_of_report, acceptance_dt, stack, acceptance_dt),
    )


def _signal(conn, accession, cik, *, section="risk_factors",
            similarity=0.85, d_neg=0.001, d_unc=-0.0005):
    conn.execute(
        "INSERT INTO filing_signals(cik, accession, prior_accession, section, "
        "diff_version, similarity_yoy, d_unc, d_lit, d_neg, computed_at) "
        "VALUES (?, ?, 'prior', ?, '1', ?, ?, 0.0, ?, '2024-11-03T00:00:00Z')",
        (cik, accession, section, similarity, d_unc, d_neg),
    )


# --- _quarter_end_date --------------------------------------------------------

def test_quarter_end_date_bucketing():
    assert agg._quarter_end_date("2024-01-15T12:00:00Z") == "2024-03-31"
    assert agg._quarter_end_date("2024-04-30T00:00:00Z") == "2024-06-30"
    assert agg._quarter_end_date("2024-07-15T00:00:00Z") == "2024-09-30"
    assert agg._quarter_end_date("2024-11-02T18:00:00Z") == "2024-12-31"


def test_quarter_end_date_handles_short_input():
    assert agg._quarter_end_date("2024-11-02") == "2024-12-31"


# --- rebuild_aggregates: empty input ----------------------------------------

def test_rebuild_aggregates_empty_db(conn):
    n = agg.rebuild_aggregates(conn)
    assert n == 0


# --- rebuild_aggregates: one filing, one sector -----------------------------

def test_rebuild_aggregates_one_filing_writes_three_metric_rows(conn):
    _membership(conn, "AAPL", 320193, "Information Technology")
    _filing(conn, "a1", 320193)
    _signal(conn, "a1", 320193, section="risk_factors",
            similarity=0.92, d_neg=0.002, d_unc=-0.001)
    conn.commit()

    n = agg.rebuild_aggregates(conn)
    # Three metrics × two scopes (sp500, sector) × one (date, section) = 6 rows.
    assert n == 6

    rows = list(conn.execute("SELECT * FROM sp500_aggregates ORDER BY scope, metric"))
    scopes = {r["scope"] for r in rows}
    assert scopes == {"sp500", "Information Technology"}
    # Mean = median = single value (n=1).
    for r in rows:
        assert r["n_filings"] == 1
        assert r["mean_value"] == r["median_value"]


# --- rebuild_aggregates: mixed sectors --------------------------------------

def test_rebuild_aggregates_groups_by_sector(conn):
    """Two IT names + one Financials name → IT averages two values, Fin
    averages one."""
    _membership(conn, "AAPL", 1, "Information Technology")
    _membership(conn, "MSFT", 2, "Information Technology")
    _membership(conn, "JPM",  3, "Financials")
    for cik, acc, neg in [(1, "a", 0.001), (2, "b", 0.003), (3, "c", 0.005)]:
        _filing(conn, acc, cik)
        _signal(conn, acc, cik, section="mdna", d_neg=neg, similarity=0.8, d_unc=0)
    conn.commit()

    agg.rebuild_aggregates(conn)
    it = conn.execute(
        "SELECT mean_value, median_value, n_filings FROM sp500_aggregates "
        "WHERE scope='Information Technology' AND section='mdna' AND metric='d_neg'"
    ).fetchone()
    assert it["n_filings"] == 2
    assert it["mean_value"] == pytest.approx(0.002)

    fin = conn.execute(
        "SELECT mean_value, n_filings FROM sp500_aggregates "
        "WHERE scope='Financials' AND section='mdna' AND metric='d_neg'"
    ).fetchone()
    assert fin["n_filings"] == 1
    assert fin["mean_value"] == pytest.approx(0.005)

    sp = conn.execute(
        "SELECT mean_value, n_filings FROM sp500_aggregates "
        "WHERE scope='sp500' AND section='mdna' AND metric='d_neg'"
    ).fetchone()
    assert sp["n_filings"] == 3
    assert sp["mean_value"] == pytest.approx((0.001 + 0.003 + 0.005) / 3)


# --- rebuild_aggregates excludes queried-stack rows -------------------------

def test_rebuild_aggregates_ignores_queried_stack(conn):
    _membership(conn, "AAPL", 1, "Information Technology")
    _filing(conn, "sp1", 1, stack="sp500")
    _filing(conn, "q1",  1, stack="queried")
    _signal(conn, "sp1", 1, d_neg=0.001)
    _signal(conn, "q1",  1, d_neg=0.999)   # would skew the mean
    conn.commit()

    agg.rebuild_aggregates(conn)
    sp = conn.execute(
        "SELECT n_filings, mean_value FROM sp500_aggregates "
        "WHERE scope='sp500' AND section='risk_factors' AND metric='d_neg'"
    ).fetchone()
    assert sp["n_filings"] == 1
    assert sp["mean_value"] == pytest.approx(0.001)


# --- aggregate_series + ticker_series ---------------------------------------

def test_aggregate_series_orders_by_date(conn):
    _membership(conn, "AAPL", 1, "IT")
    _filing(conn, "old", 1, acceptance_dt="2023-04-15T00:00:00Z")
    _filing(conn, "new", 1, acceptance_dt="2024-04-15T00:00:00Z")
    _signal(conn, "old", 1, d_neg=0.001, section="risk_factors")
    _signal(conn, "new", 1, d_neg=0.002, section="risk_factors")
    conn.commit()
    agg.rebuild_aggregates(conn)

    series = agg.aggregate_series(conn, scope="IT", section="risk_factors", metric="d_neg")
    dates = [p["as_of_date"] for p in series]
    assert dates == sorted(dates)
    assert dates == ["2023-06-30", "2024-06-30"]


def test_ticker_series_returns_per_filing_values(conn):
    _membership(conn, "AAPL", 1, "IT")
    _filing(conn, "a", 1, acceptance_dt="2024-02-01T00:00:00Z")
    _filing(conn, "b", 1, acceptance_dt="2024-05-01T00:00:00Z")
    _signal(conn, "a", 1, d_neg=0.001, section="mdna")
    _signal(conn, "b", 1, d_neg=0.004, section="mdna")
    conn.commit()

    pts = agg.ticker_series(conn, 1, section="mdna", metric="d_neg")
    assert [p["accession"] for p in pts] == ["a", "b"]
    assert [p["value"] for p in pts] == [pytest.approx(0.001), pytest.approx(0.004)]
    assert [p["as_of_date"] for p in pts] == ["2024-03-31", "2024-06-30"]


def test_status_reports_counts(conn):
    _membership(conn, "AAPL", 1, "IT")
    _filing(conn, "a", 1)
    _signal(conn, "a", 1)
    conn.commit()
    agg.rebuild_aggregates(conn)
    stat = agg.status(conn)
    assert stat["rows"] > 0
    scopes = dict(stat["per_scope"])
    assert "sp500" in scopes
    assert "IT" in scopes
