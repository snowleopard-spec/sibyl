"""S&P-wide + per-sector rolling averages of the yoy signals.

For each (date, scope, section, metric), computes mean and median over
the population of S&P filings whose acceptance_dt falls in the date's
quarter. Materialised to the `sp500_aggregates` table for fast chart
lookups (chart.py reads from this table; never recomputes on the fly).

Scopes:
  - 'sp500'       — whole S&P 500
  - <sector name> — e.g. 'Information Technology', 'Financials', ...

Metrics: 'd_neg', 'd_unc', 'similarity_yoy'  (per the chart shape)
Sections: 'risk_factors', 'mdna'             (per spec §5.6)

Date bucketing: filings are grouped into calendar quarters by
acceptance_dt. We use the quarter's last day as the as_of_date so
chart x-axes have a stable date label.
"""
from __future__ import annotations

import logging
import sqlite3
import statistics
from collections import defaultdict
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

CHART_SECTIONS = ("risk_factors", "mdna")
CHART_METRICS = ("d_neg", "d_unc", "similarity_yoy")


def _quarter_end_date(iso_dt: str) -> str:
    """Calendar-quarter end date (YYYY-MM-DD) for an ISO date/timestamp."""
    # acceptance_dt format: YYYY-MM-DDTHH:MM:SSZ — slice the date.
    try:
        year = int(iso_dt[0:4])
        month = int(iso_dt[5:7])
    except (ValueError, IndexError, TypeError):
        return iso_dt[:10]
    if month <= 3:
        return f"{year}-03-31"
    if month <= 6:
        return f"{year}-06-30"
    if month <= 9:
        return f"{year}-09-30"
    return f"{year}-12-31"


def rebuild_aggregates(conn: sqlite3.Connection) -> int:
    """Recompute all S&P aggregates from scratch. Returns rows written.

    Cheap: a handful of SQL joins + Python-side bucketing for medians.
    Always operates on the full S&P stack; queried-stack data is excluded
    from benchmark averages.
    """
    cur = conn.cursor()

    # Pull every (filing × section × metric) value tagged with its sector +
    # acceptance quarter. One row per (accession, section).
    rows = list(cur.execute(
        """
        SELECT
            s.acceptance_dt           AS acceptance_dt,
            m.sector                  AS sector,
            sig.section               AS section,
            sig.similarity_yoy        AS similarity_yoy,
            sig.d_neg                 AS d_neg,
            sig.d_unc                 AS d_unc
        FROM filing_signals sig
        JOIN filings s        ON s.accession = sig.accession
        JOIN sp500_membership m ON m.cik = s.cik
        WHERE s.stack = 'sp500'
          AND sig.section IN ('risk_factors', 'mdna')
        """
    ))

    # Bucket: { (as_of_date, scope, section, metric) → [values] }
    buckets: dict[tuple[str, str, str, str], list[float]] = defaultdict(list)
    for r in rows:
        as_of = _quarter_end_date(r["acceptance_dt"] or "")
        if not as_of:
            continue
        section = r["section"]
        sector = (r["sector"] or "(unknown)").strip() or "(unknown)"
        for metric in CHART_METRICS:
            value = r[metric]
            if value is None:
                continue
            # S&P-wide scope.
            buckets[(as_of, "sp500", section, metric)].append(float(value))
            # Sector scope.
            buckets[(as_of, sector, section, metric)].append(float(value))

    # Wipe + insert.
    cur.execute("DELETE FROM sp500_aggregates")
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    insert_rows = []
    for (as_of, scope, section, metric), values in buckets.items():
        if not values:
            continue
        mean = statistics.fmean(values)
        median = statistics.median(values)
        insert_rows.append((as_of, scope, section, metric, mean, median, len(values), now))

    if insert_rows:
        cur.executemany(
            "INSERT INTO sp500_aggregates "
            "(as_of_date, scope, section, metric, mean_value, median_value, n_filings, computed_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            insert_rows,
        )
    conn.commit()
    logger.info("Rebuilt sp500_aggregates: %d rows.", len(insert_rows))
    return len(insert_rows)


def aggregate_series(
    conn: sqlite3.Connection,
    *,
    scope: str,
    section: str,
    metric: str,
) -> list[dict]:
    """Return time-series rows [{as_of_date, mean, median, n}, ...] sorted by date.

    `scope` is either 'sp500' or a sector name. Empty list when nothing matches.
    """
    return [
        {
            "as_of_date": r["as_of_date"],
            "mean": r["mean_value"],
            "median": r["median_value"],
            "n": r["n_filings"],
        }
        for r in conn.execute(
            "SELECT as_of_date, mean_value, median_value, n_filings "
            "FROM sp500_aggregates "
            "WHERE scope = ? AND section = ? AND metric = ? "
            "ORDER BY as_of_date",
            (scope, section, metric),
        )
    ]


def ticker_series(
    conn: sqlite3.Connection,
    cik: int,
    *,
    section: str,
    metric: str,
    stack: str | None = None,
) -> list[dict]:
    """Per-filing series for a single CIK + section + metric.

    Returns [{as_of_date, value, accession}, ...] sorted by acceptance.
    `stack` is optional; default None searches both stacks (lets cross-ref
    queries pull from sp500-stack rows when applicable).
    """
    metric_col = {
        "d_neg": "sig.d_neg",
        "d_unc": "sig.d_unc",
        "similarity_yoy": "sig.similarity_yoy",
    }[metric]
    where = ["sig.section = ?", "f.cik = ?"]
    params: list = [section, int(cik)]
    if stack is not None:
        where.append("f.stack = ?")
        params.append(stack)
    sql = (
        f"SELECT f.acceptance_dt AS acceptance_dt, sig.accession AS accession, "
        f"{metric_col} AS value "
        "FROM filing_signals sig "
        "JOIN filings f ON f.accession = sig.accession "
        f"WHERE {' AND '.join(where)} "
        "ORDER BY f.acceptance_dt"
    )
    return [
        {
            "as_of_date": _quarter_end_date(r["acceptance_dt"] or ""),
            "accession": r["accession"],
            "value": r["value"],
        }
        for r in conn.execute(sql, params)
    ]


def status(conn: sqlite3.Connection) -> dict:
    """Quick summary for the CLI."""
    n = conn.execute("SELECT COUNT(*) FROM sp500_aggregates").fetchone()[0]
    last = conn.execute("SELECT MAX(computed_at) FROM sp500_aggregates").fetchone()[0]
    scopes = [
        (r["scope"], int(r["n"]))
        for r in conn.execute(
            "SELECT scope, COUNT(*) AS n FROM sp500_aggregates GROUP BY scope ORDER BY n DESC"
        )
    ]
    return {"rows": int(n), "last_computed_at": last, "per_scope": scopes}
