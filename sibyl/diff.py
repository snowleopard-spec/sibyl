"""Stage 5 — yoy textual similarity (Lazy Prices) + sentiment deltas.

For each (filing, section), finds the same-period prior-year filing of the same
form_type and computes:
  - similarity_yoy : cosine on L&M-weighted tfidf vectors
  - d_neg / d_unc / d_lit : Δ proportional L&M scores (current - prior)

Writes to filing_signals.
"""
from __future__ import annotations

import logging
import math
import sqlite3
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .config import Config
from .parse import filing_clean_dir
from .score import (
    SECTIONS,
    compute_doc_frequencies,
    tokenize,
)

logger = logging.getLogger(__name__)

DIFF_VERSION = "1"


@dataclass
class Counts:
    processed: int = 0
    rows_written: int = 0
    skipped: int = 0
    no_prior: int = 0
    section_skipped: int = 0
    errors: int = 0


# --- Prior-filing matching ----------------------------------------------------

def match_prior_filings(
    conn: sqlite3.Connection, *, ciks: list[int] | None = None
) -> dict[str, str]:
    """For every parse_status='ok' filing, find the immediately-prior filing of
    the same (cik, form_type) ordered by (period_of_report, acceptance_dt).
    First filing per chain has no prior and is absent from the dict.
    """
    cur = conn.cursor()
    where = ["parse_status = 'ok'"]
    params: list = []
    if ciks:
        where.append(f"cik IN ({','.join('?' for _ in ciks)})")
        params.extend(int(c) for c in ciks)
    sql = (
        f"SELECT accession, cik, form_type, period_of_report, acceptance_dt "
        f"FROM filings WHERE {' AND '.join(where)} "
        f"ORDER BY cik, form_type, COALESCE(period_of_report, ''), acceptance_dt"
    )
    rows = list(cur.execute(sql, params))
    prior: dict[str, str] = {}
    last_key = (None, None)
    last_acc: str | None = None
    for r in rows:
        key = (r["cik"], r["form_type"])
        if key == last_key and last_acc is not None:
            prior[r["accession"]] = last_acc
        last_key = key
        last_acc = r["accession"]
    return prior


def _filing_meta(conn: sqlite3.Connection, accession: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT accession, cik, form_type, period_of_report, acceptance_dt "
        "FROM filings WHERE accession = ?", (accession,),
    ).fetchone()


# --- Sparse tfidf vectors + cosine --------------------------------------------

def tfidf_vector(
    tokens: list[str], df: dict[str, int], n_docs: int
) -> dict[str, float]:
    """L&M-weighted sparse vector: weight_t = (1 + log10(tf_t)) * log10(N/df_t)."""
    if not tokens or n_docs == 0:
        return {}
    tf = Counter(tokens)
    log10_n = math.log10(n_docs)
    out: dict[str, float] = {}
    for tok, count in tf.items():
        d = df.get(tok)
        if not d:
            continue
        idf = log10_n - math.log10(d)
        if idf <= 0:
            continue
        out[tok] = (1.0 + math.log10(count)) * idf
    return out


def cosine_sim(v1: dict[str, float], v2: dict[str, float]) -> float:
    """Cosine similarity of two sparse vectors. Returns 0.0 if either is empty."""
    if not v1 or not v2:
        return 0.0
    # Iterate over the smaller dict for the dot product.
    if len(v1) > len(v2):
        v1, v2 = v2, v1
    dot = sum(w * v2.get(t, 0.0) for t, w in v1.items())
    if dot == 0.0:
        return 0.0
    n1 = math.sqrt(sum(w * w for w in v1.values()))
    n2 = math.sqrt(sum(w * w for w in v2.values()))
    if n1 == 0.0 or n2 == 0.0:
        return 0.0
    return dot / (n1 * n2)


# --- Section text + scores loaders --------------------------------------------

