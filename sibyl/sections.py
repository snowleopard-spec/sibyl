"""Stage 3 — Item 1A (Risk Factors) and Item 7 (MD&A) isolation via edgartools."""
from __future__ import annotations

import csv
import gzip
import json
import logging
import os
import random
import re
import sqlite3
import tempfile
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

import edgar
from edgar.company_reports import TenK

from .config import Config
from .parse import filing_clean_dir

logger = logging.getLogger(__name__)

EXTRACTOR_VERSION = "1"
EXTRACTOR_NAME = f"edgartools-{getattr(edgar, '__version__', 'unknown')}"

# Thresholds.
MIN_OK_WORDS = 1000
INCORP_REF_MAX_WORDS = 500
OVER_EXTRACTED_WORDS = 50_000
OVER_EXTRACTED_FULL_RATIO = 0.95
YOY_JUMP_RATIO = 5.0
INCORP_REF_RE = re.compile(
    r"\b(incorporated\s+by\s+reference|see\s+our\s+proxy|annual\s+proxy\s+statement)\b",
    re.IGNORECASE,
)

SECTIONS = ("risk_factors", "mdna")


@dataclass
class Counts:
    processed: int = 0
    both_ok: int = 0
    section_fail: int = 0
    suspicious: int = 0
    skipped: int = 0


class LocalFiling(edgar.Filing):
    """Bypass edgartools' network fetch by reading raw HTML from our local cache."""

    def __init__(self, *, cik: int, accession: str, html_path: Path):
        super().__init__(
            cik=int(cik), company="X", form="10-K",
            filing_date="2023-01-01", accession_no=accession,
        )
        self._html_path = Path(html_path)

    def html(self) -> str:
        with gzip.open(self._html_path, "rt", encoding="utf-8") as f:
            return f.read()


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", dir=path.parent, delete=False, encoding="utf-8") as tmp:
        tmp.write(text)
        tmp_path = Path(tmp.name)
    tmp_path.replace(path)


def _section_status(text: str | None, *, full_word_count: int) -> tuple[str, dict]:
    """Return (status, info_dict). info_dict has word_count, char_count, flags, head_excerpt."""
    text = text or ""
    word_count = len(text.split())
    char_count = len(text)
    flags: list[str] = []

    if char_count == 0:
        return "missing", {"word_count": 0, "char_count": 0, "suspicious_flags": [], "head_excerpt": ""}

    if word_count < INCORP_REF_MAX_WORDS and INCORP_REF_RE.search(text):
        return "incorp_ref", {
            "word_count": word_count, "char_count": char_count,
            "suspicious_flags": ["incorp_ref_phrase"], "head_excerpt": text[:300],
        }

    if word_count > OVER_EXTRACTED_WORDS:
        flags.append("length_high")
        return "over_extracted", {
            "word_count": word_count, "char_count": char_count,
            "suspicious_flags": flags, "head_excerpt": text[:300],
        }

    # If the section accidentally captured ~everything (Photronics failure mode),
    # mark over_extracted even if length_high threshold wasn't crossed.
    if full_word_count > 0 and word_count > full_word_count * OVER_EXTRACTED_FULL_RATIO:
        flags.append("equal_to_full")
        return "over_extracted", {
            "word_count": word_count, "char_count": char_count,
            "suspicious_flags": flags, "head_excerpt": text[:300],
        }

    if word_count < MIN_OK_WORDS:
        flags.append("length_low")

    return "ok", {
        "word_count": word_count, "char_count": char_count,
        "suspicious_flags": flags, "head_excerpt": text[:300],
    }


