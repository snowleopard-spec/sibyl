from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS universe_membership (
    cik          INTEGER,
    ticker       TEXT NOT NULL,
    as_of_date   TEXT NOT NULL,
    sector       TEXT,
    market_cap   REAL,
    name         TEXT,
    exchange     TEXT,
    in_universe  INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (ticker, as_of_date)
);
CREATE INDEX IF NOT EXISTS idx_membership_cik  ON universe_membership(cik);
CREATE INDEX IF NOT EXISTS idx_membership_date ON universe_membership(as_of_date);

CREATE TABLE IF NOT EXISTS filings (
    accession         TEXT PRIMARY KEY,
    cik               INTEGER NOT NULL,
    ticker            TEXT,
    form_type         TEXT NOT NULL,
    period_of_report  TEXT,
    acceptance_dt     TEXT NOT NULL,
    filing_date       TEXT,
    primary_doc       TEXT,
    raw_path          TEXT NOT NULL,
    parse_status      TEXT,
    downloaded_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_filings_cik    ON filings(cik);
CREATE INDEX IF NOT EXISTS idx_filings_form   ON filings(form_type);
CREATE INDEX IF NOT EXISTS idx_filings_accept ON filings(acceptance_dt);

CREATE TABLE IF NOT EXISTS filing_scores (
    accession      TEXT NOT NULL,
    section        TEXT NOT NULL,
    weighting      TEXT NOT NULL,
    scorer_version TEXT NOT NULL DEFAULT '1',
    total_words    INTEGER,
    neg            REAL,
    pos            REAL,
    unc            REAL,
    lit            REAL,
    strong_modal   REAL,
    weak_modal     REAL,
    constraining   REAL,
    scored_at      TEXT,
    PRIMARY KEY (accession, section, weighting),
    FOREIGN KEY (accession) REFERENCES filings(accession)
);

CREATE TABLE IF NOT EXISTS filing_signals (
    cik             INTEGER NOT NULL,
    accession       TEXT NOT NULL,
    prior_accession TEXT,
    section         TEXT NOT NULL,
    similarity_yoy  REAL,
    d_unc           REAL,
    d_lit           REAL,
    d_neg           REAL,
    computed_at     TEXT,
    PRIMARY KEY (accession, section),
    FOREIGN KEY (accession) REFERENCES filings(accession)
);
"""


def connect(db_path: str | Path) -> sqlite3.Connection:
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.row_factory = sqlite3.Row
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    # Idempotent column-additions for older DBs created before a column existed.
    _ensure_column(conn, "filing_scores", "scorer_version", "TEXT NOT NULL DEFAULT '1'")
    conn.commit()


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, decl: str) -> None:
    """Add a column if it doesn't already exist. No-op when the column is present."""
    cur = conn.execute(f"PRAGMA table_info({table})")
    if any(row[1] == column for row in cur.fetchall()):
        return
    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


def counts(conn: sqlite3.Connection) -> dict[str, int]:
    cur = conn.cursor()
    out = {}
    for table in ("universe_membership", "filings", "filing_scores", "filing_signals"):
        out[table] = cur.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    return out
