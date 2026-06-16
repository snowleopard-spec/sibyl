from __future__ import annotations

import gzip
import json
import logging
import sqlite3
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Iterator

from . import edgar
from .config import Config

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class FilingRow:
    accession: str
    form_type: str
    filing_date: str
    acceptance_dt: str
    period_of_report: str | None
    primary_doc: str


@dataclass
class Counts:
    ciks_processed: int = 0
    new_filings: int = 0
    skipped: int = 0
    failed: int = 0


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("wb", dir=path.parent, delete=False) as tmp:
        tmp.write(data)
        tmp_path = Path(tmp.name)
    tmp_path.replace(path)


def _atomic_write_text(path: Path, text: str) -> None:
    _atomic_write_bytes(path, text.encode("utf-8"))


def _cache_path(raw_root: Path, cik: int, name: str) -> Path:
    return raw_root / str(int(cik)) / name


def fetch_submissions(
    cik: int,
    *,
    limiter: edgar.RateLimiter,
    user_agent: str,
    raw_root: Path,
    refresh: bool = False,
) -> dict:
    path = _cache_path(raw_root, cik, "submissions.json")
    if path.exists() and not refresh:
        return json.loads(path.read_text())
    url = edgar.submissions_url(cik)
    logger.info("GET %s", url)
    resp = edgar.sec_get(url, user_agent=user_agent, limiter=limiter)
    _atomic_write_bytes(path, resp.content)
    return resp.json()


def fetch_filings_files(
    submissions: dict,
    *,
    cik: int,
    limiter: edgar.RateLimiter,
    user_agent: str,
    raw_root: Path,
    refresh: bool = False,
) -> list[dict]:
    extras: list[dict] = []
    for entry in submissions.get("filings", {}).get("files", []) or []:
        name = entry["name"]
        path = _cache_path(raw_root, cik, f"submissions_{name}")
        if path.exists() and not refresh:
            extras.append(json.loads(path.read_text()))
            continue
        url = edgar.submissions_extra_url(name)
        logger.info("GET %s", url)
        resp = edgar.sec_get(url, user_agent=user_agent, limiter=limiter)
        _atomic_write_bytes(path, resp.content)
        extras.append(resp.json())
    return extras


def _normalize_acceptance(raw: str) -> str:
    """Return ISO-8601 UTC string for a SEC acceptanceDateTime value.

    SEC returns values like '2023-11-02T18:08:38.000Z' or sometimes
    '2018-11-15T17:52:24.000Z'. Keep them as-is when they parse cleanly.
    """
    if not raw:
        return raw
    cleaned = raw.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(cleaned).astimezone(timezone.utc)
    except ValueError:
        return raw
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _iter_parallel_block(block: dict) -> Iterator[dict]:
    """SEC parallel-array block -> iterator of dicts (one per filing)."""
    keys = ("accessionNumber", "form", "filingDate", "reportDate",
            "acceptanceDateTime", "primaryDocument")
    arrays = {k: block.get(k, []) for k in keys}
    n = len(arrays["accessionNumber"])
    for i in range(n):
        yield {k: arrays[k][i] if i < len(arrays[k]) else None for k in keys}


def iter_target_filings(
    submissions: dict,
    extra_files: Iterable[dict],
    *,
    form_types: list[str],
    history_start: str,
    include_amendments: bool,
) -> Iterator[FilingRow]:
    forms = set(form_types)
    blocks: list[dict] = []
    recent = submissions.get("filings", {}).get("recent")
    if recent:
        blocks.append(recent)
    for extra in extra_files:
        if isinstance(extra, dict) and "accessionNumber" in extra:
            blocks.append(extra)
        elif isinstance(extra, dict) and "filings" in extra:
            r = extra["filings"].get("recent")
            if r:
                blocks.append(r)

    for block in blocks:
        for row in _iter_parallel_block(block):
            form = (row.get("form") or "").strip()
            is_amendment = form.endswith("/A")
            if is_amendment and not include_amendments:
                continue
            base_form = form[:-2] if is_amendment else form
            if base_form not in forms:
                continue
            filing_date = (row.get("filingDate") or "").strip()
            if not filing_date or filing_date < history_start:
                continue
            accession = (row.get("accessionNumber") or "").strip()
            primary_doc = (row.get("primaryDocument") or "").strip()
            if not accession or not primary_doc:
                continue
            yield FilingRow(
                accession=accession,
                form_type=form,
                filing_date=filing_date,
                acceptance_dt=_normalize_acceptance(row.get("acceptanceDateTime") or ""),
                period_of_report=(row.get("reportDate") or None),
                primary_doc=primary_doc,
            )


def filing_dir(raw_root: Path, cik: int, accession: str) -> Path:
    return raw_root / str(int(cik)) / accession


def is_filing_complete(conn: sqlite3.Connection, raw_root: Path, cik: int, accession: str) -> bool:
    """Resumability: both DB row and metadata.json on disk."""
    row = conn.execute(
        "SELECT raw_path FROM filings WHERE accession = ?", (accession,)
    ).fetchone()
    if row is None:
        return False
    meta = filing_dir(raw_root, cik, accession) / "metadata.json"
    return meta.exists()


