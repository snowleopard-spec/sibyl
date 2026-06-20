from __future__ import annotations

import gzip
import json
import logging
import random
import re
import sqlite3
import tempfile
import unicodedata
import warnings
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from bs4 import BeautifulSoup, XMLParsedAsHTMLWarning

warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)

from .config import Config

logger = logging.getLogger(__name__)

PARSER_VERSION = "2"

# Inline-XBRL elements: drop their content entirely (header/metadata blocks)
# vs keep their text content (the wrappers around visible narrative numbers/strings).
_XBRL_TEXT_WRAPPERS = {"ix:nonfraction", "ix:nonnumeric", "ix:continuation"}

# Thresholds for status / suspicious flags (see plan §validation Layer 1).
MIN_OK_WORDS = 1000          # below this = parse_fail
LOW_WORD_FLAG = 5000
HIGH_WORD_FLAG = 250_000
LOW_ALPHA_RATIO = 0.5        # non-letter share
NON_ASCII_DENSE = 0.05

_WS_INLINE = re.compile(r"[ \t]+")
_WS_NEWLINE = re.compile(r"\n{2,}")

# NFKC doesn't ASCIIfy smart quotes / typographic dashes. Stage 5 yoy similarity
# is fragile to filings drifting between encodings, so normalize them explicitly.
_PUNCT_FOLD = str.maketrans({
    "‘": "'", "’": "'", "‚": "'", "‛": "'",
    "“": '"', "”": '"', "„": '"', "‟": '"',
    "–": "-", "—": "-", "−": "-",
    "…": "...",
    "\xa0": " ",
})


@dataclass
class Counts:
    parsed: int = 0
    skipped: int = 0
    failed: int = 0
    suspicious: int = 0


def clean_filing(raw_html: bytes | str) -> tuple[str, dict]:
    """Strip HTML/XBRL/tables/scripts and normalize. Returns (clean_text, stats_dict)."""
    soup = BeautifulSoup(raw_html, "lxml")
    # Drop <head> (metadata) and the structural noise blocks.
    for tag in soup.find_all(["head", "table", "script", "style", "noscript"]):
        tag.decompose()
    # Inline-XBRL: text wrappers get unwrapped (keep visible narrative); every
    # other namespaced element (ix:header, ix:references, ix:resources, ix:hidden,
    # link:schemaRef, xbrli:context, ...) is dropped wholesale.
    for tag in list(soup.find_all(True)):
        name = (tag.name or "").lower()
        if ":" not in name:
            continue
        if name in _XBRL_TEXT_WRAPPERS:
            tag.unwrap()
        else:
            tag.decompose()
    text = soup.get_text(separator=" ")
    text = unicodedata.normalize("NFKC", text)
    text = text.translate(_PUNCT_FOLD)
    text = _WS_INLINE.sub(" ", text)
    text = _WS_NEWLINE.sub("\n\n", text)
    text = text.strip()

    word_count = len(text.split())
    char_count = len(text)
    if char_count:
        alpha = sum(1 for c in text if c.isalpha())
        non_ascii = sum(1 for c in text if ord(c) > 127)
        non_letter_ratio = 1.0 - (alpha / char_count)
        non_ascii_ratio = non_ascii / char_count
    else:
        non_letter_ratio = 1.0
        non_ascii_ratio = 0.0

    flags: list[str] = []
    if word_count < LOW_WORD_FLAG:
        flags.append("word_count_low")
    if word_count > HIGH_WORD_FLAG:
        flags.append("word_count_high")
    if non_letter_ratio > LOW_ALPHA_RATIO:
        flags.append("low_alpha_ratio")
    if non_ascii_ratio > NON_ASCII_DENSE:
        flags.append("non_ascii_dense")

    return text, {
        "word_count": word_count,
        "char_count": char_count,
        "non_letter_ratio": round(non_letter_ratio, 4),
        "non_ascii_ratio": round(non_ascii_ratio, 4),
        "suspicious_flags": flags,
    }


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("wb", dir=path.parent, delete=False) as tmp:
        tmp.write(data)
        tmp_path = Path(tmp.name)
    tmp_path.replace(path)