def _section_text_path(clean_root: Path, cik: int, accession: str, section: str) -> Path:
    fname = "full.txt" if section == "full" else f"{section}.txt"
    return filing_clean_dir(clean_root, cik, accession) / fname


def _load_section_tokens(clean_root: Path, cik: int, accession: str, section: str) -> list[str] | None:
    p = _section_text_path(clean_root, cik, accession, section)
    if not p.exists():
        return None
    return tokenize(p.read_text(encoding="utf-8"))


def _load_proportional(conn: sqlite3.Connection, accession: str) -> dict[str, dict[str, float]]:
    """Returns {section: {neg, unc, lit, ...}} for proportional rows. Empty
    sections (no row) are absent from the dict."""
    out: dict[str, dict[str, float]] = {}
    for r in conn.execute(
        "SELECT section, neg, unc, lit FROM filing_scores "
        "WHERE accession = ? AND weighting = 'proportional'",
        (accession,),
    ):
        out[r["section"]] = {"neg": r["neg"], "unc": r["unc"], "lit": r["lit"]}
    return out


# --- Per-filing compute --------------------------------------------------------

def compute_filing_signals(
    cik: int,
    accession: str,
    prior_accession: str,
    *,
    clean_root: Path,
    conn: sqlite3.Connection,
    df: dict[str, int],
    n_docs: int,
) -> list[dict]:
    """Returns 0-3 rows (one per section where both current and prior have
    scored proportional + section text on disk)."""
    cur_scores = _load_proportional(conn, accession)
    prior_scores = _load_proportional(conn, prior_accession)
    prior_row = _filing_meta(conn, prior_accession)
    if prior_row is None:
        return []
    prior_cik = int(prior_row["cik"])
    now = _utc_now()
    rows: list[dict] = []
    for section in SECTIONS:
        if section not in cur_scores or section not in prior_scores:
            continue
        cur_tokens = _load_section_tokens(clean_root, cik, accession, section)
        prior_tokens = _load_section_tokens(clean_root, prior_cik, prior_accession, section)
        if not cur_tokens or not prior_tokens:
            continue
        v_cur = tfidf_vector(cur_tokens, df, n_docs)
        v_prev = tfidf_vector(prior_tokens, df, n_docs)
        sim = cosine_sim(v_cur, v_prev)
        cur_s = cur_scores[section]
        prev_s = prior_scores[section]
        rows.append({
            "cik": cik,
            "accession": accession,
            "prior_accession": prior_accession,
            "section": section,
            "diff_version": DIFF_VERSION,
            "similarity_yoy": sim,
            "d_unc": cur_s["unc"] - prev_s["unc"],
            "d_lit": cur_s["lit"] - prev_s["lit"],
            "d_neg": cur_s["neg"] - prev_s["neg"],
            "computed_at": now,
        })
    return rows


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _select_targets(
    conn: sqlite3.Connection,
    *,
    ciks: list[int] | None,
    force: bool,
) -> tuple[dict[str, str], int]:
    """Returns (dict[acc → prior_acc] for targets needing computation, skipped_count)."""
    priors = match_prior_filings(conn, ciks=ciks)
    if force:
        return priors, 0
    existing = {
        r["accession"]
        for r in conn.execute(
            "SELECT DISTINCT accession FROM filing_signals WHERE diff_version = ?",
            (DIFF_VERSION,),
        )
    }
    skipped = 0
    targets: dict[str, str] = {}
    for acc, prior_acc in priors.items():
        if acc in existing:
            skipped += 1
            continue
        targets[acc] = prior_acc
    return targets, skipped


def _insert_rows(conn: sqlite3.Connection, rows: list[dict]) -> None:
    if not rows:
        return
    cols = list(rows[0].keys())
    placeholders = ",".join("?" for _ in cols)
    sql = (
        f"INSERT OR REPLACE INTO filing_signals ({','.join(cols)}) "
        f"VALUES ({placeholders})"
    )
    conn.executemany(sql, [tuple(r[c] for c in cols) for r in rows])


