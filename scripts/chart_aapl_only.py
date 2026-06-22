"""One-off: render AAPL-only per-filing signals, colored by form_type.

Lets you eyeball whether the annual sawtooth is in the data itself (visible on
a single ticker) or only an aggregation artifact (only visible across pooled
filings).
"""
from __future__ import annotations

import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.dates as mdates  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402

from sibyl.chart import (  # noqa: E402
    METRIC_LABELS, METRICS, SECTION_LABELS, SECTIONS, _to_dates,
)

CIK_AAPL = 320193


def main() -> int:
    conn = sqlite3.connect("data/sibyl.db")
    conn.row_factory = sqlite3.Row

    fig, axes = plt.subplots(len(SECTIONS), len(METRICS), figsize=(15, 7), sharex=True)

    for row, section in enumerate(SECTIONS):
        for col, metric in enumerate(METRICS):
            ax = axes[row][col]
            metric_col = {"d_neg": "sig.d_neg", "d_unc": "sig.d_unc",
                          "similarity_yoy": "sig.similarity_yoy"}[metric]
            rows = list(conn.execute(
                f"SELECT f.acceptance_dt AS dt, f.form_type AS form, "
                f"{metric_col} AS value "
                "FROM filing_signals sig "
                "JOIN filings f ON f.accession = sig.accession "
                "WHERE sig.section = ? AND f.cik = ? "
                "ORDER BY f.acceptance_dt",
                (section, CIK_AAPL),
            ))

            # Split by form type — different markers + colors so the eye can pick out
            # whether 10-Ks vs 10-Qs sit at different levels.
            for form, color, marker in (("10-K", "C3", "o"), ("10-Q", "C0", "s")):
                form_rows = [r for r in rows if r["form"] == form and r["value"] is not None]
                if not form_rows:
                    continue
                dates = _to_dates([r["dt"][:10] for r in form_rows])
                vals = [r["value"] for r in form_rows]
                if dates and vals:
                    ax.plot(
                        dates, vals, marker=marker, linestyle="-",
                        color=color, linewidth=1.2, markersize=7, alpha=0.9,
                        label=form if (row == 0 and col == 0) else None,
                    )

            ax.set_title(f"{SECTION_LABELS[section]} · {METRIC_LABELS[metric]}", fontsize=10)
            ax.grid(True, alpha=0.3)
            ax.tick_params(axis="x", rotation=30, labelsize=8)
            ax.tick_params(axis="y", labelsize=8)
            if row == len(SECTIONS) - 1:
                ax.xaxis.set_major_locator(mdates.YearLocator())
                ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y"))
            if row == 0 and col == 0:
                ax.legend(loc="best", fontsize=9)

    fig.suptitle("AAPL — sentiment signals (no overlays); red ◯ = 10-K, blue ◻ = 10-Q",
                 fontsize=12, y=0.995)
    fig.tight_layout(rect=(0, 0, 1, 0.97))

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = Path(f"data/queried/AAPL/chart_AAPL_only_{stamp}.png")
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, dpi=110, bbox_inches="tight")
    plt.close(fig)
    print(f"Wrote: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
