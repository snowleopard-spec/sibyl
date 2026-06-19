#!/usr/bin/env python3
"""Aggregate per-batch JSONL verdicts produced by the audit subagents into
a single report.

Reads:  <audit_dir>/verdicts_batch_*.jsonl
Writes: <audit_dir>/audit.json   (full machine-readable result)
        <audit_dir>/audit.csv    (per-filing verdicts, spreadsheet-friendly)
Prints: aggregate summary + failure list

Usage:
  python scripts/aggregate_audit.py data/audits/<stamp>/
"""
from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from pathlib import Path

VERDICTS = ("clean", "partial", "wrong")
SECTIONS = ("risk_factors", "mdna")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("audit_dir", help="Directory containing verdicts_batch_*.jsonl")
    args = ap.parse_args()

    audit_dir = Path(args.audit_dir).resolve()
    batch_files = sorted(audit_dir.glob("verdicts_batch_*.jsonl"))
    if not batch_files:
        print(f"No verdicts_batch_*.jsonl in {audit_dir}")
        return 1

    rows: list[dict] = []
    for bf in batch_files:
        for line in bf.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            rows.append(json.loads(line))

    # Aggregate
    per_section: dict[str, Counter] = {s: Counter() for s in SECTIONS}
    combined_clean = 0
    failures: list[tuple[str, str, str, str]] = []  # (cik, accession, section, reason)

    for r in rows:
        clean_both = True
        for s in SECTIONS:
            verdict = r[s]["verdict"]
            per_section[s][verdict] += 1
            if verdict != "clean":
                failures.append((str(r["cik"]), r["accession"], s, f"{verdict}: {r[s]['reason']}"))
                clean_both = False
        if clean_both:
            combined_clean += 1

    n = len(rows)

    # Console summary
    print(f"\nAudit of {n} both-ok filings\n")
    for s in SECTIONS:
        c = per_section[s]
        line = "  ".join(f"{v}={c[v]}" for v in VERDICTS)
        print(f"  {s:<13} {line}")
    print(f"\n  combined clean: {combined_clean}/{n} ({combined_clean/n:.1%})\n")

    gate = combined_clean / n
    print(f"  Gate (≥90% combined-clean): {'PASS' if gate >= 0.90 else 'FAIL'} ({gate:.1%})\n")

    if failures:
        print(f"Failures ({len(failures)}):")
        for cik, acc, sect, why in failures:
            print(f"  CIK {cik:<10} / {acc:<22} {sect:<13} {why}")
        print()

    # Write audit.json
    audit_json = {
        "audit_dir": str(audit_dir),
        "n_sampled": n,
        "judge": "claude-code subagents (general-purpose) — model resolves to whatever powers the harness",
        "per_section": {s: dict(per_section[s]) for s in SECTIONS},
        "combined_clean": combined_clean,
        "combined_clean_rate": round(combined_clean / n, 4),
        "gate_pass": gate >= 0.90,
        "results": rows,
    }
    (audit_dir / "audit.json").write_text(json.dumps(audit_json, indent=2))

    # Write audit.csv
    csv_path = audit_dir / "audit.csv"
    with csv_path.open("w", newline="") as f:
        w = csv.DictWriter(
            f,
            fieldnames=[
                "cik", "accession",
                "rf_verdict", "rf_reason",
                "mdna_verdict", "mdna_reason",
                "both_clean",
            ],
        )
        w.writeheader()
        for r in rows:
            w.writerow(
                {
                    "cik": r["cik"],
                    "accession": r["accession"],
                    "rf_verdict": r["risk_factors"]["verdict"],
                    "rf_reason": r["risk_factors"]["reason"],
                    "mdna_verdict": r["mdna"]["verdict"],
                    "mdna_reason": r["mdna"]["reason"],
                    "both_clean": (
                        r["risk_factors"]["verdict"] == "clean"
                        and r["mdna"]["verdict"] == "clean"
                    ),
                }
            )

    print(f"Wrote {audit_dir/'audit.json'}")
    print(f"Wrote {csv_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
