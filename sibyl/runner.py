"""Top-level workflow orchestrator for the research tool.

Two entry points:
  - refresh_sp500(cfg, conn, *, download=True): pull IVV CSV, upsert
    membership, optionally pull new filings, run the post-download
    stages, and rebuild the rolling aggregates.
  - query(cfg, conn, ticker, *, download=True, chart=True): resolve
    ticker, cross-reference against S&P or fetch into the queried
    stack, then render the comparison chart.

Both functions are CLI-invokable via `sibyl sp500 refresh` and
`sibyl research TICKER` respectively (see cli.py).
"""
from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from . import aggregate as aggregate_mod
from . import chart as chart_mod
from . import diff as diff_mod
from . import download as download_mod
from . import parse as parse_mod
from . import queried as queried_mod
from . import score as score_mod
from . import sections as sections_mod
from . import sp500 as sp500_mod
from .config import Config

logger = logging.getLogger(__name__)


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --- S&P 500 refresh ----------------------------------------------------------

def refresh_sp500(
    cfg: Config,
    conn: sqlite3.Connection,
    *,
    download: bool = True,
) -> dict:
    """Full S&P refresh: membership → optional download → post-stages → aggregates.

    With `download=False` the membership pull + aggregate rebuild still run,
    but no new SEC filings are fetched. Useful for membership-only refreshes
    (e.g. the monthly IVV-only cron).
    """
    summary: dict = {"started_at": _utc_now()}

    # 1. Membership.
    logger.info("Refreshing S&P 500 membership...")
    holdings, cik_map, unresolved, snap = sp500_mod.refresh_membership(cfg, conn)
    summary["membership"] = {
        "members": len(holdings),
        "resolved_ciks": len(cik_map),
        "unresolved_tickers": unresolved,
        "snapshot_path": str(snap),
    }

    # 2. Optional download + post-stages.
    if download:
        logger.info("Downloading missing S&P filings...")
        dl = download_mod.download_all(conn, cfg, stack="sp500")
        summary["download"] = {
            "ciks_processed": dl.ciks_processed,
            "new_filings": dl.new_filings,
            "skipped": dl.skipped,
            "failed": dl.failed,
        }

        logger.info("Parsing new filings...")
        parse_counts = parse_mod.parse_all(conn, cfg, stack="sp500")
        summary["parse"] = {
            "parsed": parse_counts.parsed, "skipped": parse_counts.skipped,
            "failed": parse_counts.failed, "suspicious": parse_counts.suspicious,
        }

        logger.info("Extracting sections...")
        sec_counts = sections_mod.extract_all(conn, cfg, stack="sp500")
        summary["sections"] = {
            "processed": sec_counts.processed, "both_ok": sec_counts.both_ok,
            "partial": sec_counts.partial, "section_fail": sec_counts.section_fail,
            "suspicious": sec_counts.suspicious, "skipped": sec_counts.skipped,
        }

        logger.info("Scoring filings...")
        score_counts = score_mod.score_all(conn, cfg, stack="sp500")
        summary["score"] = {
            "processed": score_counts.processed, "rows_written": score_counts.rows_written,
            "skipped": score_counts.skipped, "errors": score_counts.errors,
        }

        logger.info("Computing yoy signals...")
        diff_counts = diff_mod.compute_all(conn, cfg, stack="sp500")
        summary["diff"] = {
            "processed": diff_counts.processed, "rows_written": diff_counts.rows_written,
            "skipped": diff_counts.skipped, "no_prior": diff_counts.no_prior,
            "errors": diff_counts.errors,
        }
    else:
        logger.info("Skipping filing download (--no-download).")
        summary["download"] = {"skipped_entirely": True}

    # 3. Aggregates.
    logger.info("Rebuilding rolling aggregates...")
    n_agg_rows = aggregate_mod.rebuild_aggregates(conn)
    summary["aggregates"] = {"rows_written": n_agg_rows}

    summary["finished_at"] = _utc_now()
    return summary


