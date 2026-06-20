"""Smoke test the research-tool pipeline on a 20-name S&P subset.

NOTE: BlackRock's IVV CSV endpoint now returns the HTML landing page
instead of CSV (URL changed or now requires a browser session). Until
that's resolved, this smoke test injects a hardcoded 20-name S&P
membership directly into the DB and runs the rest of the pipeline
against it. The IVV issue is tracked separately.

Steps:
  1. Inject 20 hand-picked S&P names (top by weight, typical) directly
     into sp500_membership.
  2. Download missing 10-K/10-Q for those 20.
  3. Parse / sections / score / diff (sp500 stack, restricted CIKs).
  4. Rebuild aggregates.
  5. Render a chart for AAPL.
"""
from __future__ import annotations

import sys
import time
from datetime import datetime, timezone

from sibyl import aggregate as agg_mod
from sibyl import chart as chart_mod
from sibyl import diff as diff_mod
from sibyl import download as download_mod
from sibyl import parse as parse_mod
from sibyl import queried as queried_mod
from sibyl import score as score_mod
from sibyl import sections as sections_mod
from sibyl import tickers as tickers_mod
from sibyl.config import ensure_dirs, load_config
from sibyl.db import connect, init_schema


# Top-of-weight S&P names as of mid-2026 — used as a stable smoke-test set
# until the IVV pull is repaired. Sector labels from GICS.
HARDCODED_MEMBERS = [
    ("AAPL",  "Information Technology", 7.0),
    ("MSFT",  "Information Technology", 6.5),
    ("NVDA",  "Information Technology", 6.0),
    ("AMZN",  "Consumer Discretionary", 3.5),
    ("META",  "Communication Services", 2.5),
    ("GOOGL", "Communication Services", 2.0),
    ("GOOG",  "Communication Services", 2.0),
    ("BRK-B", "Financials",             1.8),
    ("LLY",   "Health Care",            1.5),
    ("V",     "Financials",             1.3),
    ("JPM",   "Financials",             1.3),
    ("XOM",   "Energy",                 1.2),
    ("UNH",   "Health Care",            1.2),
    ("WMT",   "Consumer Staples",       1.1),
    ("JNJ",   "Health Care",            1.1),
    ("MA",    "Financials",             1.1),
    ("PG",    "Consumer Staples",       1.0),
    ("HD",    "Consumer Discretionary", 1.0),
    ("COST",  "Consumer Staples",       0.9),
    ("MRK",   "Health Care",            0.9),
]

QUERY_TICKER = "AAPL"


def banner(title: str) -> None:
    print(f"\n{'=' * 70}\n{title}\n{'=' * 70}", flush=True)


def step(name: str) -> float:
    print(f"\n--- {name} ---", flush=True)
    return time.time()


