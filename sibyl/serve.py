"""Flask report at http://localhost:5000.

Renders deciles 1-10 of S&P 500 constituents ranked by mean LM negative
score over the last 4 10-Qs, MD&A and Risk Factors side by side, with
expandable per-filing detail rows.
"""
from __future__ import annotations

import math
from pathlib import Path

import pandas as pd
from flask import Flask, render_template

from . import config as config_mod
from . import db as db_mod
from . import rank as rank_mod


# Folio-allocation palette, mapped onto the 11 GICS sectors. Earth tones,
# stable across refreshes so the same sector keeps its swatch.
SECTOR_COLOURS = {
    "Information Technology":  "#8B7355",  # warm brown (folio primary)
    "Health Care":             "#A0522D",  # sienna
    "Financials":              "#556B2F",  # olive (folio accent)
    "Consumer Discretionary":  "#CD853F",  # peru
    "Industrials":             "#708090",  # slate
    "Communication Services":  "#9E7B5B",  # light taupe
    "Consumer Staples":        "#6B8E6B",  # sage
    "Energy":                  "#8B6914",  # dark goldenrod
    "Utilities":               "#B8860B",  # goldenrod
    "Real Estate":             "#7B6B5A",  # warm grey
    "Materials":               "#665544",  # deep brown
}
DEFAULT_COLOUR = "#A09080"  # taupe (folio muted text)


def _last_refresh(data_root: Path) -> str:
    path = data_root / "last_refresh.txt"
    if not path.exists():
        return "Never"
    return path.read_text(encoding="utf-8").strip() or "Never"


def _filings_for_ticker(detail: pd.DataFrame) -> list[dict]:
    return [
        {
            "acceptance_dt": (row["acceptance_dt"] or "")[:10],
            "period_of_report": row["period_of_report"] or "",
            "section": row["section"],
            "neg": float(row["neg"]) if row["neg"] is not None else None,
            "total_words": int(row["total_words"]) if row["total_words"] is not None else None,
        }
        for _, row in detail.iterrows()
    ]


def _build_decile_rows(df: pd.DataFrame, section_col: str) -> dict:
    """Return {1: [...], 2: [...], ..., 10: [...]} sorted within each decile
    by mean_neg descending (rank 1 = most negative in decile).

    Filing detail is NOT embedded in row dicts — it goes in a separate
    JSON data island so the page stays small (~500KB instead of ~10MB)
    and detail subtables build lazily on click.
    """
    out: dict[int, list[dict]] = {n: [] for n in range(1, 11)}
    mean_col = f"mean_neg_{section_col}"
    fcount_col = f"filing_count_{section_col}"
    decile_col = f"decile_{section_col}"

    eligible = df[df[decile_col].notna()]
    for decile in range(1, 11):
        sub = eligible[eligible[decile_col] == decile].sort_values(mean_col, ascending=False)
        for rank, (_, row) in enumerate(sub.iterrows(), start=1):
            out[decile].append({
                "rank": rank,
                "ticker": row["ticker"],
                "name": row["name"] or "",
                "sector": row["sector"] or "",
                "sector_colour": SECTOR_COLOURS.get(row["sector"], DEFAULT_COLOUR),
                "mean_neg": float(row[mean_col]),
                "filing_count": int(row[fcount_col]),
            })
    return out


DISPLAYED_DECILES = (10, 1)
DECILE_LABELS = {
    10: "Decile 10 — most negative",
    1: "Decile 1 — least negative",
}


def build_report_context(conn) -> dict:
    df = rank_mod.compute_ranks(conn)

    deciles_mdna = _build_decile_rows(df, "mdna") if not df.empty else {n: [] for n in range(1, 11)}
    deciles_risk = _build_decile_rows(df, "risk") if not df.empty else {n: [] for n in range(1, 11)}
    deciles = {n: {"mdna": deciles_mdna[n], "risk": deciles_risk[n]} for n in DISPLAYED_DECILES}

    # Only ship filing detail for tickers that actually appear on the page.
    visible_tickers: set[str] = set()
    for n in DISPLAYED_DECILES:
        for which in ("mdna", "risk"):
            for r in deciles[n][which]:
                visible_tickers.add(r["ticker"])

    detail_by_ticker: dict[str, list[dict]] = {}
    if visible_tickers:
        placeholders = ",".join("?" * len(visible_tickers))
        sql = f"""
            SELECT m.ticker, fs.accession, f.acceptance_dt, f.period_of_report,
                   fs.section, fs.neg, fs.total_words
            FROM filing_scores fs
            JOIN filings f          ON f.accession = fs.accession
            JOIN sp500_membership m ON m.cik = f.cik
            WHERE m.ticker IN ({placeholders})
              AND f.form_type = '10-Q'
              AND fs.weighting = 'proportional'
              AND fs.section IN ('mdna', 'risk_factors')
            ORDER BY m.ticker, f.acceptance_dt DESC, fs.section
        """
        per_ticker = pd.read_sql_query(sql, conn, params=sorted(visible_tickers))
        for ticker, group in per_ticker.groupby("ticker"):
            detail_by_ticker[ticker] = _filings_for_ticker(group)

    return {
        "deciles": deciles,
        "displayed_deciles": DISPLAYED_DECILES,
        "decile_labels": DECILE_LABELS,
        "detail_by_ticker": detail_by_ticker,
        "sector_colours": SECTOR_COLOURS,
        "score_summary": {
            "mdna": {
                "decile_boundaries": rank_mod.decile_boundaries(df["mean_neg_mdna"]) if not df.empty else [],
                "n_scored": int(df["mean_neg_mdna"].notna().sum()) if not df.empty else 0,
            },
            "risk": {
                "decile_boundaries": rank_mod.decile_boundaries(df["mean_neg_risk"]) if not df.empty else [],
                "n_scored": int(df["mean_neg_risk"].notna().sum()) if not df.empty else 0,
            },
        },
    }


def create_app(*, config_path: str | Path = "config.yaml") -> Flask:
    cfg = config_mod.load_config(str(config_path))
    template_dir = Path(__file__).resolve().parent.parent / "templates"
    app = Flask(__name__, template_folder=str(template_dir))

    @app.route("/ping")
    def ping():
        return "<!DOCTYPE html><html><body><h1>PONG</h1><p>Flask is reaching your browser.</p></body></html>"

    @app.route("/")
    def index():
        conn = db_mod.connect(cfg.paths.db)
        db_mod.init_schema(conn)
        try:
            ctx = build_report_context(conn)
        finally:
            conn.close()
        return render_template(
            "report.html",
            last_refresh=_last_refresh(cfg.paths.data_root),
            **ctx,
        )

    return app


# `flask --app sibyl.serve run` (spec §7.5) imports this module and looks
# for a module-level `app`. Defer config loading errors so test runs that
# import the module without a config.yaml don't crash on import.
try:
    app = create_app()
except FileNotFoundError:
    app = None