def extract_sections(
    cik: int,
    accession: str,
    *,
    raw_root: Path,
    clean_root: Path,
) -> dict:
    """Run edgartools section extraction on one filing. Returns the merged sections.json dict."""
    raw_path = raw_root / str(int(cik)) / accession / "primary.html.gz"
    out_dir = filing_clean_dir(clean_root, cik, accession)
    sections_path = out_dir / "sections.json"

    # Load existing sections.json (must be there — Stage 2 wrote it).
    if not sections_path.exists():
        raise FileNotFoundError(f"missing sections.json for {cik}/{accession} — run Stage 2 first")
    sections = json.loads(sections_path.read_text())

    if not raw_path.exists():
        # Should be impossible since Stage 2 succeeded, but defend.
        sections["section_extractor_version"] = EXTRACTOR_VERSION
        sections["extractor"] = EXTRACTOR_NAME
        sections["risk_factors"] = {"status": "missing", "word_count": 0, "char_count": 0,
                                    "suspicious_flags": ["raw_missing"], "head_excerpt": ""}
        sections["mdna"] = {"status": "missing", "word_count": 0, "char_count": 0,
                            "suspicious_flags": ["raw_missing"], "head_excerpt": ""}
        sections["status"] = "section_fail"
        _atomic_write_text(sections_path, json.dumps(sections, indent=2, sort_keys=True))
        return sections

    full_word_count = (sections.get("full") or {}).get("word_count", 0)

    try:
        filing = LocalFiling(cik=cik, accession=accession, html_path=raw_path)
        tenk = TenK(filing)
        rf_text = tenk.risk_factors or ""
        mdna_text = tenk.management_discussion or ""
    except Exception as exc:
        logger.warning("extractor crashed on %s/%s: %s", cik, accession, exc)
        rf_text = ""
        mdna_text = ""

    rf_status, rf_info = _section_status(rf_text, full_word_count=full_word_count)
    mdna_status, mdna_info = _section_status(mdna_text, full_word_count=full_word_count)

    if rf_status == "ok":
        _atomic_write_text(out_dir / "risk_factors.txt", rf_text)
    if mdna_status == "ok":
        _atomic_write_text(out_dir / "mdna.txt", mdna_text)

    sections["section_extractor_version"] = EXTRACTOR_VERSION
    sections["extractor"] = EXTRACTOR_NAME
    sections["risk_factors"] = {"status": rf_status, **rf_info}
    sections["mdna"] = {"status": mdna_status, **mdna_info}

    full_status = (sections.get("full") or {}).get("status", "ok")
    if full_status != "ok":
        sections["status"] = full_status   # parse_fail still trumps
    elif rf_status == "ok" and mdna_status == "ok":
        sections["status"] = "ok"
    else:
        sections["status"] = "section_fail"

    sections["parsed_at"] = _utc_now()
    _atomic_write_text(sections_path, json.dumps(sections, indent=2, sort_keys=True))
    return sections


def _apply_yoy_flags(conn: sqlite3.Connection, clean_root: Path) -> dict[str, int]:
    """Post-pass: per CIK, for each section, flag yoy length jumps > YOY_JUMP_RATIO."""
    flagged: dict[str, int] = {"risk_factors": 0, "mdna": 0}
    cur = conn.cursor()
    rows = list(cur.execute(
        "SELECT cik, accession, filing_date FROM filings "
        "WHERE parse_status IN ('ok', 'section_fail') "
        "ORDER BY cik, filing_date"
    ))
    by_cik: dict[int, list[tuple[str, str, dict]]] = {}
    for r in rows:
        sections_path = filing_clean_dir(clean_root, int(r["cik"]), r["accession"]) / "sections.json"
        if not sections_path.exists():
            continue
        try:
            sec = json.loads(sections_path.read_text())
        except Exception:
            continue
        by_cik.setdefault(int(r["cik"]), []).append((r["accession"], r["filing_date"], sec))

    for cik, items in by_cik.items():
        items.sort(key=lambda x: x[1] or "")
        for i in range(1, len(items)):
            cur_acc, _, cur_sec = items[i]
            _prev_acc, _, prev_sec = items[i - 1]
            changed = False
            for sect in SECTIONS:
                cur_wc = (cur_sec.get(sect) or {}).get("word_count") or 0
                prev_wc = (prev_sec.get(sect) or {}).get("word_count") or 0
                if not cur_wc or not prev_wc:
                    continue
                ratio = max(cur_wc, prev_wc) / max(min(cur_wc, prev_wc), 1)
                if ratio > YOY_JUMP_RATIO:
                    flags = list((cur_sec.get(sect) or {}).get("suspicious_flags", []))
                    if "yoy_jump" not in flags:
                        flags.append("yoy_jump")
                        cur_sec[sect]["suspicious_flags"] = flags
                        changed = True
                        flagged[sect] += 1
            if changed:
                sections_path = filing_clean_dir(clean_root, cik, cur_acc) / "sections.json"
                _atomic_write_text(sections_path, json.dumps(cur_sec, indent=2, sort_keys=True))
    return flagged


