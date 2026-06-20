"""Ticker → CIK resolution backed by SEC's company_tickers.json.

Wraps `sibyl.edgar.{download_company_tickers, load_ticker_to_cik}` with:
- dotted share-class normalisation (BRK.B → BRK-B, the form SEC uses);
- weekly refresh based on file mtime;
- fail-loud `LookupError` with a helpful message on unmappable tickers.
"""
from __future__ import annotations

import logging
import time
from pathlib import Path

from .config import Config
from .edgar import RateLimiter, download_company_tickers, load_ticker_to_cik

logger = logging.getLogger(__name__)

REFRESH_AFTER_SECONDS = 7 * 24 * 3600  # weekly


def normalize(ticker: str) -> str:
    """Canonical SEC form: uppercase, dots → dashes (BRK.B → BRK-B)."""
    return ticker.strip().upper().replace(".", "-")


def _file_age_seconds(path: Path) -> float:
    return max(0.0, time.time() - path.stat().st_mtime)


def _ensure_fresh(cfg: Config, *, refresh: bool = False) -> Path:
    """Refresh the on-disk SEC ticker file if missing, older than the
    refresh window, or `refresh=True`."""
    path = cfg.paths.company_tickers
    needs_pull = refresh or not path.exists() or _file_age_seconds(path) > REFRESH_AFTER_SECONDS
    if needs_pull:
        logger.info("Refreshing %s (age check / explicit refresh)", path.name)
        limiter = RateLimiter(cfg.sec.rate_limit_per_sec)
        download_company_tickers(
            path, user_agent=cfg.sec.user_agent, limiter=limiter, refresh=True,
        )
    return path


_CACHE: dict[Path, dict[str, int]] = {}


def _table(cfg: Config, *, refresh: bool = False) -> dict[str, int]:
    """Memoised in-process load of the ticker→CIK table."""
    path = _ensure_fresh(cfg, refresh=refresh)
    if refresh or path not in _CACHE:
        _CACHE[path] = load_ticker_to_cik(path)
    return _CACHE[path]


def resolve(cfg: Config, ticker: str, *, refresh: bool = False) -> int:
    """Return CIK (int) for `ticker`. Raises LookupError if unmappable.

    Normalises dotted share classes (BRK.B → BRK-B). Set `refresh=True`
    to force-pull the SEC file (useful when a recent IPO isn't found).
    """
    table = _table(cfg, refresh=refresh)
    key = normalize(ticker)
    cik = table.get(key)
    if cik is not None:
        return cik
    raise LookupError(
        f"Ticker {ticker!r} (normalised to {key!r}) not in SEC's "
        f"company_tickers.json. Possible causes: foreign listing without a "
        f"CIK, very recent IPO (SEC's file lags ~24h — try resolve(..., "
        f"refresh=True) tomorrow), or non-equity instrument."
    )


def resolve_many(
    cfg: Config, tickers: list[str], *, refresh: bool = False,
) -> tuple[dict[str, int], list[str]]:
    """Resolve a batch. Returns ({ticker: cik}, [unresolved_tickers])."""
    table = _table(cfg, refresh=refresh)
    resolved: dict[str, int] = {}
    unresolved: list[str] = []
    for t in tickers:
        key = normalize(t)
        cik = table.get(key)
        if cik is None:
            unresolved.append(t)
        else:
            resolved[t] = cik
    return resolved, unresolved
