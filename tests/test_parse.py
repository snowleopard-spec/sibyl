import gzip
import json
import sqlite3
from pathlib import Path

import pytest

from sibyl import parse as ps
from sibyl.db import init_schema

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def fixture_html() -> bytes:
    with gzip.open(FIXTURES / "sample_10k.html.gz", "rb") as f:
        return f.read()


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    init_schema(c)
    return c


def test_clean_filing_strips_tables_scripts_styles(fixture_html):
    text, stats = ps.clean_filing(fixture_html)
    assert "SHOULD_NOT_APPEAR_script" not in text
    assert "SHOULD_NOT_APPEAR_style" not in text
    assert "SHOULD_NOT_APPEAR_table_1234567" not in text
    assert "SHOULD_NOT_APPEAR_table_900000" not in text


def test_clean_filing_keeps_prose_and_xbrl_text(fixture_html):
    text, _ = ps.clean_filing(fixture_html)
    assert "Acme Industries" in text
    assert "Risk Factors" in text
    assert "Management Discussion" in text
    # inline-XBRL text content survives even though the wrapping tag is dropped
    assert "1,234" in text


def test_clean_filing_drops_xbrl_metadata_blocks():
    """The header noise that contaminated real Apple filings: ix:header / ix:references
    / ix:hidden / link:schemaRef must not appear in cleaned text."""
    html = b"""<html><head></head><body>
    <ix:header>
      <ix:hidden>aapl-20230930 false 2023 FY 0000320193</ix:hidden>
      <ix:references>
        <link:schemaRef xlink:href="http://fasb.org/us-gaap/2023#MarketableSecuritiesCurrent"/>
      </ix:references>
      <ix:resources>
        <xbrli:context id="c-1"><xbrli:entity><xbrli:identifier>0000320193</xbrli:identifier></xbrli:entity></xbrli:context>
      </ix:resources>
    </ix:header>
    """ + (b"<p>The narrative section of the filing follows, with normal English prose. </p>" * 200) + b"""
    <p>Inline number: <ix:nonFraction>1,234</ix:nonFraction> dollars.</p>
    </body></html>"""
    text, _ = ps.clean_filing(html)
    # narrative survives
    assert "narrative section of the filing" in text
    assert "1,234" in text
    # XBRL noise is gone
    assert "fasb.org" not in text
    assert "MarketableSecuritiesCurrent" not in text
    assert "schemaRef" not in text
    assert "xbrli" not in text


def test_clean_filing_normalizes_unicode():
    html = b"<html><body>" + b"x " * 1500 + (
        "we’re testing “smart” quotes and the oﬃce ligature".encode("utf-8")
    ) + b"</body></html>"
    text, _ = ps.clean_filing(html)
    # NFKC turns smart quotes into ASCII equivalents and ligatures into letters.
    assert "we're testing \"smart\" quotes" in text
    assert "office" in text


def test_clean_filing_collapses_whitespace():
    html = b"<html><body><p>hello     world\t\there</p>" + b"<p>filler </p>" * 1500 + b"</body></html>"
    text, _ = ps.clean_filing(html)
    assert "hello world" in text
    assert "\t" not in text


def test_clean_filing_stats(fixture_html):
    text, stats = ps.clean_filing(fixture_html)
    assert stats["word_count"] > 500
    assert stats["char_count"] > 1000
    assert stats["non_letter_ratio"] < 0.5
    assert stats["non_ascii_ratio"] < 0.05


def test_parse_filing_writes_full_and_sections(tmp_path, fixture_html):
    cik, accession = 320193, "0000320193-23-000106"
    raw_dir = tmp_path / "raw" / str(cik) / accession
    raw_dir.mkdir(parents=True)
    with gzip.open(raw_dir / "primary.html.gz", "wb") as f:
        f.write(fixture_html)

    sections = ps.parse_filing(cik, accession, raw_root=tmp_path / "raw", clean_root=tmp_path / "clean")
    assert sections["status"] == "ok"
    assert sections["full"]["status"] == "ok"
    assert (tmp_path / "clean" / str(cik) / accession / "full.txt").exists()
    on_disk = json.loads((tmp_path / "clean" / str(cik) / accession / "sections.json").read_text())
    assert on_disk["parser_version"] == ps.PARSER_VERSION
    assert on_disk["full"]["word_count"] == sections["full"]["word_count"]


def test_parse_filing_too_short_is_parse_fail(tmp_path):
    cik, accession = 1, "0000000001-23-000001"
    raw_dir = tmp_path / "raw" / str(cik) / accession
    raw_dir.mkdir(parents=True)
    with gzip.open(raw_dir / "primary.html.gz", "wb") as f:
        f.write(b"<html><body><p>tiny doc</p></body></html>")
    sections = ps.parse_filing(cik, accession, raw_root=tmp_path / "raw", clean_root=tmp_path / "clean")
    assert sections["status"] == "parse_fail"
    assert "word_count_low" in sections["full"]["suspicious_flags"]
    # full.txt not written when parse_fail
    assert not (tmp_path / "clean" / str(cik) / accession / "full.txt").exists()


def test_parse_filing_missing_raw_writes_parse_fail(tmp_path):
    sections = ps.parse_filing(42, "0000000042-23-000001", raw_root=tmp_path / "raw", clean_root=tmp_path / "clean")
    assert sections["status"] == "parse_fail"
    assert "raw primary doc missing" in sections.get("error", "")