def render_sp500_summary(summary: dict) -> str:
    out: list[str] = [f"S&P 500 refresh  (started {summary['started_at']}, finished {summary['finished_at']})"]
    m = summary.get("membership", {})
    out.append(f"  Members:           {m.get('members', 0)}  (resolved CIKs: {m.get('resolved_ciks', 0)})")
    if m.get("unresolved_tickers"):
        sample = ", ".join(m["unresolved_tickers"][:5])
        more = f" (+{len(m['unresolved_tickers'])-5} more)" if len(m["unresolved_tickers"]) > 5 else ""
        out.append(f"  Unresolved:        {sample}{more}")
    dl = summary.get("download") or {}
    if dl.get("skipped_entirely"):
        out.append("  Filings:           (download skipped)")
    else:
        out.append(f"  New filings:       {dl.get('new_filings', 0)}  (skipped {dl.get('skipped', 0)}, failed {dl.get('failed', 0)})")
        sec = summary.get("sections", {})
        out.append(f"  Sections both-ok:  {sec.get('both_ok', 0)}  (partial {sec.get('partial', 0)}, section_fail {sec.get('section_fail', 0)})")
        sc = summary.get("score", {})
        out.append(f"  Score rows:        {sc.get('rows_written', 0)}")
        diff = summary.get("diff", {})
        out.append(f"  Diff rows:         {diff.get('rows_written', 0)}  (no_prior {diff.get('no_prior', 0)})")
    out.append(f"  Aggregate rows:    {summary.get('aggregates', {}).get('rows_written', 0)}")
    return "\n".join(out)


# --- Single-ticker query ------------------------------------------------------

def query(
    cfg: Config,
    conn: sqlite3.Connection,
    ticker: str,
    *,
    download: bool = True,
    chart: bool = True,
    output_dir: Path | None = None,
    form_type: str = "10-K",
) -> dict:
    """End-to-end query workflow: resolve, fetch (or cross-ref), chart.

    Returns a dict with the ticker resolution, the QueryResult fields,
    pipeline counts (empty when the cross-ref hits S&P), and the chart
    path (None when chart=False).
    """
    summary: dict = {"started_at": _utc_now(), "input_ticker": ticker}

    result = queried_mod.get_or_fetch(cfg, conn, ticker, download=download)
    summary["resolution"] = {
        "ticker": result.ticker, "cik": result.cik,
        "stack": result.stack, "sector": result.sector,
    }
    summary["filings_count"] = len(result.filings)
    summary["pipeline_counts"] = result.pipeline_counts

    chart_path: Path | None = None
    if chart:
        out_dir = Path(output_dir) if output_dir else (cfg.paths.data_root / "queried" / result.ticker)
        chart_path = out_dir / chart_mod.chart_filename(result.ticker)
        chart_mod.render_chart(
            conn,
            ticker=result.ticker,
            cik=result.cik,
            sector=result.sector,
            output_path=chart_path,
            title_suffix=f"{summary['filings_count']} filings · stack={result.stack}",
            form_type=form_type,
        )
    summary["chart_path"] = str(chart_path) if chart_path else None
    summary["finished_at"] = _utc_now()
    return summary


def render_query_summary(summary: dict) -> str:
    r = summary["resolution"]
    out = [
        f"Query: {summary['input_ticker']}  →  {r['ticker']} (CIK {r['cik']})",
        f"  Stack:        {r['stack']}",
    ]
    if r["sector"]:
        out.append(f"  Sector:       {r['sector']}")
    out.append(f"  Filings:      {summary['filings_count']}")
    if summary["pipeline_counts"]:
        pc = summary["pipeline_counts"]
        out.append(
            f"  Pipeline:     downloaded={pc.get('downloaded', 0)} "
            f"parsed={pc.get('parsed', 0)} sections={pc.get('sections', 0)} "
            f"scored={pc.get('scored', 0)} diffed={pc.get('diffed', 0)}"
        )
    if summary["chart_path"]:
        out.append(f"  Chart:        {summary['chart_path']}")
    return "\n".join(out)