def _default_workers() -> int:
    """Default worker count: leave one core for the parent + OS."""
    return min(8, max(1, (os.cpu_count() or 1) - 1))


def _silence_edgartools() -> None:
    """Worker-pool initializer — dial down edgartools' chatty INFO logs in children."""
    logging.getLogger("edgar").setLevel(logging.WARNING)


def _extract_worker(args: tuple[int, str, str, str]) -> dict:
    """Pure-function task run in a worker process. No DB, no shared state."""
    cik, accession, raw_root_str, clean_root_str = args
    try:
        sec = extract_sections(
            cik, accession,
            raw_root=Path(raw_root_str),
            clean_root=Path(clean_root_str),
        )
        suspicious = any((sec.get(s) or {}).get("suspicious_flags") for s in SECTIONS)
        return {
            "accession": accession,
            "status": sec.get("status"),
            "suspicious": suspicious,
            "error": None,
        }
    except Exception as exc:
        return {
            "accession": accession,
            "status": None,
            "suspicious": False,
            "error": f"{type(exc).__name__}: {exc}",
        }


def _select_targets(
    conn: sqlite3.Connection,
    cfg: Config,
    *,
    ciks: list[int] | None = None,
    force: bool = False,
) -> tuple[list[tuple[int, str]], int]:
    """Pre-filter: return ([(cik, accession), ...], skipped_count). Skipped = already
    at current EXTRACTOR_VERSION."""
    cur = conn.cursor()
    where = ["parse_status = 'ok'"]
    params: list = []
    if ciks:
        where.append(f"cik IN ({','.join('?' for _ in ciks)})")
        params.extend(int(c) for c in ciks)
    sql = f"SELECT cik, accession FROM filings WHERE {' AND '.join(where)} ORDER BY cik, accession"
    rows = list(cur.execute(sql, params))

    targets: list[tuple[int, str]] = []
    skipped = 0
    for r in rows:
        cik = int(r["cik"])
        accession = r["accession"]
        if not force:
            sections_path = filing_clean_dir(cfg.paths.clean, cik, accession) / "sections.json"
            if sections_path.exists():
                try:
                    existing = json.loads(sections_path.read_text())
                    if existing.get("section_extractor_version") == EXTRACTOR_VERSION:
                        skipped += 1
                        continue
                except Exception:
                    pass
        targets.append((cik, accession))
    return targets, skipped


