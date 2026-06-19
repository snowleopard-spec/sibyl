#!/usr/bin/env python3
"""Prepare an LLM audit batch: sample N Stage-3-ok filings and write
prompt-shaped excerpt files for an LLM (or subagent fleet) to judge.

Outputs under data/audits/<stamp>/:
  rubric.md                                — judging instructions
  manifest.csv                             — idx, cik, accession, filing_date
  inputs/<idx>_<cik>_<accession>.txt       — per-filing excerpts

Run from the project root:
  python scripts/prepare_audit.py --n 100 --seed 42
  python scripts/prepare_audit.py --n 3 --cik 320193   # Apple smoke test
"""
from __future__ import annotations

import argparse
import csv
import json
import random
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from sibyl.config import load_config
from sibyl.parse import filing_clean_dir
from sibyl.sections import EXTRACTOR_VERSION

ITEM_1B_RE = re.compile(r"\bItem\s+1B\b", re.IGNORECASE)
ITEM_8_RE = re.compile(r"\bItem\s+8\b", re.IGNORECASE)

COVER_CHARS = 2000
NEAR_BEFORE = 750
NEAR_AFTER = 750
SECTION_HEAD = 2000
SECTION_TAIL = 1000


RUBRIC = """\
# LLM audit rubric — Sibyl Stage 3 extractions

You are auditing automated extractions of SEC 10-K sections from a
financial research pipeline. The extractor outputs two text files per
filing:

- risk_factors.txt — should contain Item 1A (Risk Factors)
- mdna.txt        — should contain Item 7 (Management's Discussion
                    and Analysis of Financial Condition and Results of
                    Operations)

For each section, judge whether the extracted text correctly represents
the corresponding Item of the 10-K. Return one of three verdicts:

- "clean":   starts at the right place, ends at the right place, and
             contents are coherent prose from that Item.
- "partial": correct section but with notable boundary issues —
             leading TOC entries, premature cutoff, signatures bleeding
             in at the end, etc.
- "wrong":   the extraction is not Item 1A / Item 7 at all, or is
             empty/junk.

For each input file you are given, you will see:
- COVER PAGE: first ~2000 chars of the filing's full text
- FULL_NEAR_ITEM_1B: ~1500 chars around the last "Item 1B" mention in
  the full filing (this is approximately where Item 1A should end)
- FULL_NEAR_ITEM_8: ~1500 chars around the last "Item 8" mention in
  the full filing (this is approximately where Item 7 should end)
- RISK_FACTORS_HEAD: first ~2000 chars of the extracted Risk Factors
- RISK_FACTORS_TAIL: last ~1000 chars of the extracted Risk Factors
- MDNA_HEAD: first ~2000 chars of the extracted MD&A
- MDNA_TAIL: last ~1000 chars of the extracted MD&A

The HEAD of each extraction should look like the start of that Item,
not a TOC line or a cover-page line. The TAIL should look like content
from late in that Item, not from a subsequent Item or the signatures.

For each filing, output one line of strict JSON (one JSON object per
line, no enclosing array):

{"accession": "<accession>", "risk_factors": {"verdict": "clean|partial|wrong", "reason": "<one sentence>"}, "mdna": {"verdict": "clean|partial|wrong", "reason": "<one sentence>"}}

Process every input file in the order given. Output one JSON line per
filing. Do not output anything else.
"""


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _last_match_span(pat: re.Pattern, text: str) -> tuple[int, int] | None:
    last = None
    for m in pat.finditer(text):
        last = m
    if last is None:
        return None
    return last.span()


def _excerpt_near(text: str, span: tuple[int, int], before: int, after: int) -> str:
    a, b = span
    lo = max(0, a - before)
    hi = min(len(text), b + after)
    return text[lo:hi]


def _build_excerpt_file(
    cik: int,
    accession: str,
    full_text: str,
    rf_text: str,
    mdna_text: str,
) -> str:
    parts: list[str] = [f"=== Filing CIK {cik} accession {accession} ===\n"]

    parts.append("--- COVER PAGE (first 2000 chars of full.txt) ---")
    parts.append(full_text[:COVER_CHARS])
    parts.append("")

    span_1b = _last_match_span(ITEM_1B_RE, full_text)
    parts.append("--- FULL_NEAR_ITEM_1B (last 'Item 1B' mention; 750c before/after) ---")
    parts.append(
        _excerpt_near(full_text, span_1b, NEAR_BEFORE, NEAR_AFTER)
        if span_1b is not None
        else "(no Item 1B mention found in full.txt)"
    )
    parts.append("")

    span_8 = _last_match_span(ITEM_8_RE, full_text)
    parts.append("--- FULL_NEAR_ITEM_8 (last 'Item 8' mention; 750c before/after) ---")
    parts.append(
        _excerpt_near(full_text, span_8, NEAR_BEFORE, NEAR_AFTER)
        if span_8 is not None
        else "(no Item 8 mention found in full.txt)"
    )
    parts.append("")

    parts.append("--- RISK_FACTORS_HEAD (first 2000 chars of risk_factors.txt) ---")
    parts.append(rf_text[:SECTION_HEAD])
    parts.append("")

    parts.append("--- RISK_FACTORS_TAIL (last 1000 chars of risk_factors.txt) ---")
    parts.append(
        rf_text[-SECTION_TAIL:]
        if len(rf_text) > SECTION_HEAD
        else "(extracted text shorter than head window; nothing additional)"
    )
    parts.append("")

    parts.append("--- MDNA_HEAD (first 2000 chars of mdna.txt) ---")
    parts.append(mdna_text[:SECTION_HEAD])
    parts.append("")

    parts.append("--- MDNA_TAIL (last 1000 chars of mdna.txt) ---")
    parts.append(
        mdna_text[-SECTION_TAIL:]
        if len(mdna_text) > SECTION_HEAD
        else "(extracted text shorter than head window; nothing additional)"
    )

    return "\n".join(parts) + "\n"