def _atomic_write_text(path: Path, text: str) -> None:
    _atomic_write_bytes(path, text.encode("utf-8"))


def filing_clean_dir(clean_root: Path, cik: int, accession: str) -> Path:
    return clean_root / str(int(cik)) / accession


def parse_filing(
    cik: int,
    accession: str,
    *,
    raw_root: Path,
    clean_root: Path,
) -> dict:
    """Parse one filing. Returns the sections.json dict."""
    raw_path = raw_root / str(int(cik)) / accession / "primary.html.gz"
    raw_path_uncompressed = raw_root / str(int(cik)) / accession / "primary.html"
    out_dir = filing_clean_dir(clean_root, cik, accession)
    sections_path = out_dir / "sections.json"
    full_path = out_dir / "full.txt"

    if raw_path.exists():
        with gzip.open(raw_path, "rb") as f:
            raw = f.read()
    elif raw_path_uncompressed.exists():
        raw = raw_path_uncompressed.read_bytes()
    else:
        sections = {
            "parser_version": PARSER_VERSION,
            "parsed_at": _utc_now(),
            "status": "parse_fail",
            "error": "raw primary doc missing",
            "full": {"status": "parse_fail", "word_count": 0, "char_count": 0, "suspicious_flags": []},
        }
        _atomic_write_text(sections_path, json.dumps(sections, indent=2, sort_keys=True))
        return sections

    try:
        text, stats = clean_filing(raw)
    except Exception as exc:
        sections = {
            "parser_version": PARSER_VERSION,
            "parsed_at": _utc_now(),
            "status": "parse_fail",
            "error": f"{type(exc).__name__}: {exc}",
            "full": {"status": "parse_fail", "word_count": 0, "char_count": 0, "suspicious_flags": []},
        }
        _atomic_write_text(sections_path, json.dumps(sections, indent=2, sort_keys=True))
        return sections

    full_status = "ok" if stats["word_count"] >= MIN_OK_WORDS else "parse_fail"
    overall = full_status

    sections = {
        "parser_version": PARSER_VERSION,
        "section_extractor_version": None,
        "parsed_at": _utc_now(),
        "status": overall,
        "full": {
            "status": full_status,
            "word_count": stats["word_count"],
            "char_count": stats["char_count"],
            "non_letter_ratio": stats["non_letter_ratio"],
            "non_ascii_ratio": stats["non_ascii_ratio"],
            "suspicious_flags": stats["suspicious_flags"],
        },
    }

    # Write full.txt first, then sections.json (file-then-metadata invariant).
    if full_status == "ok":
        _atomic_write_text(full_path, text)
    _atomic_write_text(sections_path, json.dumps(sections, indent=2, sort_keys=True))
    return sections


def parse_all(
    conn: sqlite3.Connection,
    cfg: Config,
    *,
    stack: str = "sp500",
    ciks: list[int] | None = None,
    limit: int | None = None,
    force: bool = False,
) -> Counts:
    from .config import VALID_STACKS, stack_clean, stack_raw
    if stack not in VALID_STACKS:
        raise ValueError(f"unknown stack {stack!r}; expected one of {VALID_STACKS}")
    raw_root = stack_raw(cfg, stack)
    clean_root = stack_clean(cfg, stack)

    counts = Counts()
    cur = conn.cursor()
    where = ["stack = ?"]
    params: list = [stack]
    if not force:
        where.append("(parse_status IS NULL)")
    if ciks:
        where.append(f"cik IN ({','.join('?' for _ in ciks)})")
        params.extend(int(c) for c in ciks)
    sql = "SELECT accession, cik FROM filings WHERE " + " AND ".join(where) + " ORDER BY cik, accession"
    rows = list(cur.execute(sql, params))

    total = len(rows)
    for idx, r in enumerate(rows, start=1):
        accession = r["accession"]
        cik = int(r["cik"])
        try:
            sections = parse_filing(cik, accession, raw_root=raw_root, clean_root=clean_root)
        except Exception as exc:  # defensive; parse_filing already catches
            logger.error("Unexpected parse error CIK %s acc %s: %s", cik, accession, exc)
            counts.failed += 1
            continue

        status = sections["status"]
        cur.execute(
            "UPDATE filings SET parse_status = ? WHERE accession = ?",
            (status, accession),
        )
        if status == "ok":
            counts.parsed += 1
            if sections["full"].get("suspicious_flags"):
                counts.suspicious += 1
        else:
            counts.failed += 1

        if limit is not None and counts.parsed >= limit:
            logger.info("Reached --limit %d; stopping.", limit)
            conn.commit()
            return counts

        if idx % 200 == 0 or idx == total:
            conn.commit()
            logger.info(
                "[%d/%d] parsed=%d failed=%d suspicious=%d",
                idx, total, counts.parsed, counts.failed, counts.suspicious,
            )

    conn.commit()
    return counts


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --- Diagnostics helpers consumed by `sibyl parse --stats` / `--sample` ---

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


