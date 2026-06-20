"""Queried-stack manager.

Resolves a user ticker → CIK, cross-references the S&P stack to avoid
duplicating filings, downloads any missing 10-K/10-Q within the rolling
5y window into data/queried/, and runs the full parse → sections →
score → diff pipeline against the new filings.

Cross-stack dedup: if the ticker resolves to a CIK already in
sp500_membership, all of its filings live in the S&P stack. The queried
stack stores no new bytes for that ticker; queries against it just read
from the S&P stack's tables and filesystem.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import diff as diff_mod
from . import download as download_mod
from . import parse as parse_mod
from . import score as score_mod
from . import sections as sections_mod
from . import tickers as tickers_mod
from .config import Config, stack_clean, stack_raw, stack_record

logger = logging.getLogger(__name__)

DEFAULT_ROLLING_YEARS = 5


@dataclass(frozen=True)
class FilingRecord:
    stack: str
    cik: int
    ticker: str
    accession: str
    form_type: str
    period_of_report: str | None
    acceptance_dt: str
    raw_ref: str
    sector: str | None
    downloaded_at: str
    parsed_at: str | None
    scored_at: str | None
    diffed_at: str | None

    def as_dict(self) -> dict:
        return {
            "stack": self.stack,
            "cik": self.cik,
            "ticker": self.ticker,
            "accession": self.accession,
            "form_type": self.form_type,
            "period_of_report": self.period_of_report,
            "acceptance_dt": self.acceptance_dt,
            "raw_ref": self.raw_ref,
            "sector": self.sector,
            "downloaded_at": self.downloaded_at,
            "parsed_at": self.parsed_at,
            "scored_at": self.scored_at,
            "diffed_at": self.diffed_at,
        }


# --- Record-file I/O ----------------------------------------------------------

def append_records(record_path: Path, records: list[dict]) -> None:
    """Atomic-enough append: one JSON object per line."""
    if not records:
        return
    record_path.parent.mkdir(parents=True, exist_ok=True)
    with record_path.open("a", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, sort_keys=True) + "\n")


def read_records(record_path: Path) -> list[dict]:
    if not record_path.exists():
        return []
    out: list[dict] = []
    for line in record_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError as exc:
            logger.warning("Skipping malformed record-file line: %s", exc)
    return out


# --- Stack lookups ------------------------------------------------------------

def sp500_cik_to_meta(conn: sqlite3.Connection, cik: int) -> tuple[str, str] | None:
    """Return (ticker, sector) if CIK is a current S&P member, else None."""
    r = conn.execute(
        "SELECT ticker, sector FROM sp500_membership WHERE cik = ? LIMIT 1",
        (int(cik),),
    ).fetchone()
    if r is None:
        return None
    return (r["ticker"], r["sector"] or "")


def filings_for_cik(
    conn: sqlite3.Connection, cik: int, *, stack: str | None = None,
) -> list[dict]:
    """Return DB rows for `cik`. If `stack` is None, search all stacks."""
    where = ["cik = ?"]
    params: list = [int(cik)]
    if stack is not None:
        where.append("stack = ?")
        params.append(stack)
    sql = (
        "SELECT accession, form_type, period_of_report, acceptance_dt, "
        "stack, raw_path, parse_status "
        "FROM filings WHERE " + " AND ".join(where) +
        " ORDER BY period_of_report, acceptance_dt"
    )
    return [dict(r) for r in conn.execute(sql, params)]


# --- Rolling window -----------------------------------------------------------

def _cutoff_date(rolling_years: int = DEFAULT_ROLLING_YEARS) -> str:
    """ISO date `rolling_years` ago from today."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=rolling_years * 365)
    return cutoff.strftime("%Y-%m-%d")


def filter_to_window(
    rows: list[dict], *, rolling_years: int = DEFAULT_ROLLING_YEARS,
) -> list[dict]:
    """Drop rows whose period_of_report (or filing_date fallback) is older
    than the rolling window cutoff. Rows missing both stay (we can't judge)."""
    cutoff = _cutoff_date(rolling_years)
    out: list[dict] = []
    for r in rows:
        d = (r.get("period_of_report") or r.get("acceptance_dt") or "")[:10]
        if not d or d >= cutoff:
            out.append(r)
    return out


# --- Pipeline orchestration for the queried stack ----------------------------

