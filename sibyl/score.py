"""Stage 4 — tokenize + L&M category counts (proportional + tfidf weightings)."""
from __future__ import annotations

import json
import logging
import math
import re
import sqlite3
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .config import Config
from .lm_dictionary import CATEGORIES, load_master_dictionary
from .parse import filing_clean_dir

logger = logging.getLogger(__name__)

SCORER_VERSION = "1"

# Letters-only tokenization: lowercase, strip punctuation and numbers.
# Matches L&M's tokenization conventions so word forms align with the dictionary.
TOKEN_RE = re.compile(r"[A-Za-z]+")

SECTIONS = ("full", "risk_factors", "mdna")
WEIGHTINGS = ("proportional", "tfidf")

# DB-column name → L&M category name.
CATEGORY_COLUMNS = {
    "neg": "Negative",
    "pos": "Positive",
    "unc": "Uncertainty",
    "lit": "Litigious",
    "strong_modal": "Strong_Modal",
    "weak_modal": "Weak_Modal",
    "constraining": "Constraining",
}


@dataclass
class Counts:
    processed: int = 0
    rows_written: int = 0
    skipped: int = 0
    errors: int = 0


def tokenize(text: str) -> list[str]:
    """Lowercase, strip punctuation/numbers; one token per consecutive letter run."""
    return [m.group(0).lower() for m in TOKEN_RE.finditer(text)]


def proportional_scores(
    tokens: list[str], lm: dict[str, set[str]]
) -> dict[str, float]:
    """Per category, return (count of category words) / total_words."""
    n = len(tokens)
    if n == 0:
        return {cat: 0.0 for cat in lm}
    tf = Counter(tokens)
    out: dict[str, float] = {}
    for cat, words in lm.items():
        hits = sum(tf[w] for w in words if w in tf)
        out[cat] = hits / n
    return out


def tfidf_scores(
    tokens: list[str],
    lm: dict[str, set[str]],
    df: dict[str, int],
    n_docs: int,
) -> dict[str, float]:
    """L&M weighted variant: per token, weight = (1 + log10(tf)) * log10(N/df).

    Per category, sum weights of in-category tokens. Normalize by total tokens to
    keep the result in the same units as `proportional` (per-word rate equivalent).
    Tokens absent from the corpus DF table (shouldn't happen for full-corpus DF
    but guard anyway) are skipped — they carry no IDF signal.
    """
    n = len(tokens)
    if n == 0 or n_docs == 0:
        return {cat: 0.0 for cat in lm}
    tf = Counter(tokens)
    log10_n = math.log10(n_docs)
    weights: dict[str, float] = {}
    for tok, count in tf.items():
        d = df.get(tok)
        if not d:
            continue
        # Cap idf at log10(N/df) >= 0 (i.e. df <= N). df > N shouldn't happen.
        idf = log10_n - math.log10(d)
        if idf <= 0:
            continue
        weights[tok] = (1.0 + math.log10(count)) * idf

    out: dict[str, float] = {}
    for cat, words in lm.items():
        s = sum(weights[w] for w in words if w in weights)
        out[cat] = s / n
    return out


def compute_doc_frequencies(
    conn: sqlite3.Connection,
    cfg: Config,
    *,
    stack: str = "sp500",
    ciks: list[int] | None = None,
) -> tuple[dict[str, int], int]:
    """Pass 1: tokenize every eligible full.txt in the given stack; return
    (df_per_token, n_docs). A document = one filing's full.txt."""
    from .config import VALID_STACKS, stack_clean
    if stack not in VALID_STACKS:
        raise ValueError(f"unknown stack {stack!r}; expected one of {VALID_STACKS}")
    clean_root = stack_clean(cfg, stack)

    df: Counter[str] = Counter()
    n_docs = 0
    cur = conn.cursor()
    where = ["parse_status = 'ok'", "stack = ?"]
    params: list = [stack]
    if ciks:
        where.append(f"cik IN ({','.join('?' for _ in ciks)})")
        params.extend(int(c) for c in ciks)
    sql = f"SELECT cik, accession FROM filings WHERE {' AND '.join(where)}"
    rows = list(cur.execute(sql, params))

    for r in rows:
        cik = int(r["cik"])
        accession = r["accession"]
        full_path = filing_clean_dir(clean_root, cik, accession) / "full.txt"
        if not full_path.exists():
            continue
        tokens = tokenize(full_path.read_text(encoding="utf-8"))
        if not tokens:
            continue
        df.update(set(tokens))
        n_docs += 1
        if n_docs % 1000 == 0:
            logger.info("DF pass: %d docs", n_docs)
    return dict(df), n_docs


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _section_paths(clean_root: Path, cik: int, accession: str) -> dict[str, Path]:
    d = filing_clean_dir(clean_root, cik, accession)
    return {
        "full": d / "full.txt",
        "risk_factors": d / "risk_factors.txt",
        "mdna": d / "mdna.txt",
    }