def compute_stats(conn: sqlite3.Connection, clean_root: Path) -> dict:
    ok = fail = suspicious = 0
    words: list[int] = []
    flag_tally: dict[str, int] = {}
    by_cik: dict[int, list[tuple[str, int]]] = {}
    accession_to_wc: list[tuple[int, str, int]] = []

    for cik, accession, sections in _iter_sections(clean_root):
        status = sections.get("status")
        full = sections.get("full") or {}
        wc = int(full.get("word_count") or 0)
        if status == "ok":
            ok += 1
            words.append(wc)
            accession_to_wc.append((cik, accession, wc))
            if full.get("suspicious_flags"):
                suspicious += 1
                for f in full["suspicious_flags"]:
                    flag_tally[f] = flag_tally.get(f, 0) + 1
            by_cik.setdefault(cik, []).append((accession, wc))
        else:
            fail += 1

    pct = lambda p: _percentile(words, p) if words else 0
    yoy_jumps = _yoy_length_jumps(conn, by_cik)

    return {
        "ok": ok,
        "fail": fail,
        "suspicious": suspicious,
        "word_count": {
            "min": min(words) if words else 0,
            "p5": pct(5),
            "p50": pct(50),
            "p95": pct(95),
            "p99": pct(99),
            "max": max(words) if words else 0,
        },
        "flag_tally": flag_tally,
        "shortest": sorted(accession_to_wc, key=lambda x: x[2])[:10],
        "longest": sorted(accession_to_wc, key=lambda x: -x[2])[:10],
        "yoy_jumps": yoy_jumps[:20],
    }


def _percentile(sorted_or_unsorted: list[int], p: float) -> int:
    if not sorted_or_unsorted:
        return 0
    data = sorted(sorted_or_unsorted)
    k = (len(data) - 1) * (p / 100)
    f = int(k)
    c = min(f + 1, len(data) - 1)
    return int(data[f] + (data[c] - data[f]) * (k - f))


def _yoy_length_jumps(conn: sqlite3.Connection, by_cik: dict[int, list[tuple[str, int]]]) -> list[tuple[int, str, str, int, int, float]]:
    """Per CIK, find accessions whose word_count differs from the prior 10-K by >5x.

    Returns list of (cik, accession, prior_accession, wc, prior_wc, ratio).
    """
    out = []
    cur = conn.cursor()
    for cik, items in by_cik.items():
        # Sort the CIK's parsed accessions by filing_date via DB lookup.
        accs = [a for a, _ in items]
        if len(accs) < 2:
            continue
        placeholders = ",".join("?" for _ in accs)
        rows = list(cur.execute(
            f"SELECT accession, filing_date FROM filings WHERE accession IN ({placeholders}) ORDER BY filing_date",
            accs,
        ))
        wc_by_acc = {a: w for a, w in items}
        for i in range(1, len(rows)):
            prev_acc = rows[i - 1]["accession"]
            cur_acc = rows[i]["accession"]
            prev_wc = wc_by_acc.get(prev_acc, 0)
            cur_wc = wc_by_acc.get(cur_acc, 0)
            if not prev_wc or not cur_wc:
                continue
            ratio = max(prev_wc, cur_wc) / max(min(prev_wc, cur_wc), 1)
            if ratio > 5.0:
                out.append((cik, cur_acc, prev_acc, cur_wc, prev_wc, round(ratio, 2)))
    out.sort(key=lambda t: -t[5])
    return out