def _run_pipeline_for_cik(
    cfg: Config,
    conn: sqlite3.Connection,
    cik: int,
    *,
    download: bool,
    rolling_years: int,
) -> dict:
    """Download → parse → sections → score → diff for one CIK in the queried
    stack. The S&P stack's DF is reused for tfidf so queried scores are
    directly comparable to the benchmark series.
    """
    counts: dict = {"downloaded": 0, "parsed": 0, "sections": 0, "scored": 0, "diffed": 0}

    if download:
        # Download (or skip if cached) all 10-K/10-Q within the rolling window.
        # download_all's history_start from cfg.universe is used as a cap;
        # callers should ensure it's set to (today - rolling_years).
        dl_counts = download_mod.download_all(
            conn, cfg, stack="queried", ciks=[cik],
        )
        counts["downloaded"] = dl_counts.new_filings

    # Always run the post-download stages: parse → sections → score → diff.
    # These are idempotent and cheap when nothing is new.
    parse_counts = parse_mod.parse_all(conn, cfg, stack="queried", ciks=[cik])
    counts["parsed"] = parse_counts.parsed

    section_counts = sections_mod.extract_all(
        conn, cfg, stack="queried", ciks=[cik], workers=1,
    )
    counts["sections"] = section_counts.both_ok

    # For tfidf consistency with the benchmark series, reuse the S&P DF.
    sp500_df, sp500_n = score_mod.compute_doc_frequencies(conn, cfg, stack="sp500")
    score_counts = score_mod.score_all(
        conn, cfg, stack="queried", ciks=[cik],
        df_override=(sp500_df, sp500_n),
    )
    counts["scored"] = score_counts.processed

    diff_counts = diff_mod.compute_all(
        conn, cfg, stack="queried", ciks=[cik],
    )
    counts["diffed"] = diff_counts.processed
    return counts


# --- Public API ---------------------------------------------------------------

@dataclass
class QueryResult:
    ticker: str
    cik: int
    stack: str               # 'sp500' (cross-ref) or 'queried'
    sector: str | None
    filings: list[dict]      # DB rows for this ticker, filtered to rolling window
    pipeline_counts: dict    # counts from _run_pipeline_for_cik (or empty when sp500)


def get_or_fetch(
    cfg: Config,
    conn: sqlite3.Connection,
    ticker: str,
    *,
    download: bool = True,
    rolling_years: int = DEFAULT_ROLLING_YEARS,
) -> QueryResult:
    """Resolve `ticker` and ensure we have filings for the rolling 5y window.

    1. Resolve ticker → CIK.
    2. If CIK is in S&P, return its S&P-stack filings (cross-ref). No write
       to the queried record. The S&P refresh job is what keeps these fresh.
    3. Otherwise, run download → parse → sections → score → diff for this
       CIK in the queried stack (if `download` is True). Append new filings
       to data/queried/record.jsonl.
    """
    cik = tickers_mod.resolve(cfg, ticker)
    sp_meta = sp500_cik_to_meta(conn, cik)

    if sp_meta is not None:
        sp_ticker, sector = sp_meta
        sp_filings = filings_for_cik(conn, cik, stack="sp500")
        sp_filings = filter_to_window(sp_filings, rolling_years=rolling_years)
        logger.info(
            "Cross-ref: %s (CIK %d) is in S&P; using sp500 stack (%d filings).",
            ticker, cik, len(sp_filings),
        )
        return QueryResult(
            ticker=sp_ticker, cik=cik, stack="sp500", sector=sector,
            filings=sp_filings, pipeline_counts={},
        )

    pipeline_counts = _run_pipeline_for_cik(
        cfg, conn, cik, download=download, rolling_years=rolling_years,
    )

    # Append any newly-downloaded queried-stack filings to record.jsonl.
    queried_filings = filings_for_cik(conn, cik, stack="queried")
    queried_filings = filter_to_window(queried_filings, rolling_years=rolling_years)

    record_path = stack_record(cfg, "queried")
    seen = {r["accession"] for r in read_records(record_path)}
    new_records: list[dict] = []
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    for f in queried_filings:
        if f["accession"] in seen:
            continue
        new_records.append({
            "stack": "queried",
            "cik": int(cik),
            "ticker": ticker,
            "accession": f["accession"],
            "form_type": f["form_type"],
            "period_of_report": f["period_of_report"],
            "acceptance_dt": f["acceptance_dt"],
            "raw_ref": f["raw_path"],
            "sector": None,
            "downloaded_at": now,
            "parsed_at": now if f["parse_status"] else None,
            "scored_at": None,
            "diffed_at": None,
        })
    append_records(record_path, new_records)

    return QueryResult(
        ticker=ticker, cik=cik, stack="queried", sector=None,
        filings=queried_filings, pipeline_counts=pipeline_counts,
    )
