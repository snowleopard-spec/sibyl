#!/usr/bin/env python3
"""Re-classify short Stage-3 section extractions under the patched
INCORP_REF_RE regex. Only affects filings whose mdna.txt or
risk_factors.txt is short enough to plausibly be an incorporation-by-
reference stub (< INCORP_REF_MAX_WORDS).

The text on disk is unchanged — only the section status in sections.json
and the filing's parse_status in sibyl.db are updated.

Usage:
  python scripts/remediate_incorp_ref.py [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

from sibyl.config import load_config
from sibyl.parse import filing_clean_dir
from sibyl.sections import (
    INCORP_REF_MAX_WORDS,
    SECTIONS,
    _section_status,
)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--dry-run", action="store_true",
                    help="Report changes that would be made; do not write.")
    args = ap.parse_args()

    cfg = load_config(args.config)
    clean_root = cfg.paths.clean
    conn = sqlite3.connect(cfg.paths.db)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    changes: list[tuple[int, str, str, str, str]] = []  # cik, acc, section, old_status, new_status
    filings_now_section_fail: set[str] = set()

    for cik_dir in sorted(clean_root.iterdir()):
        if not cik_dir.is_dir():
            continue
        try:
            cik = int(cik_dir.name)
        except ValueError:
            continue
        for acc_dir in cik_dir.iterdir():
            sec_path = acc_dir / "sections.json"
            if not sec_path.exists():
                continue
            try:
                sec = json.loads(sec_path.read_text())
            except Exception:
                continue

            full_wc = (sec.get("full") or {}).get("word_count", 0)
            dirty = False
            for sect in SECTIONS:
                block = sec.get(sect) or {}
                if block.get("status") != "ok":
                    continue
                if block.get("word_count", 0) >= INCORP_REF_MAX_WORDS:
                    continue
                text_path = acc_dir / f"{sect}.txt"
                if not text_path.exists():
                    continue
                text = text_path.read_text(encoding="utf-8")
                new_status, info = _section_status(text, full_word_count=full_wc)
                if new_status == block.get("status"):
                    continue
                changes.append((cik, acc_dir.name, sect, block.get("status"), new_status))
                sec[sect] = {"status": new_status, **info}
                dirty = True

            if dirty:
                # Re-derive filing-level status
                rf_st = (sec.get("risk_factors") or {}).get("status")
                mdna_st = (sec.get("mdna") or {}).get("status")
                full_st = (sec.get("full") or {}).get("status", "ok")
                if full_st != "ok":
                    sec["status"] = full_st
                elif rf_st == "ok" and mdna_st == "ok":
                    sec["status"] = "ok"
                else:
                    sec["status"] = "section_fail"

                if sec["status"] != "ok":
                    filings_now_section_fail.add(acc_dir.name)

                if not args.dry_run:
                    sec_path.write_text(json.dumps(sec, indent=2, sort_keys=True))

    # Update DB parse_status for filings that flipped to section_fail
    if not args.dry_run and filings_now_section_fail:
        for acc in sorted(filings_now_section_fail):
            cur.execute(
                "UPDATE filings SET parse_status = 'section_fail' WHERE accession = ? AND parse_status = 'ok'",
                (acc,),
            )
        conn.commit()

    # Report
    by_section: dict[str, int] = {s: 0 for s in SECTIONS}
    by_new_status: dict[str, int] = {}
    for _cik, _acc, sect, _old, new in changes:
        by_section[sect] += 1
        by_new_status[new] = by_new_status.get(new, 0) + 1

    print(f"Section-block re-classifications: {len(changes)}")
    for s in SECTIONS:
        print(f"  {s}: {by_section[s]}")
    print(f"New status counts: {by_new_status}")
    print(f"Filings flipped to section_fail: {len(filings_now_section_fail)}")
    if args.dry_run:
        print("(dry-run — no files or DB rows changed)")
    if changes and len(changes) <= 30:
        print("\nDetails:")
        for cik, acc, sect, old, new in changes:
            print(f"  CIK {cik:<10} / {acc:<22} {sect:<13} {old} -> {new}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
