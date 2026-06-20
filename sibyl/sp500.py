"""S&P 500 universe acquisition from iShares' public IVV holdings CSV.

The CSV BlackRock publishes has a header stanza of fund metadata, then a
blank line, then the holdings table. We snapshot each pull, parse the
holdings, resolve tickers → CIKs, and upsert into the `sp500_membership`
table. Used by the runner before any S&P filings are downloaded.

Top-of-module flag `DOWNLOAD_MISSING_FILINGS` controls whether a refresh
should also pull missing filings (per spec §5.1). The CLI exposes this
via --download / --no-download.
"""
from __future__ import annotations

import csv
import io
import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import requests

from .config import Config
from . import tickers as tickers_mod

logger = logging.getLogger(__name__)

# Default: a refresh also pulls any new filings. Flip to False (or use
# `--no-download` on the CLI) to refresh membership only.
DOWNLOAD_MISSING_FILINGS = True

# Direct CSV-download endpoint. URL has been stable since 2019 but is
# documented as volatile — check first if a refresh fails.
IVV_CSV_URL = (
    "https://www.ishares.com/us/products/239726/"
    "ishares-core-sp-500-etf/1467271812596.ajax"
    "?fileType=csv&fileName=IVV_holdings&dataType=fund"
)

# The holdings rows start after a stanza of fund metadata + a blank line.
# Header row contains 'Ticker' and 'Sector'. We sniff for that row.
HOLDINGS_HEADER_MARKER = "Ticker"

# Drop rows that aren't actually equity positions (cash, futures, etc.).
EQUITY_ASSET_CLASS_VALUES = {"Equity", "EQUITY", "Stock"}


@dataclass(frozen=True)
class Holding:
    ticker: str
    name: str
    sector: str
    weight_pct: float


def fetch_ivv_csv(*, user_agent: str | None = None, timeout: float = 30.0) -> bytes:
    """Download the live IVV holdings CSV. Returns raw bytes for snapshotting."""
    headers = {"User-Agent": user_agent or "Mozilla/5.0 (Sibyl research tool)"}
    logger.info("Fetching IVV holdings CSV: %s", IVV_CSV_URL)
    resp = requests.get(IVV_CSV_URL, headers=headers, timeout=timeout)
    resp.raise_for_status()
    return resp.content


def snapshot_csv(cfg: Config, raw_bytes: bytes, *, stamp: str | None = None) -> Path:
    """Save the raw CSV under data/sp500/membership_snapshots/ivv_<YYYY-MM-DD>.csv."""
    stamp = stamp or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    cfg.paths.sp500_snapshots.mkdir(parents=True, exist_ok=True)
    path = cfg.paths.sp500_snapshots / f"ivv_{stamp}.csv"
    path.write_bytes(raw_bytes)
    return path


def parse_holdings(raw_bytes: bytes) -> list[Holding]:
    """Parse the holdings table out of the IVV CSV.

    The file is:
      <stanza of fund metadata rows: name, dates, fees, etc.>
      <blank line>
      <header row starting with 'Ticker'>
      <holdings rows>
      <optional trailing rows: 'Disclosure', etc.>
    """
    text = raw_bytes.decode("utf-8-sig", errors="replace")  # strip BOM if present
    # Locate the holdings header row.
    lines = text.splitlines()
    header_idx = None
    for i, line in enumerate(lines):
        if line.startswith(HOLDINGS_HEADER_MARKER + ","):
            header_idx = i
            break
    if header_idx is None:
        raise ValueError(
            "Could not find holdings header in IVV CSV. "
            "BlackRock may have changed the file format."
        )
    holdings_csv = "\n".join(lines[header_idx:])
    reader = csv.DictReader(io.StringIO(holdings_csv))
    required = ("Ticker", "Name", "Sector", "Weight (%)", "Asset Class")
    missing = [c for c in required if c not in (reader.fieldnames or [])]
    if missing:
        raise ValueError(
            f"IVV CSV holdings header missing required columns {missing}; "
            f"got {reader.fieldnames}"
        )

    out: list[Holding] = []
    for row in reader:
        if (row.get("Asset Class") or "").strip() not in EQUITY_ASSET_CLASS_VALUES:
            continue
        ticker = (row.get("Ticker") or "").strip()
        if not ticker or ticker == "-":
            continue
        try:
            weight = float((row.get("Weight (%)") or "0").replace(",", ""))
        except ValueError:
            weight = 0.0
        out.append(Holding(
            ticker=ticker,
            name=(row.get("Name") or "").strip(),
            sector=(row.get("Sector") or "").strip(),
            weight_pct=weight,
        ))
    return out


