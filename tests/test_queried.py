import json
import sqlite3
from pathlib import Path

import pytest

from sibyl import queried
from sibyl.db import init_schema


# --------- fixtures ---------

def _cfg(tmp_path):
    from sibyl.config import Config, SecConfig, UniverseConfig, _resolve_paths
    paths = _resolve_paths(tmp_path, None, None)
    return Config(
        paths=paths,
        sec=SecConfig(user_agent="t", rate_limit_per_sec=1),
        universe=UniverseConfig(form_types=["10-K", "10-Q"], include_amendments=False, history_start="2019-01-01"),
        download_gzip=True,
    )


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    init_schema(c)
    return c


@pytest.fixture
def cfg(tmp_path):
    return _cfg(tmp_path)


@pytest.fixture(autouse=True)
def _clear_ticker_cache():
    from sibyl import tickers
    tickers._CACHE.clear()
    yield
    tickers._CACHE.clear()


def _write_ticker_file(cfg, mapping):
    payload = {
        str(i): {"cik_str": cik, "ticker": tk, "title": f"Co {tk}"}
        for i, (tk, cik) in enumerate(mapping.items())
    }
    cfg.paths.company_tickers.parent.mkdir(parents=True, exist_ok=True)
    cfg.paths.company_tickers.write_text(json.dumps(payload))


# --------- record-file I/O ---------

def test_append_and_read_records(tmp_path):
    p = tmp_path / "record.jsonl"
    queried.append_records(p, [{"a": 1}, {"b": 2}])
    queried.append_records(p, [{"c": 3}])
    rows = queried.read_records(p)
    assert rows == [{"a": 1}, {"b": 2}, {"c": 3}]


def test_read_records_missing_returns_empty(tmp_path):
    assert queried.read_records(tmp_path / "nope.jsonl") == []


def test_read_records_skips_malformed_lines(tmp_path, caplog):
    p = tmp_path / "record.jsonl"
    p.write_text('{"ok":true}\nnot json\n{"also":"ok"}\n')
    rows = queried.read_records(p)
    assert rows == [{"ok": True}, {"also": "ok"}]


# --------- rolling window ---------

def test_filter_to_window_keeps_recent_drops_old():
    cutoff_pre = "2010-01-01"   # well outside any sane 5y window
    cutoff_post = "2099-01-01"  # well inside (future date)
    rows = [
        {"accession": "old", "period_of_report": cutoff_pre, "acceptance_dt": cutoff_pre},
        {"accession": "new", "period_of_report": cutoff_post, "acceptance_dt": cutoff_post},
    ]
    out = queried.filter_to_window(rows, rolling_years=5)
    accs = {r["accession"] for r in out}
    assert "old" not in accs
    assert "new" in accs


def test_filter_to_window_uses_acceptance_when_period_missing():
    rows = [{"accession": "x", "period_of_report": None, "acceptance_dt": "2099-01-01"}]
    out = queried.filter_to_window(rows)
    assert len(out) == 1


def test_filter_to_window_keeps_rows_with_no_dates():
    """Defensive: can't judge → keep."""
    rows = [{"accession": "noDates", "period_of_report": None, "acceptance_dt": None}]
    assert len(queried.filter_to_window(rows)) == 1


# --------- sp500 cross-ref lookup ---------

def test_sp500_cik_to_meta_hit(conn):
    conn.execute(
        "INSERT INTO sp500_membership(ticker, cik, name, sector, weight_pct, updated_at) "
        "VALUES ('AAPL', 320193, 'Apple', 'Information Technology', 7.0, '2026-06-20')"
    )
    conn.commit()
    assert queried.sp500_cik_to_meta(conn, 320193) == ("AAPL", "Information Technology")


def test_sp500_cik_to_meta_miss(conn):
    assert queried.sp500_cik_to_meta(conn, 999999) is None


def test_filings_for_cik_filters_by_stack(conn):
    for acc, stk in [("a", "sp500"), ("b", "queried"), ("c", "sp500")]:
        conn.execute(
            "INSERT INTO filings(accession, cik, form_type, acceptance_dt, raw_path, "
            "parse_status, stack, downloaded_at) "
            "VALUES (?, 100, '10-K', '2024-01-01', '/p', 'ok', ?, '2024-01-01')",
            (acc, stk),
        )
    conn.commit()
    assert sorted(r["accession"] for r in queried.filings_for_cik(conn, 100, stack="sp500")) == ["a", "c"]
    assert [r["accession"] for r in queried.filings_for_cik(conn, 100, stack="queried")] == ["b"]
    assert len(queried.filings_for_cik(conn, 100)) == 3


# --------- get_or_fetch cross-ref path ---------

def test_get_or_fetch_returns_sp500_cross_ref_when_member(cfg, conn):
    _write_ticker_file(cfg, {"AAPL": 320193})
    # AAPL is in S&P
    conn.execute(
        "INSERT INTO sp500_membership(ticker, cik, name, sector, weight_pct, updated_at) "
        "VALUES ('AAPL', 320193, 'Apple', 'Information Technology', 7.0, '2026-06-20')"
    )
    # And has an sp500-stack filing
    conn.execute(
        "INSERT INTO filings(accession, cik, form_type, period_of_report, acceptance_dt, "
        "raw_path, parse_status, stack, downloaded_at) "
        "VALUES ('a1', 320193, '10-K', '2099-09-30', '2099-11-02', "
        "'sp500/raw/320193/a1/primary.html.gz', 'ok', 'sp500', '2099-11-02')"
    )
    conn.commit()

    result = queried.get_or_fetch(cfg, conn, "AAPL", download=False)
    assert result.stack == "sp500"
    assert result.cik == 320193
    assert result.sector == "Information Technology"
    assert [f["accession"] for f in result.filings] == ["a1"]
    # No queried record was written.
    assert not cfg.paths.queried_record.exists() or queried.read_records(cfg.paths.queried_record) == []


def test_get_or_fetch_dotted_ticker_resolved_via_normalisation(cfg, conn):
    _write_ticker_file(cfg, {"BRK-B": 1067983})
    conn.execute(
        "INSERT INTO sp500_membership(ticker, cik, name, sector, weight_pct, updated_at) "
        "VALUES ('BRK-B', 1067983, 'Berkshire Hathaway', 'Financials', 1.5, '2026-06-20')"
    )
    conn.commit()
    # User input has dot; resolve normalises to dash.
    result = queried.get_or_fetch(cfg, conn, "BRK.B", download=False)
    assert result.stack == "sp500"
    assert result.cik == 1067983


def test_get_or_fetch_raises_on_unknown_ticker(cfg, conn):
    _write_ticker_file(cfg, {"AAPL": 320193})
    with pytest.raises(LookupError):
        queried.get_or_fetch(cfg, conn, "DOESNOTEXIST", download=False)


# --------- get_or_fetch queried path (no download; just verifies it runs) ----

def test_get_or_fetch_queried_with_download_false_returns_empty_when_no_data(cfg, conn):
    """A non-S&P ticker with no cached queried data + download=False → empty result."""
    _write_ticker_file(cfg, {"OBSCURE": 42})
    result = queried.get_or_fetch(cfg, conn, "OBSCURE", download=False)
    assert result.stack == "queried"
    assert result.cik == 42
    assert result.sector is None
    assert result.filings == []