def download_filing(
    cik: int,
    row: FilingRow,
    *,
    raw_root: Path,
    limiter: edgar.RateLimiter,
    user_agent: str,
    gzip_compress: bool = True,
) -> Path:
    folder = filing_dir(raw_root, cik, row.accession)
    folder.mkdir(parents=True, exist_ok=True)

    url = edgar.archives_doc_url(cik, row.accession, row.primary_doc)
    logger.debug("GET %s", url)
    resp = edgar.sec_get(url, user_agent=user_agent, limiter=limiter)
    content = resp.content

    if gzip_compress:
        doc_path = folder / "primary.html.gz"
        # Compress, then atomic rename.
        with tempfile.NamedTemporaryFile("wb", dir=folder, delete=False) as tmp:
            with gzip.GzipFile(fileobj=tmp, mode="wb") as gz:
                gz.write(content)
            tmp_path = Path(tmp.name)
        tmp_path.replace(doc_path)
    else:
        doc_path = folder / "primary.html"
        _atomic_write_bytes(doc_path, content)

    metadata = {
        **asdict(row),
        "cik": int(cik),
        "doc_url": url,
        "doc_filename": doc_path.name,
        "downloaded_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    _atomic_write_text(folder / "metadata.json", json.dumps(metadata, indent=2, sort_keys=True))

    return doc_path


def insert_filing(
    conn: sqlite3.Connection,
    *,
    cik: int,
    ticker: str | None,
    row: FilingRow,
    raw_path: Path,
    data_root: Path,
) -> None:
    try:
        rel_path = raw_path.relative_to(data_root)
    except ValueError:
        rel_path = raw_path
    conn.execute(
        """
        INSERT OR IGNORE INTO filings
            (accession, cik, ticker, form_type, period_of_report, acceptance_dt,
             filing_date, primary_doc, raw_path, downloaded_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            row.accession,
            int(cik),
            ticker,
            row.form_type,
            row.period_of_report,
            row.acceptance_dt,
            row.filing_date,
            row.primary_doc,
            str(rel_path),
            datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        ),
    )
    conn.commit()


def _select_targets(conn: sqlite3.Connection, ciks: list[int] | None) -> list[tuple[int, str | None]]:
    """Return [(cik, ticker), ...] from the latest as_of_date in universe_membership.

    If `ciks` is provided, use those CIKs explicitly (ticker looked up from any
    membership row), regardless of latest-snapshot membership.
    """
    cur = conn.cursor()
    if ciks:
        out: list[tuple[int, str | None]] = []
        for cik in ciks:
            r = cur.execute(
                "SELECT ticker FROM universe_membership WHERE cik = ? "
                "ORDER BY as_of_date DESC LIMIT 1",
                (int(cik),),
            ).fetchone()
            out.append((int(cik), r["ticker"] if r else None))
        return out

    latest = cur.execute("SELECT MAX(as_of_date) FROM universe_membership").fetchone()[0]
    if not latest:
        return []
    return [
        (int(r["cik"]), r["ticker"])
        for r in cur.execute(
            "SELECT cik, ticker FROM universe_membership "
            "WHERE as_of_date = ? AND cik IS NOT NULL "
            "ORDER BY ticker",
            (latest,),
        )
    ]


def _append_downloaded_log(logs_dir: Path, accession: str) -> None:
    logs_dir.mkdir(parents=True, exist_ok=True)
    with (logs_dir / "downloaded.txt").open("a", encoding="utf-8") as f:
        f.write(accession + "\n")


def download_all(
    conn: sqlite3.Connection,
    cfg: Config,
    *,
    ciks: list[int] | None = None,
    limit: int | None = None,
    refresh_submissions: bool = False,
) -> Counts:
    targets = _select_targets(conn, ciks)
    if not targets:
        logger.warning("No CIKs to download (universe_membership empty?). Run `sibyl universe` first.")
        return Counts()

    limiter = edgar.RateLimiter(cfg.sec.rate_limit_per_sec)
    counts = Counts()
    total = len(targets)

    for idx, (cik, ticker) in enumerate(targets, start=1):
        try:
            submissions = fetch_submissions(
                cik,
                limiter=limiter,
                user_agent=cfg.sec.user_agent,
                raw_root=cfg.paths.raw,
                refresh=refresh_submissions,
            )
            extras = fetch_filings_files(
                submissions,
                cik=cik,
                limiter=limiter,
                user_agent=cfg.sec.user_agent,
                raw_root=cfg.paths.raw,
                refresh=refresh_submissions,
            )
        except Exception as exc:
            logger.error("Submissions fetch failed for CIK %s (%s): %s", cik, ticker, exc)
            counts.failed += 1
            continue

        for row in iter_target_filings(
            submissions, extras,
            form_types=cfg.universe.form_types,
            history_start=cfg.universe.history_start,
            include_amendments=cfg.universe.include_amendments,
        ):
            if is_filing_complete(conn, cfg.paths.raw, cik, row.accession):
                counts.skipped += 1
                continue
            try:
                doc_path = download_filing(
                    cik, row,
                    raw_root=cfg.paths.raw,
                    limiter=limiter,
                    user_agent=cfg.sec.user_agent,
                    gzip_compress=cfg.download_gzip,
                )
                insert_filing(
                    conn,
                    cik=cik, ticker=ticker, row=row,
                    raw_path=doc_path, data_root=cfg.paths.data_root,
                )
                _append_downloaded_log(cfg.paths.logs, row.accession)
                counts.new_filings += 1
                if limit is not None and counts.new_filings >= limit:
                    counts.ciks_processed = idx
                    logger.info("Reached --limit %d; stopping.", limit)
                    return counts
            except Exception as exc:
                logger.error("Filing download failed CIK %s accession %s: %s", cik, row.accession, exc)
                counts.failed += 1

        counts.ciks_processed = idx
        if idx % 25 == 0 or idx == total:
            logger.info(
                "[%d/%d CIKs] new=%d skipped=%d failed=%d",
                idx, total, counts.new_filings, counts.skipped, counts.failed,
            )

    return counts