def main() -> int:
    cfg = load_config("config.yaml")
    ensure_dirs(cfg)
    conn = connect(cfg.paths.db)
    init_schema(conn)

    banner("STEP 1 — Inject 20 hardcoded S&P members (IVV pull deferred)")
    t = step("resolve tickers + insert into sp500_membership")
    resolved, unresolved = tickers_mod.resolve_many(cfg, [t for t, _, _ in HARDCODED_MEMBERS])
    print(f"  Resolved CIKs:   {len(resolved)}")
    if unresolved:
        print(f"  Unresolved:      {unresolved}")
    # Wipe + insert.
    conn.execute("DELETE FROM sp500_membership")
    updated_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    for ticker, sector, weight in HARDCODED_MEMBERS:
        cik = resolved.get(ticker)
        conn.execute(
            "INSERT INTO sp500_membership(ticker, cik, name, sector, weight_pct, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (ticker, cik, ticker, sector, weight, updated_at),
        )
    conn.commit()
    top_ciks = [resolved[t] for t, _, _ in HARDCODED_MEMBERS if t in resolved]
    print(f"  sp500_membership rows: {len(HARDCODED_MEMBERS)}")
    print(f"  CIKs for pipeline:     {len(top_ciks)}")
    print(f"  Elapsed:               {time.time()-t:.1f}s")

    banner("STEP 2 — Download (10-K + 10-Q within history_start)")
    print(f"  cfg.universe.form_types:    {cfg.universe.form_types}")
    print(f"  cfg.universe.history_start: {cfg.universe.history_start}")
    t = step("download_mod.download_all(stack='sp500', ciks=top_ciks)")
    dl = download_mod.download_all(conn, cfg, stack="sp500", ciks=top_ciks)
    print(f"  CIKs processed:    {dl.ciks_processed}")
    print(f"  New filings:       {dl.new_filings}")
    print(f"  Skipped (cached):  {dl.skipped}")
    print(f"  Failed:            {dl.failed}")
    print(f"  Elapsed:           {time.time()-t:.1f}s")

    banner("STEP 3a — Parse")
    t = step("parse_mod.parse_all(stack='sp500', ciks=top_ciks)")
    pc = parse_mod.parse_all(conn, cfg, stack="sp500", ciks=top_ciks)
    print(f"  Parsed:     {pc.parsed}  Failed: {pc.failed}  Suspicious: {pc.suspicious}")
    print(f"  Elapsed:    {time.time()-t:.1f}s")

    banner("STEP 3b — Sections (10-K + 10-Q)")
    t = step("sections_mod.extract_all(stack='sp500', ciks=top_ciks)")
    sc = sections_mod.extract_all(conn, cfg, stack="sp500", ciks=top_ciks)
    print(f"  Processed: {sc.processed}  Both-OK: {sc.both_ok}  section_fail: {sc.section_fail}")
    print(f"  Suspicious: {sc.suspicious}  Skipped: {sc.skipped}")
    print(f"  Elapsed:   {time.time()-t:.1f}s")

    banner("STEP 3c — Score (L&M)")
    t = step("score_mod.score_all(stack='sp500', ciks=top_ciks)")
    scr = score_mod.score_all(conn, cfg, stack="sp500", ciks=top_ciks)
    print(f"  Filings scored: {scr.processed}  Rows written: {scr.rows_written}")
    print(f"  Skipped: {scr.skipped}  Errors: {scr.errors}")
    print(f"  Elapsed: {time.time()-t:.1f}s")

    banner("STEP 3d — Diff (yoy)")
    t = step("diff_mod.compute_all(stack='sp500', ciks=top_ciks)")
    df = diff_mod.compute_all(conn, cfg, stack="sp500", ciks=top_ciks)
    print(f"  Processed: {df.processed}  Rows: {df.rows_written}  No-prior: {df.no_prior}  Errors: {df.errors}")
    print(f"  Elapsed:   {time.time()-t:.1f}s")

    banner("STEP 4 — Rebuild aggregates")
    t = step("agg_mod.rebuild_aggregates(conn)")
    n_agg = agg_mod.rebuild_aggregates(conn)
    print(f"  Aggregate rows: {n_agg}")
    print(f"  Elapsed:        {time.time()-t:.1f}s")

    banner(f"STEP 5 — Chart for {QUERY_TICKER}")
    t = step("queried.get_or_fetch + chart.render_chart")
    result = queried_mod.get_or_fetch(cfg, conn, QUERY_TICKER, download=False)
    print(f"  Stack:    {result.stack}  Sector: {result.sector}  Filings: {len(result.filings)}")
    out_dir = cfg.paths.data_root / "queried" / result.ticker
    out_path = out_dir / chart_mod.chart_filename(result.ticker)
    chart_mod.render_chart(
        conn, ticker=result.ticker, cik=result.cik, sector=result.sector,
        output_path=out_path,
        title_suffix=f"SMOKE TEST ({len(HARDCODED_MEMBERS)}-name S&P subset)",
    )
    print(f"  Chart:    {out_path}")
    print(f"  Elapsed:  {time.time()-t:.1f}s")

    banner("SMOKE TEST COMPLETE")
    return 0


if __name__ == "__main__":
    sys.exit(main())