def _section_eligible(sections: dict, section: str) -> bool:
    """`full` runs whenever the filing is parse_status='ok'.
    `risk_factors` / `mdna` only when their own status is 'ok'."""
    if section == "full":
        full_block = sections.get("full") or {}
        return full_block.get("status", "ok") == "ok"
    block = sections.get(section) or {}
    return block.get("status") == "ok"


def score_filing(
    cik: int,
    accession: str,
    *,
    clean_root: Path,
    lm: dict[str, set[str]],
    df: dict[str, int],
    n_docs: int,
) -> list[dict]:
    """Returns up to 6 rows: 3 sections × 2 weightings, for sections where
    the section is 'ok'. Each row matches the filing_scores schema."""
    sections_path = filing_clean_dir(clean_root, cik, accession) / "sections.json"
    if not sections_path.exists():
        return []
    sec_meta = json.loads(sections_path.read_text())
    paths = _section_paths(clean_root, cik, accession)
    rows: list[dict] = []
    now = _utc_now()
    for section in SECTIONS:
        if not _section_eligible(sec_meta, section):
            continue
        path = paths[section]
        if not path.exists():
            continue
        tokens = tokenize(path.read_text(encoding="utf-8"))
        if not tokens:
            continue
        total_words = len(tokens)
        prop = proportional_scores(tokens, lm)
        tfidf = tfidf_scores(tokens, lm, df, n_docs)
        for weighting, scores in (("proportional", prop), ("tfidf", tfidf)):
            row = {
                "accession": accession,
                "section": section,
                "weighting": weighting,
                "scorer_version": SCORER_VERSION,
                "total_words": total_words,
                "scored_at": now,
            }
            for col, cat in CATEGORY_COLUMNS.items():
                row[col] = float(scores.get(cat, 0.0))
            rows.append(row)
    return rows


def _select_targets(
    conn: sqlite3.Connection,
    *,
    stack: str = "sp500",
    ciks: list[int] | None,
    force: bool,
) -> tuple[list[tuple[int, str]], int]:
    """Return ((cik, accession), skipped_count). Skipped = already scored
    at current SCORER_VERSION (any rows present)."""
    cur = conn.cursor()
    where = ["parse_status = 'ok'", "stack = ?"]
    params: list = [stack]
    if ciks:
        where.append(f"cik IN ({','.join('?' for _ in ciks)})")
        params.extend(int(c) for c in ciks)
    sql = (
        f"SELECT cik, accession FROM filings WHERE {' AND '.join(where)} "
        f"ORDER BY cik, accession"
    )
    rows = list(cur.execute(sql, params))
    if force:
        return [(int(r["cik"]), r["accession"]) for r in rows], 0

    # Skip filings that already have at least one row at the current scorer_version.
    # Re-scoring partial rows is cheap; we just want to avoid full re-runs.
    existing = {
        r["accession"]
        for r in cur.execute(
            "SELECT DISTINCT accession FROM filing_scores WHERE scorer_version = ?",
            (SCORER_VERSION,),
        )
    }
    targets = []
    skipped = 0
    for r in rows:
        if r["accession"] in existing:
            skipped += 1
            continue
        targets.append((int(r["cik"]), r["accession"]))
    return targets, skipped


def _insert_rows(conn: sqlite3.Connection, rows: list[dict]) -> None:
    if not rows:
        return
    cols = list(rows[0].keys())
    placeholders = ",".join("?" for _ in cols)
    sql = (
        f"INSERT OR REPLACE INTO filing_scores ({','.join(cols)}) VALUES ({placeholders})"
    )
    conn.executemany(sql, [tuple(r[c] for c in cols) for r in rows])


