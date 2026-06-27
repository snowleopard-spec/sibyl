"""Decile ranking of S&P 500 constituents by mean LM negative score.

Pulls the N most recent 10-Q filings per ticker per section (MD&A, Risk
Factors), averages the proportional `neg` score, then assigns deciles
1-10 (1 = least negative, 10 = most negative) independently per section.
"""
from __future__ import annotations

import sqlite3

import pandas as pd

SECTIONS = ("mdna", "risk_factors")
MIN_FILINGS_PER_SECTION = 2

# Filings whose section is shorter than this are treated as boilerplate
# stubs (e.g. 10-Q Risk Factors that just says "no material changes from
# the prior 10-K") and excluded from the mean before deciling. Without
# this filter, ~15% of S&P 500 constituents collapse into Risk decile 1
# tied at neg=0 because their 10-Q Item 1A is a single boilerplate
# sentence rather than substantive risk disclosure.
MIN_WORDS_PER_FILING = 200


def compute_ranks(conn: sqlite3.Connection, *, n_filings: int = 4) -> pd.DataFrame:
    """Return one row per ticker with mean neg scores and deciles.

    Columns: ticker, name, sector, mean_neg_mdna, mean_neg_risk,
    filing_count_mdna, filing_count_risk, decile_mdna, decile_risk.
    """
    sql = """
        WITH ranked AS (
            SELECT
                m.ticker,
                m.name,
                m.sector,
                fs.section,
                fs.neg,
                ROW_NUMBER() OVER (
                    PARTITION BY m.ticker, fs.section
                    ORDER BY f.acceptance_dt DESC
                ) AS rn
            FROM filing_scores fs
            JOIN filings f             ON f.accession = fs.accession
            JOIN sp500_membership m    ON m.cik = f.cik
            WHERE f.form_type = '10-Q'
              AND fs.weighting = 'proportional'
              AND fs.section IN ('mdna', 'risk_factors')
              AND fs.total_words >= ?
        )
        SELECT ticker, name, sector, section,
               AVG(neg) AS mean_neg,
               COUNT(*) AS filing_count
        FROM ranked
        WHERE rn <= ?
        GROUP BY ticker, name, sector, section
    """
    long = pd.read_sql_query(sql, conn, params=(MIN_WORDS_PER_FILING, n_filings))
    if long.empty:
        return pd.DataFrame(
            columns=[
                "ticker", "name", "sector",
                "mean_neg_mdna", "mean_neg_risk",
                "filing_count_mdna", "filing_count_risk",
                "decile_mdna", "decile_risk",
            ]
        )

    wide = long.pivot_table(
        index=["ticker", "name", "sector"],
        columns="section",
        values=["mean_neg", "filing_count"],
        aggfunc="first",
    )
    wide.columns = [f"{a}_{b}" for a, b in wide.columns]
    wide = wide.reset_index().rename(
        columns={
            "mean_neg_mdna": "mean_neg_mdna",
            "mean_neg_risk_factors": "mean_neg_risk",
            "filing_count_mdna": "filing_count_mdna",
            "filing_count_risk_factors": "filing_count_risk",
        }
    )

    for col in ("mean_neg_mdna", "mean_neg_risk", "filing_count_mdna", "filing_count_risk"):
        if col not in wide.columns:
            wide[col] = pd.NA

    wide["filing_count_mdna"] = wide["filing_count_mdna"].fillna(0).astype(int)
    wide["filing_count_risk"] = wide["filing_count_risk"].fillna(0).astype(int)

    # Per-section eligibility: a ticker can be ranked in one section even
    # if it has no substantive filings in the other. (Many S&P 500 10-Qs
    # carry a boilerplate Risk Factors stub — those tickers should still
    # appear in the MD&A ranking.)
    mdna_ok = wide["filing_count_mdna"] >= MIN_FILINGS_PER_SECTION
    risk_ok = wide["filing_count_risk"] >= MIN_FILINGS_PER_SECTION
    wide = wide.loc[mdna_ok | risk_ok].copy()
    wide.loc[~mdna_ok, "mean_neg_mdna"] = pd.NA
    wide.loc[~risk_ok, "mean_neg_risk"] = pd.NA

    wide["decile_mdna"] = _qcut_deciles(wide["mean_neg_mdna"])
    wide["decile_risk"] = _qcut_deciles(wide["mean_neg_risk"])

    return wide[
        [
            "ticker", "name", "sector",
            "mean_neg_mdna", "mean_neg_risk",
            "filing_count_mdna", "filing_count_risk",
            "decile_mdna", "decile_risk",
        ]
    ].reset_index(drop=True)


def _qcut_deciles(values: pd.Series) -> pd.Series:
    """Decile labels 1-10 via pd.qcut, tolerant of tied bin edges + NaN.

    With duplicates='drop' you cannot also pin labels=range(1, 11): if any
    bin edges collapse, pandas raises. Use unlabelled bins instead and
    shift to 1-based, then forward-cast (NaN-aware Int64) so tickers with
    a NaN section mean stay NaN in the output rather than being
    coerced or dropped.
    """
    codes = pd.qcut(values, q=10, labels=False, duplicates="drop")
    return (codes + 1).astype("Int64")


def get_filing_detail(conn: sqlite3.Connection, ticker: str) -> pd.DataFrame:
    """Per-filing breakdown for one ticker: one row per (filing, section)."""
    sql = """
        SELECT
            fs.accession,
            f.acceptance_dt,
            f.period_of_report,
            fs.section,
            fs.neg,
            fs.total_words
        FROM filing_scores fs
        JOIN filings f          ON f.accession = fs.accession
        JOIN sp500_membership m ON m.cik = f.cik
        WHERE m.ticker = ?
          AND f.form_type = '10-Q'
          AND fs.weighting = 'proportional'
          AND fs.section IN ('mdna', 'risk_factors')
        ORDER BY f.acceptance_dt DESC, fs.section
    """
    return pd.read_sql_query(sql, conn, params=(ticker,))


def decile_boundaries(series: pd.Series) -> list[float]:
    """Return 11 boundary values: min + 9 internal cuts + max."""
    s = series.dropna()
    if s.empty:
        return []
    return [float(q) for q in s.quantile([i / 10 for i in range(11)])]
