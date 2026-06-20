import sqlite3

import pytest

from sibyl import aggregate as agg
from sibyl import chart
from sibyl.db import init_schema


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    init_schema(c)
    return c


def _seed(conn):
    """Minimal: 1 S&P sector ('IT') with two filings → aggregates populated."""
    for cik, ticker in [(1, "AAPL"), (2, "MSFT")]:
        conn.execute(
            "INSERT INTO sp500_membership(ticker, cik, name, sector, weight_pct, updated_at) "
            "VALUES (?, ?, '-', 'Information Technology', 1.0, '2026-06-20')",
            (ticker, cik),
        )
    for cik, acc, dt, neg, sim in [
        (1, "a1", "2024-02-01T00:00:00Z", 0.001, 0.9),
        (1, "a2", "2024-05-01T00:00:00Z", 0.003, 0.85),
        (2, "m1", "2024-02-15T00:00:00Z", 0.002, 0.88),
    ]:
        conn.execute(
            "INSERT INTO filings(accession, cik, form_type, period_of_report, "
            "acceptance_dt, raw_path, parse_status, stack, downloaded_at) "
            "VALUES (?, ?, '10-K', ?, ?, '/p', 'ok', 'sp500', ?)",
            (acc, cik, dt[:10], dt, dt),
        )
        for section in ("risk_factors", "mdna"):
            conn.execute(
                "INSERT INTO filing_signals(cik, accession, prior_accession, section, "
                "diff_version, similarity_yoy, d_unc, d_lit, d_neg, computed_at) "
                "VALUES (?, ?, 'prior', ?, '1', ?, 0.0, 0.0, ?, '2024-11-03T00:00:00Z')",
                (cik, acc, section, sim, neg),
            )
    conn.commit()
    agg.rebuild_aggregates(conn)


def test_chart_filename_pattern_stable():
    name = chart.chart_filename("AAPL", stamp="20260620T120000Z")
    assert name == "chart_AAPL_20260620T120000Z.png"


def test_render_chart_writes_png(conn, tmp_path):
    _seed(conn)
    out = tmp_path / "chart.png"
    result = chart.render_chart(
        conn, ticker="AAPL", cik=1, sector="Information Technology", output_path=out,
    )
    assert result == out
    assert out.exists()
    # PNG magic header check.
    assert out.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"


def test_render_chart_works_without_sector(conn, tmp_path):
    """A queried (non-S&P) ticker has sector=None — should still render."""
    _seed(conn)
    out = tmp_path / "chart.png"
    chart.render_chart(
        conn, ticker="OBSCURE", cik=999, sector=None, output_path=out,
    )
    assert out.exists()


def test_render_chart_handles_empty_data(conn, tmp_path):
    """Empty corpus shouldn't crash the renderer."""
    out = tmp_path / "chart.png"
    chart.render_chart(
        conn, ticker="EMPTY", cik=0, sector=None, output_path=out,
    )
    assert out.exists()