def compute_all(
    conn: sqlite3.Connection,
    cfg: Config,
    *,
    ciks: list[int] | None = None,
    limit: int | None = None,
    force: bool = False,
) -> Counts:
    counts = Counts()
    targets, skipped = _select_targets(conn, ciks=ciks, force=force)
    counts.skipped = skipped

    cur = conn.cursor()
    where = ["parse_status = 'ok'"]
    params: list = []
    if ciks:
        where.append(f"cik IN ({','.join('?' for _ in ciks)})")
        params.extend(int(c) for c in ciks)
    total_eligible = cur.execute(
        f"SELECT COUNT(*) FROM filings WHERE {' AND '.join(where)}", params,
    ).fetchone()[0]
    all_priors = match_prior_filings(conn, ciks=ciks)
    counts.no_prior = total_eligible - len(all_priors)

    if not targets:
        logger.info("Nothing to diff (skipped=%d, no_prior=%d).", skipped, counts.no_prior)
        return counts

    logger.info("Pass 1: computing document frequencies...")
    df, n_docs = compute_doc_frequencies(conn, cfg, ciks=ciks)
    logger.info("DF table: %d unique tokens over %d documents.", len(df), n_docs)

    logger.info(
        "Pass 2: diffing %d filings (skipped %d already at v%s, no_prior %d).",
        len(targets), skipped, DIFF_VERSION, counts.no_prior,
    )

    for idx, (accession, prior_accession) in enumerate(targets.items(), start=1):
        meta = _filing_meta(conn, accession)
        if meta is None:
            counts.errors += 1
            continue
        try:
            rows = compute_filing_signals(
                int(meta["cik"]), accession, prior_accession,
                clean_root=cfg.paths.clean, conn=conn, df=df, n_docs=n_docs,
            )
        except Exception as exc:
            logger.error("Diff failed for %s: %s", accession, exc)
            counts.errors += 1
            continue
        if rows:
            _insert_rows(conn, rows)
            counts.processed += 1
            counts.rows_written += len(rows)
        else:
            counts.section_skipped += 1
        if idx % 500 == 0 or idx == len(targets):
            conn.commit()
            logger.info(
                "[%d/%d] processed=%d rows=%d skipped_no_section=%d errors=%d",
                idx, len(targets), counts.processed, counts.rows_written,
                counts.section_skipped, counts.errors,
            )
        if limit is not None and counts.processed >= limit:
            logger.info("Reached --limit %d; stopping.", limit)
            break
    conn.commit()
    return counts


# --- Diagnostics ---------------------------------------------------------------

def _percentile(data: list[float], p: float) -> float:
    if not data:
        return 0.0
    s = sorted(data)
    k = (len(s) - 1) * (p / 100)
    f = int(k)
    c = min(f + 1, len(s) - 1)
    return s[f] + (s[c] - s[f]) * (k - f)