def _sample_targets(
    conn: sqlite3.Connection,
    clean_root: Path,
    n: int,
    seed: int,
    ciks: list[int] | None,
) -> list[tuple[int, str, str]]:
    """Return [(cik, accession, filing_date), ...] of up to n both-ok filings."""
    cur = conn.cursor()
    where = ["parse_status = 'ok'"]
    params: list = []
    if ciks:
        where.append(f"cik IN ({','.join('?' for _ in ciks)})")
        params.extend(int(c) for c in ciks)
    sql = f"SELECT cik, accession, filing_date FROM filings WHERE {' AND '.join(where)}"
    rows = list(cur.execute(sql, params))

    eligible: list[tuple[int, str, str]] = []
    for r in rows:
        cik = int(r["cik"])
        accession = r["accession"]
        sec_path = filing_clean_dir(clean_root, cik, accession) / "sections.json"
        if not sec_path.exists():
            continue
        try:
            sec = json.loads(sec_path.read_text())
        except Exception:
            continue
        if sec.get("section_extractor_version") != EXTRACTOR_VERSION:
            continue
        if (sec.get("risk_factors") or {}).get("status") != "ok":
            continue
        if (sec.get("mdna") or {}).get("status") != "ok":
            continue
        eligible.append((cik, accession, r["filing_date"] or ""))

    rng = random.Random(seed)
    rng.shuffle(eligible)
    return eligible[:n]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--n", type=int, default=100, help="Number of filings to sample.")
    ap.add_argument("--seed", type=int, default=42, help="RNG seed for sampling.")
    ap.add_argument(
        "--cik",
        type=int,
        action="append",
        help="Restrict sampling to specific CIK(s). Repeatable. Example: --cik 320193.",
    )
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument(
        "--out-dir",
        default=None,
        help="Override output dir. Default: <data_root>/audits/<stamp>/",
    )
    args = ap.parse_args()

    cfg = load_config(args.config)
    db_path = cfg.paths.db
    clean_root = cfg.paths.clean

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    targets = _sample_targets(conn, clean_root, args.n, args.seed, args.cik)
    if not targets:
        print("No eligible filings found.")
        return 1

    out_dir = (
        Path(args.out_dir).resolve()
        if args.out_dir
        else cfg.paths.data_root / "audits" / _stamp()
    )
    inputs_dir = out_dir / "inputs"
    inputs_dir.mkdir(parents=True, exist_ok=True)

    (out_dir / "rubric.md").write_text(RUBRIC, encoding="utf-8")

    manifest_rows = []
    for idx, (cik, accession, fdate) in enumerate(targets, start=1):
        cdir = filing_clean_dir(clean_root, cik, accession)
        full_text = (cdir / "full.txt").read_text(encoding="utf-8")
        rf_text = (cdir / "risk_factors.txt").read_text(encoding="utf-8")
        mdna_text = (cdir / "mdna.txt").read_text(encoding="utf-8")
        out_text = _build_excerpt_file(cik, accession, full_text, rf_text, mdna_text)
        fname = f"{idx:03d}_{cik}_{accession}.txt"
        (inputs_dir / fname).write_text(out_text, encoding="utf-8")
        manifest_rows.append(
            {
                "idx": idx,
                "cik": cik,
                "accession": accession,
                "filing_date": fdate,
                "input_file": f"inputs/{fname}",
            }
        )

    with (out_dir / "manifest.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(manifest_rows[0].keys()))
        w.writeheader()
        w.writerows(manifest_rows)

    print(f"Wrote {len(targets)} excerpts to {out_dir}")
    print(f"  rubric:   {out_dir/'rubric.md'}")
    print(f"  manifest: {out_dir/'manifest.csv'}")
    print(f"  inputs:   {inputs_dir}/  ({len(targets)} files)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