def extract_all(
    conn: sqlite3.Connection,
    cfg: Config,
    *,
    ciks: list[int] | None = None,
    limit: int | None = None,
    force: bool = False,
    workers: int | None = None,
) -> Counts:
    counts = Counts()
    targets, skipped = _select_targets(conn, cfg, ciks=ciks, force=force)
    counts.skipped = skipped
    total = len(targets)

    if total == 0:
        logger.info("Nothing to extract (skipped=%d, no targets at current version).", skipped)
        yoy = _apply_yoy_flags(conn, cfg.paths.clean)
        logger.info("yoy_jump flags applied: risk_factors=%d, mdna=%d",
                    yoy["risk_factors"], yoy["mdna"])
        return counts

    workers = workers if workers is not None else _default_workers()
    logger.info("Extracting %d filings with %d worker(s) (skipped %d at current version)",
                total, workers, skipped)
    args_list = [(cik, acc, str(cfg.paths.raw), str(cfg.paths.clean)) for cik, acc in targets]

    cur = conn.cursor()

    def _handle(idx: int, r: dict, total: int) -> bool:
        """Update counts + DB from one worker result. Returns True when --limit hit."""
        if r["error"]:
            logger.error("Worker error on %s: %s", r["accession"], r["error"])
            counts.section_fail += 1
        else:
            counts.processed += 1
            status = r["status"]
            if status == "ok":
                counts.both_ok += 1
            elif status == "section_fail":
                counts.section_fail += 1
            if r["suspicious"]:
                counts.suspicious += 1
            db_status = status if status in ("ok", "section_fail", "parse_fail") else "section_fail"
            cur.execute(
                "UPDATE filings SET parse_status = ? WHERE accession = ?",
                (db_status, r["accession"]),
            )
        if idx % 200 == 0 or idx == total:
            conn.commit()
            logger.info(
                "[%d/%d] processed=%d ok=%d section_fail=%d suspicious=%d",
                idx, total, counts.processed, counts.both_ok,
                counts.section_fail, counts.suspicious,
            )
        return limit is not None and counts.processed >= limit

    if workers <= 1:
        # Inline path — used in tests and when --workers 1. No pool overhead.
        for idx, a in enumerate(args_list, start=1):
            r = _extract_worker(a)
            stop = _handle(idx, r, total)
            if stop:
                logger.info("Reached --limit %d; stopping.", limit)
                break
    else:
        with ProcessPoolExecutor(max_workers=workers, initializer=_silence_edgartools) as pool:
            futures = {pool.submit(_extract_worker, a): a for a in args_list}
            for idx, fut in enumerate(as_completed(futures), start=1):
                r = fut.result()
                stop = _handle(idx, r, total)
                if stop:
                    logger.info("Reached --limit %d; cancelling remaining workers.", limit)
                    for f in futures:
                        f.cancel()
                    break

    conn.commit()
    yoy = _apply_yoy_flags(conn, cfg.paths.clean)
    logger.info("yoy_jump flags applied: risk_factors=%d, mdna=%d",
                yoy["risk_factors"], yoy["mdna"])
    return counts


# --- Diagnostics helpers (--stats / --sample / --pick-validation-set / --validate) ---

def _iter_sections(clean_root: Path) -> Iterable[tuple[int, str, dict]]:
    if not clean_root.exists():
        return
    for cik_dir in clean_root.iterdir():
        if not cik_dir.is_dir():
            continue
        try:
            cik = int(cik_dir.name)
        except ValueError:
            continue
        for acc_dir in cik_dir.iterdir():
            sec = acc_dir / "sections.json"
            if not sec.exists():
                continue
            try:
                yield cik, acc_dir.name, json.loads(sec.read_text())
            except Exception:
                continue


def _percentile(data: list[int], p: float) -> int:
    if not data:
        return 0
    s = sorted(data)
    k = (len(s) - 1) * (p / 100)
    f = int(k)
    c = min(f + 1, len(s) - 1)
    return int(s[f] + (s[c] - s[f]) * (k - f))