def render_stats(stats: dict) -> str:
    out = []
    total = stats["ok"] + stats["fail"]
    out.append(f"Total parsed: {total}")
    out.append(f"  ok:         {stats['ok']}")
    out.append(f"  fail:       {stats['fail']}")
    out.append(f"  suspicious: {stats['suspicious']}")
    out.append("")
    out.append("Word-count distribution (ok only):")
    wc = stats["word_count"]
    out.append(f"  min/P5/P50/P95/P99/max: {wc['min']}/{wc['p5']}/{wc['p50']}/{wc['p95']}/{wc['p99']}/{wc['max']}")
    out.append("")
    out.append("Suspicious flag tally:")
    for k, v in sorted(stats["flag_tally"].items(), key=lambda x: -x[1]):
        out.append(f"  {k:<20} {v}")
    if not stats["flag_tally"]:
        out.append("  (none)")
    out.append("")
    out.append("Top 10 shortest filings:")
    for cik, acc, wc_val in stats["shortest"]:
        out.append(f"  {wc_val:>8} words  CIK {cik}  {acc}")
    out.append("")
    out.append("Top 10 longest filings:")
    for cik, acc, wc_val in stats["longest"]:
        out.append(f"  {wc_val:>8} words  CIK {cik}  {acc}")
    out.append("")
    out.append("YoY length jumps > 5x (worst 20; likely parse breaks, not real change):")
    if not stats["yoy_jumps"]:
        out.append("  (none)")
    for cik, acc, prior, wc_val, prior_wc, ratio in stats["yoy_jumps"]:
        out.append(f"  {ratio:>5}x  CIK {cik}  current={acc} ({wc_val})  prior={prior} ({prior_wc})")
    return "\n".join(out)


def pick_samples(clean_root: Path, n: int, *, suspicious_only: bool, seed: int | None = None) -> list[tuple[int, str, dict]]:
    candidates = []
    for cik, accession, sections in _iter_sections(clean_root):
        if sections.get("status") != "ok":
            continue
        flags = (sections.get("full") or {}).get("suspicious_flags") or []
        if suspicious_only and not flags:
            continue
        if (not suspicious_only) and flags:
            # for the random pool, exclude flagged so the 'normal' sample is clean
            continue
        candidates.append((cik, accession, sections))
    rng = random.Random(seed)
    rng.shuffle(candidates)
    return candidates[:n]


def render_sample(clean_root: Path, cik: int, accession: str, sections: dict, *, head_chars: int = 1500, tail_chars: int = 1500) -> str:
    full_path = filing_clean_dir(clean_root, cik, accession) / "full.txt"
    if not full_path.exists():
        return f"=== CIK {cik} / {accession} ===\n(full.txt missing)"
    text = full_path.read_text(encoding="utf-8")
    full = sections.get("full") or {}
    wc = full.get("word_count", "?")
    flags = full.get("suspicious_flags") or []
    head = text[:head_chars]
    tail = text[-tail_chars:] if len(text) > head_chars + tail_chars else ""
    parts = [
        f"=== CIK {cik} / {accession} ===",
        f"Word count: {wc:,}  Flags: {flags}" if isinstance(wc, int) else f"Word count: {wc}  Flags: {flags}",
        "--- first %d chars ---" % head_chars,
        head,
    ]
    if tail:
        parts += ["--- last %d chars ---" % tail_chars, tail]
    parts.append("")
    return "\n".join(parts)
