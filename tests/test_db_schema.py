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
    assert {"universe_membership", "filings", "filing_scores", "filing_signals",
            "sp500_membership", "sp500_aggregates"} <= tables

    indexes = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index'"
    )}
    expected_indexes = {
        "idx_membership_cik", "idx_membership_date",
        "idx_filings_cik", "idx_filings_form", "idx_filings_accept", "idx_filings_stack",
        "idx_sp500_membership_sector", "idx_sp500_membership_cik",
        "idx_sp500_agg_scope",
    }
    assert expected_indexes <= indexes

    assert counts(conn) == {
        "universe_membership": 0,
        "filings": 0,
        "filing_scores": 0,
        "filing_signals": 0,
        "sp500_membership": 0,
        "sp500_aggregates": 0,
    }


def test_filings_has_stack_column_with_default():
    conn = _conn()
    init_schema(conn)
    cols = {r[1]: r for r in conn.execute("PRAGMA table_info(filings)").fetchall()}
    assert "stack" in cols
    # column 4 is dflt_value; SQLite stores it as the literal SQL expression
    assert cols["stack"][4] == "'sp500'"


def test_ensure_column_migrates_old_db():
    """Simulate a pre-stack-column DB by recreating the table without it,
    then verify init_schema adds the column via _ensure_column."""
    conn = _conn()
    conn.executescript("""
        CREATE TABLE filings (
            accession      TEXT PRIMARY KEY,
            cik            INTEGER NOT NULL,
            form_type      TEXT NOT NULL,
            acceptance_dt  TEXT NOT NULL,
            raw_path       TEXT NOT NULL,
            downloaded_at  TEXT NOT NULL
        );
    """)
    conn.execute(
        "INSERT INTO filings (accession, cik, form_type, acceptance_dt, raw_path, downloaded_at) "
        "VALUES ('a1', 100, '10-K', '2024-01-01', '/p', '2024-01-01')"
    )
    init_schema(conn)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(filings)").fetchall()}
    assert "stack" in cols
    row = conn.execute("SELECT stack FROM filings WHERE accession = 'a1'").fetchone()
    assert row[0] == "sp500"
