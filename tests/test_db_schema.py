import sqlite3

from sibyl.db import counts, init_schema


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    return c


def test_init_schema_creates_all_tables():
    conn = _conn()
    init_schema(conn)
    init_schema(conn)  # idempotent re-run

    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )}
    assert {"universe_membership", "filings", "filing_scores", "filing_signals"} <= tables

    indexes = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index'"
    )}
    expected_indexes = {
        "idx_membership_cik", "idx_membership_date",
        "idx_filings_cik", "idx_filings_form", "idx_filings_accept",
    }
    assert expected_indexes <= indexes

    assert counts(conn) == {
        "universe_membership": 0,
        "filings": 0,
        "filing_scores": 0,
        "filing_signals": 0,
    }