def score_all(
    conn: sqlite3.Connection,
    cfg: Config,
    *,
    stack: str = "sp500",
    ciks: list[int] | None = None,
    limit: int | None = None,
    force: bool = False,
    df_override: tuple[dict[str, int], int] | None = None,
) -> Counts:
    """Two-pass driver: build corpus DF from full.txt, then score every section.

    `df_override` lets callers (e.g. queried.py) reuse a DF computed elsewhere
    (typically from the S&P corpus, so queried-stack scores share IDF with the
    benchmark set). When None, computes DF over the target stack.
    """
    from .config import VALID_STACKS, stack_clean
    if stack not in VALID_STACKS:
        raise ValueError(f"unknown stack {stack!r}; expected one of {VALID_STACKS}")
    clean_root = stack_clean(cfg, stack)
    counts = Counts()

    targets, skipped = _select_targets(conn, stack=stack, ciks=ciks, force=force)
    counts.skipped = skipped
    if not targets:
        logger.info("Nothing to score (skipped=%d, no targets).", skipped)
        return counts

    lm = load_master_dictionary(cfg.paths.lm_dictionary)

    if df_override is not None:
        df, n_docs = df_override
        logger.info("Using supplied DF table: %d tokens over %d documents.", len(df), n_docs)
    else:
        logger.info("Pass 1: computing document frequencies over full.txt corpus (stack=%s)...", stack)
        df, n_docs = compute_doc_frequencies(conn, cfg, stack=stack, ciks=ciks)
        logger.info("DF table: %d unique tokens over %d documents.", len(df), n_docs)

    # Pass 2: score targets.
    logger.info("Pass 2: scoring %d filings (skipped %d already at v%s).",
                len(targets), skipped, SCORER_VERSION)
    for idx, (cik, accession) in enumerate(targets, start=1):
        try:
            rows = score_filing(
                cik, accession,
                clean_root=clean_root, lm=lm, df=df, n_docs=n_docs,
            )
        except Exception as exc:
            logger.error("Score failed for %s/%s: %s", cik, accession, exc)
            counts.errors += 1
            continue
        _insert_rows(conn, rows)
        counts.processed += 1
        counts.rows_written += len(rows)
        if idx % 500 == 0 or idx == len(targets):
            conn.commit()
            logger.info(
                "[%d/%d] processed=%d rows=%d errors=%d",
                idx, len(targets), counts.processed, counts.rows_written, counts.errors,
            )
        if limit is not None and counts.processed >= limit:
            logger.info("Reached --limit %d; stopping.", limit)
            break
    conn.commit()
    return counts


# --- Diagnostics ---------------------------------------------------------------

def compute_stats(conn: sqlite3.Connection) -> dict:
    """Per-section / per-weighting summary of scored filings."""
    cur = conn.cursor()
    out: dict = {"by_section_weighting": {}, "totals": {}}
    rows = list(cur.execute(
        "SELECT section, weighting, COUNT(*) AS n, "
        "AVG(neg) AS neg, AVG(pos) AS pos, AVG(unc) AS unc, AVG(lit) AS lit, "
        "AVG(strong_modal) AS strong_modal, AVG(weak_modal) AS weak_modal, "
        "AVG(constraining) AS constraining, AVG(total_words) AS avg_words "
        "FROM filing_scores WHERE scorer_version = ? "
        "GROUP BY section, weighting ORDER BY section, weighting",
        (SCORER_VERSION,),
    ))
    for r in rows:
        key = f"{r['section']}/{r['weighting']}"
        out["by_section_weighting"][key] = {
            "n": int(r["n"]),
            "avg_total_words": int(r["avg_words"] or 0),
            "avg_neg": round(r["neg"] or 0.0, 5),
            "avg_pos": round(r["pos"] or 0.0, 5),
            "avg_unc": round(r["unc"] or 0.0, 5),
            "avg_lit": round(r["lit"] or 0.0, 5),
            "avg_strong_modal": round(r["strong_modal"] or 0.0, 5),
            "avg_weak_modal": round(r["weak_modal"] or 0.0, 5),
            "avg_constraining": round(r["constraining"] or 0.0, 5),
        }
    total_rows = cur.execute(
        "SELECT COUNT(*) FROM filing_scores WHERE scorer_version = ?",
        (SCORER_VERSION,),
    ).fetchone()[0]
    distinct_filings = cur.execute(
        "SELECT COUNT(DISTINCT accession) FROM filing_scores WHERE scorer_version = ?",
        (SCORER_VERSION,),
    ).fetchone()[0]
    out["totals"] = {
        "rows": int(total_rows),
        "distinct_filings": int(distinct_filings),
        "scorer_version": SCORER_VERSION,
    }
    return out


def render_stats(stats: dict) -> str:
    out: list[str] = []
    t = stats["totals"]
    out.append(f"scorer_version: {t['scorer_version']}")
    out.append(f"  rows:              {t['rows']}")
    out.append(f"  distinct filings:  {t['distinct_filings']}")
    out.append("")
    for key, s in stats["by_section_weighting"].items():
        out.append(f"== {key}  (n={s['n']}, avg words={s['avg_total_words']}) ==")
        out.append(
            f"  neg={s['avg_neg']:.5f}  pos={s['avg_pos']:.5f}  unc={s['avg_unc']:.5f}  "
            f"lit={s['avg_lit']:.5f}"
        )
        out.append(
            f"  strong_modal={s['avg_strong_modal']:.5f}  "
            f"weak_modal={s['avg_weak_modal']:.5f}  "
            f"constraining={s['avg_constraining']:.5f}"
        )
        out.append("")
    return "\n".join(out)
