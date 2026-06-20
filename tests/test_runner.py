"""Lightweight integration tests for sibyl/runner.py.

These exercise the orchestrator's wiring — we mock out the network-bound
download stages and verify the pre/post sequencing, aggregate rebuild,
chart rendering, and S&P cross-ref dispatch.
"""
import json
import sqlite3
from pathlib import Path

import pytest

from sibyl import aggregate as agg_mod
from sibyl import runner
from sibyl.db import init_schema


def _cfg(tmp_path):
    from sibyl.config import Config, SecConfig, UnicornConfig, UniverseConfig, _resolve_paths
    paths = _resolve_paths(tmp_path, None, None)
    return Config(
        paths=paths,
        sec=SecConfig(user_agent="t", rate_limit_per_sec=1),
        unicorn=UnicornConfig(base_url="", universe_path="", expected_contract_version="1.0", token=None),
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


# --- refresh_sp500 ------------------------------------------------------------

def _seed_for_aggregates(conn):
    """Pre-populate enough state that rebuild_aggregates produces rows."""
    conn.execute(
        "INSERT INTO sp500_membership(ticker, cik, name, sector, weight_pct, updated_at) "
        "VALUES ('AAPL', 1, '-', 'Information Technology', 1.0, '2026-06-20')"
    )
    conn.execute(
        "INSERT INTO filings(accession, cik, form_type, period_of_report, acceptance_dt, "
        "raw_path, parse_status, stack, downloaded_at) "
        "VALUES ('a1', 1, '10-K', '2024-09-30', '2024-11-02T18:00:00Z', '/p', 'ok', 'sp500', '2024-11-02T18:00:00Z')"
    )
    conn.execute(
        "INSERT INTO filing_signals(cik, accession, prior_accession, section, "
        "diff_version, similarity_yoy, d_unc, d_lit, d_neg, computed_at) "
        "VALUES (1, 'a1', 'prior', 'risk_factors', '1', 0.9, 0.0, 0.0, 0.001, '2024-11-03T00:00:00Z')"
    )
    conn.commit()


def test_refresh_sp500_no_download_skips_filing_stages(cfg, conn, monkeypatch):
    """With download=False we still pull membership + rebuild aggregates,
    but no SEC filings are fetched and no parse/sections/score/diff runs."""
    _write_ticker_file(cfg, {"AAPL": 1, "MSFT": 2})
    # Stub the membership pull to return synthetic holdings.
    holdings = [
        type("H", (), {"ticker": "AAPL", "name": "Apple", "sector": "Information Technology", "weight_pct": 7.0})(),
        type("H", (), {"ticker": "MSFT", "name": "Microsoft", "sector": "Information Technology", "weight_pct": 6.5})(),
    ]
    def fake_refresh_membership(_cfg, _conn, **_kw):
        # Mimic the real upsert path so sp500_membership has rows for aggregate.
        _conn.executemany(
            "INSERT INTO sp500_membership(ticker, cik, name, sector, weight_pct, updated_at) "
            "VALUES (?, ?, '-', ?, 1.0, '2026-06-20')",
            [(h.ticker, {"AAPL": 1, "MSFT": 2}[h.ticker], h.sector) for h in holdings],
        )
        _conn.commit()
        return holdings, {"AAPL": 1, "MSFT": 2}, [], Path("/tmp/snap.csv")

    called = {"download_all": 0, "parse_all": 0, "extract_all": 0,
              "score_all": 0, "compute_all": 0}
    monkeypatch.setattr(runner.sp500_mod, "refresh_membership", fake_refresh_membership)
    monkeypatch.setattr(runner.download_mod, "download_all",
                        lambda *a, **k: called.update(download_all=called["download_all"]+1) or None)
    # The other stages should NOT be called with download=False.
    for name, mod in (("parse_all", runner.parse_mod), ("extract_all", runner.sections_mod),
                      ("score_all", runner.score_mod), ("compute_all", runner.diff_mod)):
        monkeypatch.setattr(mod, name,
                            lambda *a, _n=name, **k: called.update({_n: called[_n]+1}) or None)

    summary = runner.refresh_sp500(cfg, conn, download=False)
    assert summary["membership"]["members"] == 2
    assert summary["download"] == {"skipped_entirely": True}
    # Downstream stages skipped.
    assert called["download_all"] == 0
    assert called["parse_all"] == 0
    assert called["extract_all"] == 0
    assert called["score_all"] == 0
    assert called["compute_all"] == 0
    # Aggregates always rebuilt (even when no new filings).
    assert "aggregates" in summary


def test_render_sp500_summary_includes_expected_fields():
    fake = {
        "started_at": "T0", "finished_at": "T1",
        "membership": {"members": 502, "resolved_ciks": 498,
                       "unresolved_tickers": ["X.A", "Y.B"]},
        "download": {"new_filings": 12, "skipped": 8, "failed": 0,
                     "ciks_processed": 502},
        "sections": {"processed": 12, "both_ok": 11, "section_fail": 1,
                     "suspicious": 0, "skipped": 0},
        "score": {"processed": 12, "rows_written": 72, "skipped": 0, "errors": 0},
        "diff": {"processed": 12, "rows_written": 36, "skipped": 0,
                 "no_prior": 0, "errors": 0},
        "aggregates": {"rows_written": 60},
    }
    text = runner.render_sp500_summary(fake)
    assert "Members:" in text
    assert "Unresolved:" in text
    assert "New filings:" in text
    assert "Aggregate rows:" in text


# --- query --------------------------------------------------------------------

def test_query_cross_ref_returns_sp500_stack_no_pipeline_runs(cfg, conn, tmp_path):
    """When ticker is in S&P, the pipeline shouldn't run — just the chart."""
    _write_ticker_file(cfg, {"AAPL": 1})
    _seed_for_aggregates(conn)
    agg_mod.rebuild_aggregates(conn)

    output_dir = tmp_path / "out"
    summary = runner.query(
        cfg, conn, "AAPL", download=False, chart=True, output_dir=output_dir,
    )
    assert summary["resolution"]["stack"] == "sp500"
    assert summary["resolution"]["sector"] == "Information Technology"
    # No pipeline counts when cross-ref.
    assert summary["pipeline_counts"] == {}
    # Chart file written.
    chart_path = Path(summary["chart_path"])
    assert chart_path.exists()
    assert chart_path.read_bytes()[:4] == b"\x89PNG"


def test_query_no_chart_when_chart_false(cfg, conn):
    _write_ticker_file(cfg, {"AAPL": 1})
    _seed_for_aggregates(conn)
    summary = runner.query(cfg, conn, "AAPL", download=False, chart=False)
    assert summary["chart_path"] is None


def test_query_renders_for_non_sp_ticker(cfg, conn, tmp_path):
    """Non-S&P ticker w/ no data + download=False still produces an empty chart."""
    _write_ticker_file(cfg, {"OBSCURE": 999})
    # Need at least the aggregates table populated so chart has S&P reference lines
    _seed_for_aggregates(conn)
    agg_mod.rebuild_aggregates(conn)

    summary = runner.query(
        cfg, conn, "OBSCURE", download=False, chart=True,
        output_dir=tmp_path / "out",
    )
    assert summary["resolution"]["stack"] == "queried"
    assert Path(summary["chart_path"]).exists()


def test_render_query_summary_text_format():
    fake = {
        "input_ticker": "AAPL",
        "resolution": {"ticker": "AAPL", "cik": 320193, "stack": "sp500",
                       "sector": "Information Technology"},
        "filings_count": 22,
        "pipeline_counts": {},
        "chart_path": "/tmp/chart.png",
    }
    text = runner.render_query_summary(fake)
    assert "AAPL" in text
    assert "Stack:" in text
    assert "Sector:" in text
    assert "/tmp/chart.png" in text
