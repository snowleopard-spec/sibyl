"""One-shot migration: recompute filing-level status under the v2 taxonomy.

The v2 taxonomy (introduced 2026-06-21) splits the old binary ok/section_fail
into ok/partial/section_fail. `partial` covers filings where one section
extracted cleanly and the other is a legitimate non-extraction pattern
(missing or incorp_ref — content referenced in another document).
`over_extracted` (boundary detection bug) stays under `section_fail`.

Run from project root: `.venv/bin/python scripts/relabel_sections_v2.py`
"""
from __future__ import annotations

import argparse
import collections
import json
import sqlite3
import sys
from pathlib import Path

from sibyl import config as config_mod
from sibyl.parse import filing_clean_dir
from sibyl.sections import _compute_filing_status, EXTRACTOR_VERSION


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--stack", default="sp500", choices=("sp500", "queried"))
    ap.add_argument("--dry-run", action="store_true",
                    help="Compute the delta but don't write changes.")
    args = ap.parse_args()

    cfg = config_mod.load_config(args.config)
    clean_root = config_mod.stack_clean(cfg, args.stack)
    conn = sqlite3.connect(cfg.paths.db)
    conn.row_factory = sqlite3.Row

    rows = list(conn.execute(
        "SELECT cik, accession, parse_status FROM filings "
        "WHERE stack = ? AND parse_status IN ('ok', 'partial', 'section_fail')",
        (args.stack,),
    ))
    print(f"Considering {len(rows)} filings in stack={args.stack}")

    transitions: collections.Counter = collections.Counter()
    json_updates = 0
    db_updates = 0
    missing_json = 0
    no_change = 0

    for r in rows:
        cik, acc, old_status = int(r["cik"]), r["accession"], r["parse_status"]
        sp = filing_clean_dir(clean_root, cik, acc) / "sections.json"
        if not sp.exists():
            missing_json += 1
            continue
        try:
            sec = json.loads(sp.read_text())
        except Exception:
            missing_json += 1
            continue

        # Skip filings that aren't at the current extractor version. They'll
        # get the new status next time sections is re-run on them.
        if sec.get("section_extractor_version") != EXTRACTOR_VERSION and \
           sec.get("section_extractor_version") != "1":
            # Untouched by section extraction — leave alone.
            continue

        rf_status = (sec.get("risk_factors") or {}).get("status", "missing")
        md_status = (sec.get("mdna") or {}).get("status", "missing")
        full_status = (sec.get("full") or {}).get("status", "ok")
        new_status = _compute_filing_status(rf_status, md_status, full_status=full_status)

        transitions[(old_status, new_status)] += 1

        if new_status == old_status and sec.get("status") == new_status \
                and sec.get("section_extractor_version") == EXTRACTOR_VERSION:
            no_change += 1
            continue

        # Update sections.json: bump extractor_version, set new status.
        sec["status"] = new_status
        sec["section_extractor_version"] = EXTRACTOR_VERSION
        if not args.dry_run:
            sp.write_text(json.dumps(sec, indent=2, sort_keys=True))
        json_updates += 1

        if new_status != old_status:
            if not args.dry_run:
                conn.execute(
                    "UPDATE filings SET parse_status = ? WHERE accession = ?",
                    (new_status, acc),
                )
            db_updates += 1

    if not args.dry_run:
        conn.commit()

    print(f"\nResult:")
    print(f"  sections.json rewritten: {json_updates}")
    print(f"  DB parse_status changes: {db_updates}")
    print(f"  no_change (already current): {no_change}")
    print(f"  missing/unreadable sections.json: {missing_json}")
    print(f"\nStatus transitions (old → new):")
    for (old, new), n in sorted(transitions.items(), key=lambda x: -x[1]):
        marker = " " if old == new else "→"
        print(f"  {marker} {old:>12s} → {new:<12s}  {n}")
    if args.dry_run:
        print("\n(dry run — no writes performed)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