def compute_stats(conn: sqlite3.Connection) -> dict:
    """Distributions of similarity + deltas per section, plus prior-period
    gap distribution (alignment audit)."""
    out: dict = {"by_section": {}, "totals": {}, "period_gap_days": {}}

    total = conn.execute(
        "SELECT COUNT(*) FROM filing_signals WHERE diff_version = ?",
        (DIFF_VERSION,),
    ).fetchone()[0]
    out["totals"] = {
        "rows": int(total),
        "distinct_filings": int(conn.execute(
            "SELECT COUNT(DISTINCT accession) FROM filing_signals WHERE diff_version = ?",
            (DIFF_VERSION,),
        ).fetchone()[0]),
        "diff_version": DIFF_VERSION,
    }

    for section in SECTIONS:
        rows = list(conn.execute(
            "SELECT similarity_yoy, d_neg, d_unc, d_lit FROM filing_signals "
            "WHERE diff_version = ? AND section = ?",
            (DIFF_VERSION, section),
        ))
        if not rows:
            out["by_section"][section] = {"n": 0}
            continue
        sims = [r["similarity_yoy"] for r in rows if r["similarity_yoy"] is not None]
        d_neg = [r["d_neg"] for r in rows if r["d_neg"] is not None]
        d_unc = [r["d_unc"] for r in rows if r["d_unc"] is not None]
        d_lit = [r["d_lit"] for r in rows if r["d_lit"] is not None]
        out["by_section"][section] = {
            "n": len(rows),
            "similarity_p5": round(_percentile(sims, 5), 4),
            "similarity_p50": round(_percentile(sims, 50), 4),
            "similarity_p95": round(_percentile(sims, 95), 4),
            "d_neg_p5": round(_percentile(d_neg, 5), 5),
            "d_neg_p50": round(_percentile(d_neg, 50), 5),
            "d_neg_p95": round(_percentile(d_neg, 95), 5),
            "d_unc_p50": round(_percentile(d_unc, 50), 5),
            "d_lit_p50": round(_percentile(d_lit, 50), 5),
        }

    gap_rows = list(conn.execute(
        """
        SELECT julianday(c.period_of_report) - julianday(p.period_of_report) AS gap_days
        FROM filing_signals s
        JOIN filings c ON c.accession = s.accession
        JOIN filings p ON p.accession = s.prior_accession
        WHERE s.diff_version = ?
          AND c.period_of_report IS NOT NULL
          AND p.period_of_report IS NOT NULL
          AND s.section = 'full'
        """,
        (DIFF_VERSION,),
    ))
    gaps = [float(r["gap_days"]) for r in gap_rows if r["gap_days"] is not None]
    if gaps:
        out["period_gap_days"] = {
            "n": len(gaps),
            "p5": round(_percentile(gaps, 5), 1),
            "p50": round(_percentile(gaps, 50), 1),
            "p95": round(_percentile(gaps, 95), 1),
            "min": round(min(gaps), 1),
            "max": round(max(gaps), 1),
            "pct_within_300_to_430": round(
                sum(1 for g in gaps if 300 <= g <= 430) / len(gaps), 4
            ),
        }
    return out


def render_stats(stats: dict) -> str:
    out: list[str] = []
    t = stats["totals"]
    out.append(f"diff_version: {t['diff_version']}")
    out.append(f"  rows:              {t['rows']}")
    out.append(f"  distinct filings:  {t['distinct_filings']}")
    out.append("")
    for section, s in stats["by_section"].items():
        if s.get("n", 0) == 0:
            out.append(f"== {section}  (n=0) ==")
            out.append("")
            continue
        out.append(f"== {section}  (n={s['n']}) ==")
        out.append(
            f"  similarity P5/P50/P95: {s['similarity_p5']:.4f} / "
            f"{s['similarity_p50']:.4f} / {s['similarity_p95']:.4f}"
        )
        out.append(
            f"  d_neg     P5/P50/P95: {s['d_neg_p5']:+.5f} / "
            f"{s['d_neg_p50']:+.5f} / {s['d_neg_p95']:+.5f}"
        )
        out.append(
            f"  d_unc P50: {s['d_unc_p50']:+.5f}    d_lit P50: {s['d_lit_p50']:+.5f}"
        )
        out.append("")

    g = stats.get("period_gap_days") or {}
    if g:
        out.append("== alignment audit (current vs prior period_of_report, days) ==")
        out.append(
            f"  n={g['n']}  min/P5/P50/P95/max: {g['min']:.0f} / "
            f"{g['p5']:.0f} / {g['p50']:.0f} / {g['p95']:.0f} / {g['max']:.0f}"
        )
        pct = g["pct_within_300_to_430"]
        warn = "  ← LOW" if pct < 0.95 else ""
        out.append(f"  % within 300-430 days (expected ~365 ± 65): {pct:.2%}{warn}")
        out.append("")
    return "\n".join(out)