def compute_stats(conn: sqlite3.Connection, clean_root: Path) -> dict:
    per_section_status: dict[str, dict[str, int]] = {s: {} for s in SECTIONS}
    per_section_words: dict[str, list[int]] = {s: [] for s in SECTIONS}
    per_section_flags: dict[str, dict[str, int]] = {s: {} for s in SECTIONS}
    by_year_ok: dict[str, dict[int, int]] = {s: {} for s in SECTIONS}
    by_year_total: dict[int, int] = {}
    both_ok = section_fail = 0

    # Date lookup
    cur = conn.cursor()
    date_by_acc = {r["accession"]: r["filing_date"] for r in cur.execute(
        "SELECT accession, filing_date FROM filings"
    )}

    for cik, accession, sec in _iter_sections(clean_root):
        if sec.get("section_extractor_version") != EXTRACTOR_VERSION:
            continue
        year = None
        fd = date_by_acc.get(accession)
        if fd:
            try:
                year = int(fd[:4])
            except ValueError:
                pass
        if year is not None:
            by_year_total[year] = by_year_total.get(year, 0) + 1

        if sec.get("status") == "ok":
            both_ok += 1
        elif sec.get("status") == "section_fail":
            section_fail += 1

        for sect in SECTIONS:
            block = sec.get(sect) or {}
            st = block.get("status", "missing")
            per_section_status[sect][st] = per_section_status[sect].get(st, 0) + 1
            if st == "ok":
                wc = int(block.get("word_count") or 0)
                if wc > 0:
                    per_section_words[sect].append(wc)
                if year is not None:
                    by_year_ok[sect][year] = by_year_ok[sect].get(year, 0) + 1
            for f in block.get("suspicious_flags") or []:
                per_section_flags[sect][f] = per_section_flags[sect].get(f, 0) + 1

    # Year-bucket rates
    by_year_rate: dict[str, dict[int, float]] = {s: {} for s in SECTIONS}
    for sect in SECTIONS:
        for y, tot in by_year_total.items():
            ok = by_year_ok[sect].get(y, 0)
            by_year_rate[sect][y] = round(ok / tot, 3) if tot else 0.0

    return {
        "both_ok": both_ok,
        "section_fail": section_fail,
        "per_section_status": per_section_status,
        "per_section_words": {
            s: ({
                "n": len(per_section_words[s]),
                "min": min(per_section_words[s]) if per_section_words[s] else 0,
                "p5": _percentile(per_section_words[s], 5),
                "p50": _percentile(per_section_words[s], 50),
                "p95": _percentile(per_section_words[s], 95),
                "p99": _percentile(per_section_words[s], 99),
                "max": max(per_section_words[s]) if per_section_words[s] else 0,
            }) for s in SECTIONS
        },
        "per_section_flags": per_section_flags,
        "by_year_rate": by_year_rate,
    }


def render_stats(stats: dict) -> str:
    out = []
    total = stats["both_ok"] + stats["section_fail"]
    out.append(f"Total processed (at current extractor version): {total}")
    out.append(f"  both ok:      {stats['both_ok']}")
    out.append(f"  section_fail: {stats['section_fail']}")
    out.append("")
    for sect in SECTIONS:
        out.append(f"== {sect} ==")
        st = stats["per_section_status"][sect]
        for k in ("ok", "missing", "incorp_ref", "over_extracted"):
            out.append(f"  status={k:<15} {st.get(k, 0)}")
        wc = stats["per_section_words"][sect]
        out.append(f"  word counts (n={wc['n']}) min/P5/P50/P95/P99/max: "
                   f"{wc['min']}/{wc['p5']}/{wc['p50']}/{wc['p95']}/{wc['p99']}/{wc['max']}")
        flags = stats["per_section_flags"][sect]
        if flags:
            out.append("  flags:")
            for k, v in sorted(flags.items(), key=lambda x: -x[1]):
                out.append(f"    {k:<20} {v}")
        else:
            out.append("  flags: (none)")
        out.append("  ok-rate by year:")
        for y in sorted(stats["by_year_rate"][sect].keys()):
            r = stats["by_year_rate"][sect][y]
            warn = "  ← LOW" if r < 0.6 else ""
            out.append(f"    {y}: {r:.2f}{warn}")
        out.append("")
    return "\n".join(out)


