"""S&P 500 membership scraped from Wikipedia.

Replaces the broken BlackRock IVV CSV pull (returns HTML now). Wikipedia's
`List_of_S&P_500_companies` page contains the canonical constituents table
maintained by editors with GICS sectors + CIKs in a single source.

Limitations:
- Editor-maintained: index changes can lag by hours-to-days vs the actual
  S&P index reconstitution. For a monthly-refresh research tool this is fine.
- No weight column. If weight-aware analysis ever matters, layer an ETF
  holdings source (SPY xlsx, etc.) on top.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

WIKI_URL = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
CONSTITUENTS_TABLE_ID = "constituents"

# Column mapping inside the table. The header text uses 'GICSSector' (no
# space) on Wikipedia — kept as a literal to avoid silent reordering bugs
# if a header style edit happens upstream.
EXPECTED_HEADERS = (
    "Symbol", "Security", "GICSSector", "GICS Sub-Industry",
    "Headquarters Location", "Date added", "CIK", "Founded",
)


@dataclass(frozen=True)
class WikiMember:
    """One row of the constituents table."""
    ticker: str            # canonical SEC form (dots → dashes already applied)
    name: str
    sector: str            # GICS sector
    sub_industry: str
    cik: int | None        # parsed from the page's zero-padded CIK; None if unparseable
    headquarters: str
    date_added: str | None
    founded: str | None


def fetch_constituents_html(*, user_agent: str | None = None, timeout: float = 30.0) -> bytes:
    """HTTP GET the Wikipedia page. Returns raw bytes for snapshotting."""
    ua = user_agent or "Sibyl research tool (https://github.com/snowleopard-spec/sibyl)"
    logger.info("Fetching Wikipedia S&P 500 page: %s", WIKI_URL)
    resp = requests.get(WIKI_URL, headers={"User-Agent": ua}, timeout=timeout)
    resp.raise_for_status()
    return resp.content


def snapshot_html(html_bytes: bytes, snapshot_dir: Path, *, stamp: str | None = None) -> Path:
    """Persist the raw HTML to `snapshot_dir/wiki_sp500_<YYYY-MM-DD>.html`."""
    stamp = stamp or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    path = snapshot_dir / f"wiki_sp500_{stamp}.html"
    path.write_bytes(html_bytes)
    return path


def _normalise_ticker(raw: str) -> str:
    """Canonical SEC form: uppercase, dots → dashes (BRK.B → BRK-B)."""
    return raw.strip().upper().replace(".", "-")


def _parse_cik(raw: str) -> int | None:
    """Wikipedia gives zero-padded 10-digit CIKs (e.g. '0000320193'). Return int."""
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def parse_constituents(html_bytes: bytes) -> list[WikiMember]:
    """Parse the constituents table out of the Wikipedia HTML."""
    soup = BeautifulSoup(html_bytes, "lxml")
    table = soup.find("table", id=CONSTITUENTS_TABLE_ID)
    if table is None:
        # Fall back to the first wikitable on the page.
        table = soup.find("table", class_="wikitable")
    if table is None:
        raise ValueError(
            "No constituents table found on Wikipedia page. Layout may have changed."
        )

    rows = table.find_all("tr")
    if not rows:
        raise ValueError("Constituents table has no rows.")

    # Validate header order so silent column reshuffles fail loudly.
    headers = [th.get_text(strip=True) for th in rows[0].find_all("th")]
    headers_8 = tuple(headers[:8])
    if headers_8 != EXPECTED_HEADERS:
        raise ValueError(
            f"Wikipedia constituents table header changed.\n"
            f"  expected: {EXPECTED_HEADERS}\n"
            f"  got:      {headers_8}\n"
            f"Update EXPECTED_HEADERS in sibyl/wiki.py."
        )

    out: list[WikiMember] = []
    for row in rows[1:]:
        cells = [c.get_text(strip=True) for c in row.find_all(["td", "th"])]
        if len(cells) < 8:
            continue
        ticker_raw, name, sector, sub_industry, hq, date_added, cik_raw, founded = cells[:8]
        ticker = _normalise_ticker(ticker_raw)
        if not ticker:
            continue
        out.append(WikiMember(
            ticker=ticker,
            name=name,
            sector=sector,
            sub_industry=sub_industry,
            cik=_parse_cik(cik_raw),
            headquarters=hq,
            date_added=date_added or None,
            founded=founded or None,
        ))
    return out


def fetch_and_parse(
    *, user_agent: str | None = None, timeout: float = 30.0,
) -> tuple[list[WikiMember], bytes]:
    """Convenience: one-shot fetch + parse. Returns (members, raw_bytes)."""
    raw = fetch_constituents_html(user_agent=user_agent, timeout=timeout)
    members = parse_constituents(raw)
    logger.info("Wikipedia: parsed %d S&P 500 constituents.", len(members))
    return members, raw
