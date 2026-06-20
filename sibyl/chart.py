"""Six-panel PNG chart: queried stock vs sector vs S&P average.

Layout: 2 rows (sections: risk_factors, mdna) × 3 columns
(metrics: d_neg, d_unc, similarity_yoy). Each panel has up to three
lines:
  - queried ticker (heavier line)
  - sector mean
  - S&P 500 mean

Rendered via matplotlib's Agg backend (no display required; works on
the droplet over SSH and in CI).
"""
from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.dates as mdates  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402

from . import aggregate as agg_mod  # noqa: E402

logger = logging.getLogger(__name__)

SECTIONS = ("risk_factors", "mdna")
METRICS = ("d_neg", "d_unc", "similarity_yoy")
SECTION_LABELS = {"risk_factors": "Risk Factors (Item 1A)", "mdna": "MD&A (Item 7)"}
METRIC_LABELS = {
    "d_neg": "Δ Negative",
    "d_unc": "Δ Uncertainty",
    "similarity_yoy": "YoY Similarity",
}


def _to_dates(iso_strings: list[str]) -> list[datetime]:
    out: list[datetime] = []
    for s in iso_strings:
        try:
            out.append(datetime.strptime(s[:10], "%Y-%m-%d"))
        except ValueError:
            continue
    return out


def render_chart(
    conn: sqlite3.Connection,
    *,
    ticker: str,
    cik: int,
    sector: str | None,
    output_path: Path,
    title_suffix: str = "",
) -> Path:
    """Render the 6-panel comparison chart for `ticker` (CIK `cik`).

    `sector` is the S&P sector for the ticker if known; if None, the
    sector-average line is skipped.
    """
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    fig, axes = plt.subplots(
        len(SECTIONS), len(METRICS),
        figsize=(15, 7), sharex=True,
    )
    if len(SECTIONS) == 1:
        axes = [axes]

    for row, section in enumerate(SECTIONS):
        for col, metric in enumerate(METRICS):
            ax = axes[row][col]

            # Queried ticker series.
            ticker_pts = agg_mod.ticker_series(conn, cik, section=section, metric=metric)
            t_dates = _to_dates([p["as_of_date"] for p in ticker_pts])
            t_vals = [p["value"] for p in ticker_pts if p["value"] is not None]
            if t_dates and t_vals and len(t_dates) == len(t_vals):
                ax.plot(t_dates, t_vals, "o-", color="C0",
                        linewidth=2.0, markersize=5, label=ticker)

            # Sector mean.
            if sector:
                sec_pts = agg_mod.aggregate_series(
                    conn, scope=sector, section=section, metric=metric,
                )
                s_dates = _to_dates([p["as_of_date"] for p in sec_pts])
                s_vals = [p["mean"] for p in sec_pts]
                if s_dates and s_vals:
                    ax.plot(s_dates, s_vals, "--", color="C1",
                            linewidth=1.2, alpha=0.85, label=f"{sector} avg")

            # S&P 500 mean.
            sp_pts = agg_mod.aggregate_series(
                conn, scope="sp500", section=section, metric=metric,
            )
            sp_dates = _to_dates([p["as_of_date"] for p in sp_pts])
            sp_vals = [p["mean"] for p in sp_pts]
            if sp_dates and sp_vals:
                ax.plot(sp_dates, sp_vals, ":", color="C2",
                        linewidth=1.2, alpha=0.85, label="S&P 500 avg")

            # Panel decoration.
            ax.set_title(f"{SECTION_LABELS[section]} · {METRIC_LABELS[metric]}", fontsize=10)
            ax.grid(True, alpha=0.3)
            ax.tick_params(axis="x", rotation=30, labelsize=8)
            ax.tick_params(axis="y", labelsize=8)
            if row == 1:
                ax.xaxis.set_major_locator(mdates.YearLocator())
                ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y"))
            if row == 0 and col == 0:
                ax.legend(loc="best", fontsize=8)

    title = f"{ticker} vs S&P 500 sentiment signals"
    if sector:
        title += f" (Sector: {sector})"
    if title_suffix:
        title += f"  —  {title_suffix}"
    fig.suptitle(title, fontsize=12, y=0.995)
    fig.tight_layout(rect=(0, 0, 1, 0.97))
    fig.savefig(output_path, dpi=110, bbox_inches="tight")
    plt.close(fig)
    logger.info("Wrote chart: %s", output_path)
    return output_path


def chart_filename(ticker: str, *, stamp: str | None = None) -> str:
    """Stable per-query filename: chart_<TICKER>_<UTCstamp>.png."""
    stamp = stamp or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"chart_{ticker}_{stamp}.png"