def pick_samples(clean_root: Path, n: int, *, suspicious_only: bool, seed: int | None = None) -> list[tuple[int, str, dict]]:
    candidates = []
    for _cik, accession, sec in _iter_sections(clean_root):
        if sec.get("section_extractor_version") != EXTRACTOR_VERSION:
            continue
        if sec.get("status") not in ("ok", "section_fail"):
            continue
        flagged = any((sec.get(s) or {}).get("suspicious_flags") for s in SECTIONS) or sec.get("status") == "section_fail"
        if suspicious_only and not flagged:
            continue
        if (not suspicious_only) and flagged:
            continue
        candidates.append((_cik, accession, sec))
    rng = random.Random(seed)
    rng.shuffle(candidates)
    return candidates[:n]


def render_sample(clean_root: Path, cik: int, accession: str, sec: dict, *, head_chars: int = 1200, tail_chars: int = 800) -> str:
    parts = [f"=== CIK {cik} / {accession}  (status: {sec.get('status')}) ==="]
    for sect in SECTIONS:
        block = sec.get(sect) or {}
        wc = block.get("word_count", 0)
        st = block.get("status", "missing")
        flags = block.get("suspicious_flags") or []
        parts.append(f"--- {sect}: {st}, {wc} words, flags={flags} ---")
        path = filing_clean_dir(clean_root, cik, accession) / f"{sect if sect == 'mdna' else sect}.txt"
        if not path.exists():
            parts.append("(file not on disk)")
            continue
        text = path.read_text(encoding="utf-8")
        parts.append(text[:head_chars])
        if len(text) > head_chars + tail_chars:
            parts.append("...")
            parts.append(text[-tail_chars:])
    parts.append("")
    return "\n".join(parts)


def pick_validation_set(conn: sqlite3.Connection, clean_root: Path, n: int, *, seed: int = 0) -> Path:
    """Emit data/clean/validation_labels.csv spanning hard cases. Returns path."""
    rng = random.Random(seed)
    bucket_size = max(1, n // 4)

    cur = conn.cursor()
    accession_to_year: dict[str, str] = {}
    for r in cur.execute("SELECT cik, accession, filing_date FROM filings WHERE parse_status IN ('ok','section_fail')"):
        accession_to_year[r["accession"]] = r["filing_date"] or ""

    yoy_candidates: list[tuple[int, str]] = []
    oe_candidates: list[tuple[int, str]] = []
    ok_candidates: list[tuple[int, str]] = []
    for cik, accession, sec in _iter_sections(clean_root):
        if sec.get("section_extractor_version") != EXTRACTOR_VERSION:
            continue
        flags = [f for s in SECTIONS for f in (sec.get(s) or {}).get("suspicious_flags", [])]
        statuses = [(sec.get(s) or {}).get("status") for s in SECTIONS]
        if "yoy_jump" in flags:
            yoy_candidates.append((cik, accession))
        if "over_extracted" in statuses:
            oe_candidates.append((cik, accession))
        if sec.get("status") == "ok" and not flags:
            ok_candidates.append((cik, accession))

    # Oldest
    oldest = list(cur.execute(
        "SELECT cik, accession FROM filings WHERE parse_status IN ('ok','section_fail') "
        "ORDER BY filing_date ASC LIMIT ?", (bucket_size,)
    ))
    oldest_list = [(int(r["cik"]), r["accession"]) for r in oldest]

    rng.shuffle(yoy_candidates)
    rng.shuffle(oe_candidates)
    rng.shuffle(ok_candidates)
    picks = (yoy_candidates[:bucket_size] + oe_candidates[:bucket_size]
             + oldest_list + ok_candidates[:bucket_size])

    seen = set()
    rows = []
    for cik, accession in picks:
        if (cik, accession) in seen:
            continue
        seen.add((cik, accession))
        rows.append({
            "cik": cik,
            "accession": accession,
            "filing_date": accession_to_year.get(accession, ""),
            "risk_factors_start_substring": "",
            "risk_factors_end_substring": "",
            "mdna_start_substring": "",
            "mdna_end_substring": "",
            "notes": "",
        })
        if len(rows) >= n:
            break

    out_path = clean_root / "validation_labels.csv"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()) if rows else
                                ["cik","accession","filing_date","risk_factors_start_substring",
                                 "risk_factors_end_substring","mdna_start_substring",
                                 "mdna_end_substring","notes"])
        writer.writeheader()
        writer.writerows(rows)
    return out_path