def test_parse_all_writes_status_and_skips_existing(tmp_path, fixture_html, conn):
    cfg = _make_cfg(tmp_path)
    cik = 320193
    a1 = "0000320193-23-000106"
    a2 = "0000320193-22-000108"
    for acc in (a1, a2):
        d = cfg.paths.raw / str(cik) / acc
        d.mkdir(parents=True)
        with gzip.open(d / "primary.html.gz", "wb") as f:
            f.write(fixture_html)
        conn.execute(
            "INSERT INTO filings(accession, cik, form_type, acceptance_dt, raw_path, downloaded_at) "
            "VALUES (?, ?, '10-K', '2023-11-02T18:08:38Z', ?, '2026-06-14T00:00:00Z')",
            (acc, cik, f"raw/{cik}/{acc}/primary.html.gz"),
        )
    conn.commit()

    counts = ps.parse_all(conn, cfg)
    assert counts.parsed == 2
    assert counts.failed == 0
    # second call: nothing to do
    counts2 = ps.parse_all(conn, cfg)
    assert counts2.parsed == 0

    # --force re-parses
    counts3 = ps.parse_all(conn, cfg, force=True)
    assert counts3.parsed == 2


def test_parse_all_filters_by_cik(tmp_path, fixture_html, conn):
    cfg = _make_cfg(tmp_path)
    for cik in (1, 2):
        acc = f"0000000{cik}-23-000001"
        d = cfg.paths.raw / str(cik) / acc
        d.mkdir(parents=True)
        with gzip.open(d / "primary.html.gz", "wb") as f:
            f.write(fixture_html)
        conn.execute(
            "INSERT INTO filings(accession, cik, form_type, acceptance_dt, raw_path, downloaded_at) "
            "VALUES (?, ?, '10-K', '2023-11-02T18:08:38Z', ?, '2026-06-14T00:00:00Z')",
            (acc, cik, f"raw/{cik}/{acc}/primary.html.gz"),
        )
    conn.commit()
    counts = ps.parse_all(conn, cfg, ciks=[1])
    assert counts.parsed == 1
    rows = list(conn.execute("SELECT cik, parse_status FROM filings ORDER BY cik"))
    assert (rows[0]["cik"], rows[0]["parse_status"]) == (1, "ok")
    assert rows[1]["parse_status"] is None  # cik=2 untouched


def test_compute_stats_summarizes_corpus(tmp_path, fixture_html, conn):
    cfg = _make_cfg(tmp_path)
    cik = 1234
    for year, words_target in [(2020, 1), (2021, 5), (2022, 10), (2023, 50)]:
        # Vary length by repeating content to exercise yoy jumps + percentiles
        text = (b"<html><body>" + b"<p>filler word </p>" * (200 * words_target) + b"</body></html>")
        acc = f"0000001234-{str(year)[-2:]}-000001"
        d = cfg.paths.raw / str(cik) / acc
        d.mkdir(parents=True)
        with gzip.open(d / "primary.html.gz", "wb") as f:
            f.write(text)
        conn.execute(
            "INSERT INTO filings(accession, cik, form_type, acceptance_dt, filing_date, raw_path, downloaded_at) "
            "VALUES (?, ?, '10-K', ?, ?, ?, '2026-06-14T00:00:00Z')",
            (acc, cik, f"{year}-11-01T18:08:38Z", f"{year}-11-01", f"raw/{cik}/{acc}/primary.html.gz"),
        )
    conn.commit()
    ps.parse_all(conn, cfg)

    stats = ps.compute_stats(conn, cfg.paths.clean)
    assert stats["ok"] >= 2
    assert stats["word_count"]["min"] > 0
    assert isinstance(stats["yoy_jumps"], list)
    rendered = ps.render_stats(stats)
    assert "Total parsed" in rendered
    assert "Word-count distribution" in rendered


def test_pick_samples_excludes_suspicious_by_default(tmp_path, fixture_html, conn):
    cfg = _make_cfg(tmp_path)
    cik = 999
    # Make one ok-clean and one ok-but-flagged filing.
    for acc, body in [
        ("0000000999-23-000001", fixture_html),
        ("0000000999-22-000001", b"<html><body>" + b"<p>filler </p>" * 1500 + b"</body></html>"),
    ]:
        d = cfg.paths.raw / str(cik) / acc
        d.mkdir(parents=True)
        with gzip.open(d / "primary.html.gz", "wb") as f:
            f.write(body)
        conn.execute(
            "INSERT INTO filings(accession, cik, form_type, acceptance_dt, raw_path, downloaded_at) "
            "VALUES (?, ?, '10-K', '2023-11-02T18:08:38Z', ?, '2026-06-14T00:00:00Z')",
            (acc, cik, f"raw/{cik}/{acc}/primary.html.gz"),
        )
    conn.commit()
    ps.parse_all(conn, cfg)
    normal = ps.pick_samples(cfg.paths.clean, 5, suspicious_only=False, seed=1)
    flagged = ps.pick_samples(cfg.paths.clean, 5, suspicious_only=True, seed=1)
    # normal must not include the flagged accession; flagged must include only flagged ones
    assert all((s.get("full") or {}).get("suspicious_flags", []) == [] for _, _, s in normal)
    for _, _, s in flagged:
        assert (s.get("full") or {}).get("suspicious_flags")


# --- helpers ---

def _make_cfg(tmp_path: Path):
    from sibyl.config import Config, SecConfig, UniverseConfig, _resolve_paths
    paths = _resolve_paths(tmp_path, None, None)
    for p in (paths.raw, paths.clean, paths.logs):
        p.mkdir(parents=True, exist_ok=True)
    return Config(
        paths=paths,
        sec=SecConfig(user_agent="ua x@x", rate_limit_per_sec=8),
        universe=UniverseConfig(form_types=["10-K"], include_amendments=False,
                                history_start="2016-01-01"),
        download_gzip=True,
    )
