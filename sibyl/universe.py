from __future__ import annotations

import json
import logging
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

from .config import Config

logger = logging.getLogger(__name__)


def fetch_unicorn_universe(config: Config) -> dict[str, Any]:
    if not config.unicorn.token:
        raise RuntimeError(
            "SIBYL_UNICORN_TOKEN not set. Add it to .env (see SIBYL_HANDOFF.md §3)."
        )
    url = f"{config.unicorn.base_url}{config.unicorn.universe_path}"
    logger.info("GET %s", url)
    resp = requests.get(
        url,
        headers={"Authorization": f"Bearer {config.unicorn.token}"},
        timeout=30,
    )
    resp.raise_for_status()
    payload = resp.json()

    got = str(payload.get("contract_version"))
    want = config.unicorn.expected_contract_version
    if got != want:
        logger.warning(
            "Unicorn contract_version mismatch: got %r, expected %r. "
            "Inspect snapshot before trusting downstream stages.",
            got, want,
        )

    return payload


def _generated_date(payload: dict[str, Any]) -> str:
    """Extract the UTC date (YYYY-MM-DD) from payload['generated_at']."""
    raw = payload.get("generated_at")
    if not raw:
        return datetime.now(timezone.utc).date().isoformat()
    # Accept '...Z' or '+00:00' style.
    cleaned = raw.replace("Z", "+00:00")
    dt = datetime.fromisoformat(cleaned).astimezone(timezone.utc)
    return dt.date().isoformat()


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", dir=path.parent, delete=False, encoding="utf-8") as tmp:
        tmp.write(text)
        tmp_path = Path(tmp.name)
    tmp_path.replace(path)


def snapshot_universe(payload: dict[str, Any], snapshots_dir: Path, working_path: Path) -> Path:
    """Write the payload verbatim to a date-stamped snapshot and update the working file."""
    as_of = _generated_date(payload)
    snapshots_dir.mkdir(parents=True, exist_ok=True)
    snapshot_path = snapshots_dir / f"universe_{as_of}.json"
    serialized = json.dumps(payload, indent=2, sort_keys=True)
    _atomic_write_text(snapshot_path, serialized)
    _atomic_write_text(working_path, serialized)
    logger.info("Snapshot written: %s", snapshot_path)
    return snapshot_path


def upsert_membership(conn: sqlite3.Connection, payload: dict[str, Any]) -> tuple[str, int]:
    """Upsert universe rows for the payload's as_of_date. Returns (as_of_date, row_count)."""
    as_of = _generated_date(payload)
    rows = payload.get("universe", [])
    cur = conn.cursor()
    cur.executemany(
        """
        INSERT INTO universe_membership
            (ticker, as_of_date, sector, market_cap, name, exchange, in_universe)
        VALUES (:ticker, :as_of_date, :sector, :market_cap, :name, :exchange, 1)
        ON CONFLICT(ticker, as_of_date) DO UPDATE SET
            sector      = excluded.sector,
            market_cap  = excluded.market_cap,
            name        = excluded.name,
            exchange    = excluded.exchange,
            in_universe = 1
        """,
        [
            {
                "ticker": str(r["ticker"]).upper(),
                "as_of_date": as_of,
                "sector": r.get("sector"),
                "market_cap": r.get("market_cap"),
                "name": r.get("name"),
                "exchange": r.get("exchange"),
            }
            for r in rows
        ],
    )
    conn.commit()
    logger.info("Upserted %d membership rows for %s", len(rows), as_of)
    return as_of, len(rows)


def resolve_ciks(
    conn: sqlite3.Connection, ticker_map: dict[str, int], as_of_date: str
) -> list[str]:
    """Update universe_membership.cik for the given as_of_date. Returns unresolved tickers."""
    cur = conn.cursor()
    tickers = [
        row["ticker"]
        for row in cur.execute(
            "SELECT ticker FROM universe_membership WHERE as_of_date = ?",
            (as_of_date,),
        )
    ]
    updates: list[tuple[int, str]] = []
    unresolved: list[str] = []
    for t in tickers:
        cik = ticker_map.get(t.upper())
        if cik is None:
            unresolved.append(t)
        else:
            updates.append((cik, t))
    if updates:
        cur.executemany(
            "UPDATE universe_membership SET cik = ? WHERE ticker = ? AND as_of_date = ?",
            [(cik, t, as_of_date) for cik, t in updates],
        )
        conn.commit()
    logger.info(
        "CIK resolution: %d resolved, %d unresolved (as_of %s)",
        len(updates), len(unresolved), as_of_date,
    )
    return unresolved