def validate_against_labels(clean_root: Path, labels_csv: Path) -> dict:
    """Compare extractor output against hand labels. Returns accuracy report."""
    if not labels_csv.exists():
        raise FileNotFoundError(f"{labels_csv} not found — run --pick-validation-set first and fill it in.")
    rows = list(csv.DictReader(labels_csv.open()))
    results = {"per_section": {s: {"labelled": 0, "found": 0, "start_within_200": 0,
                                   "end_within_1000": 0} for s in SECTIONS},
               "per_row": []}

    for row in rows:
        cik = int(row["cik"])
        accession = row["accession"]
        sec_path = filing_clean_dir(clean_root, cik, accession) / "sections.json"
        if not sec_path.exists():
            continue
        sec = json.loads(sec_path.read_text())

        for sect in SECTIONS:
            start_key = f"{sect}_start_substring"
            end_key = f"{sect}_end_substring"
            start_sub = (row.get(start_key) or "").strip()
            end_sub = (row.get(end_key) or "").strip()
            if not start_sub and not end_sub:
                continue
            results["per_section"][sect]["labelled"] += 1

            block = sec.get(sect) or {}
            if block.get("status") != "ok":
                results["per_row"].append({"cik": cik, "accession": accession,
                                           "section": sect, "result": f"extractor status={block.get('status')}"})
                continue

            text_path = filing_clean_dir(clean_root, cik, accession) / f"{sect}.txt"
            if not text_path.exists():
                continue
            text = text_path.read_text(encoding="utf-8")
            results["per_section"][sect]["found"] += 1

            # Locate label substrings within full.txt (ground truth offsets).
            full_path = filing_clean_dir(clean_root, cik, accession) / "full.txt"
            if not full_path.exists():
                continue
            full_text = full_path.read_text(encoding="utf-8")
            label_start = full_text.find(start_sub) if start_sub else -1
            label_end = full_text.find(end_sub) if end_sub else -1

            # Detected start: where in full.txt does the extracted text begin?
            detected_start = full_text.find(text[:200]) if text else -1
            detected_end = (detected_start + len(text)) if detected_start >= 0 else -1

            row_result = {"cik": cik, "accession": accession, "section": sect,
                          "label_start": label_start, "label_end": label_end,
                          "detected_start": detected_start, "detected_end": detected_end}
            if label_start >= 0 and detected_start >= 0 and abs(label_start - detected_start) <= 200:
                results["per_section"][sect]["start_within_200"] += 1
                row_result["start_ok"] = True
            if label_end >= 0 and detected_end >= 0 and abs(label_end - detected_end) <= 1000:
                results["per_section"][sect]["end_within_1000"] += 1
                row_result["end_ok"] = True
            results["per_row"].append(row_result)

    return results


def render_validation(results: dict) -> str:
    out = []
    for sect in SECTIONS:
        r = results["per_section"][sect]
        n = r["labelled"]
        out.append(f"== {sect} ==")
        out.append(f"  labelled: {n}")
        if n:
            out.append(f"  recall:           {r['found']}/{n} = {r['found']/n:.2%}")
            out.append(f"  start within 200: {r['start_within_200']}/{n} = {r['start_within_200']/n:.2%}")
            out.append(f"  end within 1000:  {r['end_within_1000']}/{n} = {r['end_within_1000']/n:.2%}")
        out.append("")
    return "\n".join(out)