def upsert_membership(
    conn: sqlite3.Connection,
    holdings: list[Holding],
    *,
    cik_map: dict[str, int],
    updated_at: str | None = None,
) -> int:
    """Replace the sp500_membership table contents with the new holdings.
    Returns number of rows written.

    `cik_map` is {ticker: cik}; tickers without a CIK get NULL stored and
    are still tracked (membership is real even if our CIK lookup failed).
    """
    updated_at = updated_at or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    cur = conn.cursor()
    cur.execute("DELETE FROM sp500_membership")
    rows = [
        (h.ticker, cik_map.get(h.ticker), h.name, h.sector, h.weight_pct, updated_at)
        for h in holdings
    ]
    cur.executemany(
        "INSERT INTO sp500_membership (ticker, cik, name, sector, weight_pct, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        rows,
    )
    conn.commit()
    return len(rows)


def refresh_membership(
    cfg: Config,
    conn: sqlite3.Connection,
    *,
    raw_bytes: bytes | None = None,
) -> tuple[list[Holding], dict[str, int], list[str], Path]:
    """End-to-end membership refresh:
      1. Fetch live IVV CSV (or use the provided bytes for testing).
      2. Snapshot to disk.
      3. Parse holdings.
      4. Resolve tickers → CIKs.
      5. Upsert sp500_membership.

    Returns (holdings, cik_map, unresolved_tickers, snapshot_path).
    """
    bytes_ = raw_bytes if raw_bytes is not None else fetch_ivv_csv(user_agent=cfg.sec.user_agent)
    snap = snapshot_csv(cfg, bytes_)
    holdings = parse_holdings(bytes_)
    logger.info("IVV holdings parsed: %d equity positions.", len(holdings))

    tickers_list = [h.ticker for h in holdings]
    cik_map, unresolved = tickers_mod.resolve_many(cfg, tickers_list)
    if unresolved:
        logger.warning(
            "Failed to resolve %d/%d tickers to CIK (kept in membership with NULL CIK): %s",
            len(unresolved), len(tickers_list), ", ".join(unresolved[:10]) + (
                " ..." if len(unresolved) > 10 else ""
            ),
        )
    upsert_membership(conn, holdings, cik_map=cik_map)
    return holdings, cik_map, unresolved, snap


def members_with_ciks(conn: sqlite3.Connection) -> list[tuple[str, int, str]]:
    """Return [(ticker, cik, sector)] for current S&P members with resolved CIKs."""
    return [
        (r["ticker"], int(r["cik"]), r["sector"] or "")
        for r in conn.execute(
            "SELECT ticker, cik, sector FROM sp500_membership "
            "WHERE cik IS NOT NULL ORDER BY ticker"
        )
    ]


def status(conn: sqlite3.Connection) -> dict:
    """Quick summary for the CLI: counts, last-updated, per-sector breakdown."""
    n = conn.execute("SELECT COUNT(*) FROM sp500_membership").fetchone()[0]
    n_with_cik = conn.execute(
        "SELECT COUNT(*) FROM sp500_membership WHERE cik IS NOT NULL"
    ).fetchone()[0]
    last = conn.execute(
        "SELECT MAX(updated_at) FROM sp500_membership"
    ).fetchone()[0]
    per_sector = [
        (r["sector"] or "(unknown)", int(r["n"]))
        for r in conn.execute(
            "SELECT sector, COUNT(*) AS n FROM sp500_membership "
            "GROUP BY sector ORDER BY n DESC"
        )
    ]
    return {
        "members": int(n),
        "members_with_cik": int(n_with_cik),
        "last_updated": last,
        "per_sector": per_sector,
    }


def render_status(stat: dict) -> str:
    out = []
    out.append(f"S&P 500 membership (last updated: {stat['last_updated']})")
    out.append(f"  members:           {stat['members']}")
    out.append(f"  with resolved CIK: {stat['members_with_cik']}")
    out.append("")
    out.append("By sector:")
    for sector, n in stat["per_sector"]:
        out.append(f"  {sector:<28} {n}")
    return "\n".join(out)
