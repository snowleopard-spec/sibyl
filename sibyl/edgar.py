from __future__ import annotations

import json
import logging
import threading
import time
from collections import deque
from pathlib import Path

import requests

logger = logging.getLogger(__name__)

COMPANY_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SUBMISSIONS_BASE = "https://data.sec.gov/submissions/"
ARCHIVES_BASE = "https://www.sec.gov/Archives/edgar/data/"

RETRY_BACKOFF_SEC = (2.0, 4.0, 8.0)


class RateLimiter:
    """Simple sliding-window limiter: at most `rate` requests per second."""

    def __init__(self, rate_per_sec: int):
        self.rate = rate_per_sec
        self._stamps: deque[float] = deque()
        self._lock = threading.Lock()

    def acquire(self) -> None:
        with self._lock:
            now = time.monotonic()
            while self._stamps and now - self._stamps[0] >= 1.0:
                self._stamps.popleft()
            if len(self._stamps) >= self.rate:
                sleep_for = 1.0 - (now - self._stamps[0])
                if sleep_for > 0:
                    time.sleep(sleep_for)
                now = time.monotonic()
                while self._stamps and now - self._stamps[0] >= 1.0:
                    self._stamps.popleft()
            self._stamps.append(time.monotonic())


def sec_get(url: str, *, user_agent: str, limiter: RateLimiter, timeout: float = 30.0) -> requests.Response:
    """SEC GET with the mandatory UA + rate limit. Retries 429/5xx with backoff."""
    last_exc: Exception | None = None
    for attempt, backoff in enumerate((*RETRY_BACKOFF_SEC, None)):
        limiter.acquire()
        try:
            resp = requests.get(
                url,
                headers={"User-Agent": user_agent, "Accept-Encoding": "gzip, deflate"},
                timeout=timeout,
            )
        except requests.RequestException as exc:
            last_exc = exc
            if backoff is None:
                raise
            logger.warning("SEC GET error (attempt %d) %s: %s; retrying in %.0fs", attempt + 1, url, exc, backoff)
            time.sleep(backoff)
            continue

        if resp.status_code == 429 or 500 <= resp.status_code < 600:
            if backoff is None:
                resp.raise_for_status()
            logger.warning(
                "SEC %d for %s (attempt %d); retrying in %.0fs",
                resp.status_code, url, attempt + 1, backoff,
            )
            time.sleep(backoff)
            continue

        resp.raise_for_status()
        return resp

    assert last_exc is not None
    raise last_exc


def download_company_tickers(dest: Path, *, user_agent: str, limiter: RateLimiter, refresh: bool = False) -> Path:
    dest = Path(dest)
    if dest.exists() and not refresh:
        logger.info("company_tickers.json cache hit: %s", dest)
        return dest
    logger.info("Fetching %s", COMPANY_TICKERS_URL)
    resp = sec_get(COMPANY_TICKERS_URL, user_agent=user_agent, limiter=limiter)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".tmp")
    tmp.write_bytes(resp.content)
    tmp.replace(dest)
    return dest


def load_ticker_to_cik(path: Path) -> dict[str, int]:
    """Parse SEC's company_tickers.json -> {TICKER: cik_int}. Tickers uppercased."""
    raw = json.loads(Path(path).read_text())
    out: dict[str, int] = {}
    for row in raw.values():
        ticker = str(row["ticker"]).upper()
        cik = int(row["cik_str"])
        out[ticker] = cik
    return out


def cik_padded(cik: int) -> str:
    return f"{int(cik):010d}"


def submissions_url(cik: int) -> str:
    return f"{SUBMISSIONS_BASE}CIK{cik_padded(cik)}.json"


def submissions_extra_url(filename: str) -> str:
    """SEC sometimes paginates submissions across additional JSONs in `filings.files[]`."""
    return f"{SUBMISSIONS_BASE}{filename}"


def archives_doc_url(cik: int, accession: str, primary_doc: str) -> str:
    accession_nodash = accession.replace("-", "")
    return f"{ARCHIVES_BASE}{int(cik)}/{accession_nodash}/{primary_doc}"
