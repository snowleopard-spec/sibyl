import gzip
import json
import sqlite3
from pathlib import Path

import pytest

from sibyl import parse as ps
from sibyl import sections as sx
from sibyl.db import init_schema


FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    init_schema(c)
    return c


def test_local_filing_reads_local_gz(tmp_path):
    p = tmp_path / "primary.html.gz"
    with gzip.open(p, "wb") as f:
        f.write(b"<html><body>hello</body></html>")
    f = sx.LocalFiling(cik=1, accession="0000000001-23-000001", html_path=p)
    assert "<body>hello" in f.html()


def test_local_filing_accepts_form_type(tmp_path):
    """LocalFiling must propagate form_type to edgar.Filing for the dispatch."""
    p = tmp_path / "primary.html.gz"
    with gzip.open(p, "wb") as f:
        f.write(b"<html></html>")
    f = sx.LocalFiling(cik=1, accession="x", html_path=p, form_type="10-Q")
    assert f.form == "10-Q"


def test_extract_via_edgartools_rejects_unsupported_form(tmp_path):
    p = tmp_path / "primary.html.gz"
    with gzip.open(p, "wb") as f:
        f.write(b"<html></html>")
    f = sx.LocalFiling(cik=1, accession="x", html_path=p, form_type="8-K")
    with pytest.raises(ValueError, match="unsupported form_type"):
        sx._extract_via_edgartools(f, "8-K")


def test_section_status_min_ok_words_parameter_overrides_default():
    """For 10-Q RF, min_ok_words=0 means a 3-word section is 'ok' with no flag."""
    text = "no material changes"
    status, info = sx._section_status(text, full_word_count=10_000, min_ok_words=0)
    assert status == "ok"
    assert "length_low" not in info["suspicious_flags"]


def test_section_status_default_flags_short_section_low():
    text = "short text here"
    status, info = sx._section_status(text, full_word_count=10_000)
    assert status == "ok"
    assert "length_low" in info["suspicious_flags"]


def test_section_status_ok():
    body = "Apple Inc. faces a range of risks. " * 200  # ~1,400 words
    status, info = sx._section_status(body, full_word_count=40_000)
    assert status == "ok"
    assert info["suspicious_flags"] == []
    assert info["word_count"] > 1000


def test_section_status_low_word_is_ok_but_flagged():
    body = "we have some risks. " * 100  # ~400 words; below MIN_OK_WORDS but no incorp_ref phrase
    status, info = sx._section_status(body, full_word_count=40_000)
    # Low-word with no incorp_ref phrase: per the status table this stays ok with length_low flag
    assert status == "ok"
    assert "length_low" in info["suspicious_flags"]


def test_section_status_incorp_ref():
    body = ("See our annual proxy statement filed with the SEC for further details. " * 30)
    status, info = sx._section_status(body, full_word_count=40_000)
    assert status == "incorp_ref"


def test_section_status_over_extracted_by_length():
    body = "word " * 60_000  # 60k words
    status, info = sx._section_status(body, full_word_count=100_000)
    assert status == "over_extracted"
    assert "length_high" in info["suspicious_flags"]


def test_section_status_over_extracted_by_full_ratio():
    # Section captured ~all of full.txt: 39k words section, 40k word full doc
    body = "word " * 39_000
    status, info = sx._section_status(body, full_word_count=40_000)
    assert status == "over_extracted"
    assert "equal_to_full" in info["suspicious_flags"]


def test_section_status_missing():
    status, info = sx._section_status("", full_word_count=40_000)
    assert status == "missing"
    assert info["word_count"] == 0


@pytest.mark.skipif(
    not (Path("data/raw/320193/0000320193-23-000106/primary.html.gz").exists()
         and Path("data/clean/320193/0000320193-23-000106/sections.json").exists()),
    reason="requires the project corpus (Apple 2023 10-K + Stage 2 output)",
)
def test_apple_2023_regression(tmp_path, monkeypatch):
    """Locks in the spike's Apple-2023 word counts so silent edgartools drift is caught."""
    # Use the project's actual raw + clean dirs.
    raw_root = Path("data/raw")
    clean_root_orig = Path("data/clean")
    # Copy Apple's sections.json into tmp_path so we don't mutate the project's clean/.
    src = clean_root_orig / "320193" / "0000320193-23-000106" / "sections.json"
    dst_dir = tmp_path / "320193" / "0000320193-23-000106"
    dst_dir.mkdir(parents=True)
    (dst_dir / "sections.json").write_text(src.read_text())
    # Also need full.txt for the over_extracted heuristic.
    (dst_dir / "full.txt").write_text((clean_root_orig / "320193" / "0000320193-23-000106" / "full.txt").read_text())

    result = sx.extract_sections(320193, "0000320193-23-000106",
                                 raw_root=raw_root, clean_root=tmp_path)
    rf = result["risk_factors"]
    mdna = result["mdna"]
    # Spike numbers: rf 9,794 words / 67,998 chars ; mdna 2,348 / 15,509
    assert rf["status"] == "ok"
    assert 9000 <= rf["word_count"] <= 11000
    assert 60000 <= rf["char_count"] <= 75000
    assert mdna["status"] == "ok"
    assert 2000 <= mdna["word_count"] <= 3000


def test_extract_sections_writes_files_and_status(tmp_path):
    """End-to-end: real edgartools on a synthetic minimal 10-K. Validates the wiring,
    not the boundary heuristic accuracy."""
    cik, accession = 1, "0000000001-23-000001"
    html = b"""<html><body>
    <p>UNITED STATES SECURITIES AND EXCHANGE COMMISSION</p>
    <p>Item 1A. Risk Factors</p>
    """ + (b"<p>We face significant competition and material risks of operations. </p>" * 200) + b"""
    <p>Item 1B. Unresolved Staff Comments</p>
    <p>None.</p>
    <p>Item 7. Management's Discussion and Analysis of Financial Condition and Results of Operations</p>
    """ + (b"<p>Revenue grew this year. Our margins remained strong. </p>" * 200) + b"""
    <p>Item 7A. Quantitative and Qualitative Disclosures About Market Risk</p>
    </body></html>"""
    raw_dir = tmp_path / "raw" / str(cik) / accession
    raw_dir.mkdir(parents=True)
    with gzip.open(raw_dir / "primary.html.gz", "wb") as f:
        f.write(html)
    # Seed sections.json (Stage 2 output stand-in).
    clean_dir = tmp_path / "clean" / str(cik) / accession
    clean_dir.mkdir(parents=True)
    (clean_dir / "sections.json").write_text(json.dumps({
        "parser_version": "2", "section_extractor_version": None,
        "status": "ok", "full": {"status": "ok", "word_count": 4000, "char_count": 25000}
    }))
    result = sx.extract_sections(cik, accession,
                                 raw_root=tmp_path / "raw",
                                 clean_root=tmp_path / "clean")
    # Whatever edgartools finds, we should have a non-crashing result with both keys.
    assert "risk_factors" in result
    assert "mdna" in result
    assert result["section_extractor_version"] == sx.EXTRACTOR_VERSION
    assert result["status"] in ("ok", "section_fail")


def test_extract_all_skips_at_current_version(tmp_path, conn):
    cfg = _make_cfg(tmp_path)
    # Pre-existing sections.json at current version.
    cik, accession = 1, "0000000001-23-000001"
    conn.execute(
        "INSERT INTO filings(accession, cik, form_type, acceptance_dt, raw_path, downloaded_at, parse_status) "
        "VALUES (?, ?, '10-K', '2023-11-02T18:08:38Z', 'x', '2026-06-14T00:00:00Z', 'ok')",
        (accession, cik),
    )
    conn.commit()
    d = cfg.paths.clean / str(cik) / accession
    d.mkdir(parents=True)
    (d / "sections.json").write_text(json.dumps({
        "parser_version": "2",
        "section_extractor_version": sx.EXTRACTOR_VERSION,
        "status": "ok",
        "full": {"status": "ok", "word_count": 4000},
        "risk_factors": {"status": "ok", "word_count": 5000, "suspicious_flags": []},
        "mdna": {"status": "ok", "word_count": 3000, "suspicious_flags": []},
    }))

    counts = sx.extract_all(conn, cfg)
    assert counts.skipped == 1
    assert counts.processed == 0


def test_apply_yoy_flag_marks_jumps(tmp_path, conn):
    cfg = _make_cfg(tmp_path)
    cik = 7
    # Insert 2 filings, same CIK, with 10× length difference in risk_factors.
    rows = [
        ("0000000007-22-000001", "2022-03-01", 1000),
        ("0000000007-23-000001", "2023-03-01", 12000),
    ]
    for accession, fd, rf_wc in rows:
        conn.execute(
            "INSERT INTO filings(accession, cik, form_type, acceptance_dt, filing_date, raw_path, downloaded_at, parse_status) "
            "VALUES (?, ?, '10-K', ?, ?, 'x', '2026-06-14T00:00:00Z', 'ok')",
            (accession, cik, fd + "T18:00:00Z", fd),
        )
        d = cfg.paths.clean / str(cik) / accession
        d.mkdir(parents=True)
        (d / "sections.json").write_text(json.dumps({
            "parser_version": "2",
            "section_extractor_version": sx.EXTRACTOR_VERSION,
            "status": "ok",
            "full": {"status": "ok", "word_count": 50_000},
            "risk_factors": {"status": "ok", "word_count": rf_wc, "suspicious_flags": []},
            "mdna": {"status": "ok", "word_count": 5000, "suspicious_flags": []},
        }))
    conn.commit()

    flagged = sx._apply_yoy_flags(conn, cfg.paths.clean)
    assert flagged["risk_factors"] == 1
    # The newer (23) filing should have the flag now.
    sec = json.loads((cfg.paths.clean / str(cik) / rows[1][0] / "sections.json").read_text())
    assert "yoy_jump" in sec["risk_factors"]["suspicious_flags"]


def test_pick_validation_set_writes_csv(tmp_path, conn):
    cfg = _make_cfg(tmp_path)
    # Seed: 2 filings spanning ok and yoy_jump.
    for accession, fd, flags in [
        ("0000000010-22-000001", "2022-03-01", []),
        ("0000000010-23-000001", "2023-03-01", ["yoy_jump"]),
    ]:
        conn.execute(
            "INSERT INTO filings(accession, cik, form_type, acceptance_dt, filing_date, raw_path, downloaded_at, parse_status) "
            "VALUES (?, 10, '10-K', ?, ?, 'x', '2026-06-14T00:00:00Z', 'ok')",
            (accession, fd + "T18:00:00Z", fd),
        )
        d = cfg.paths.clean / "10" / accession
        d.mkdir(parents=True)
        (d / "sections.json").write_text(json.dumps({
            "parser_version": "2",
            "section_extractor_version": sx.EXTRACTOR_VERSION,
            "status": "ok",
            "full": {"status": "ok", "word_count": 40000},
            "risk_factors": {"status": "ok", "word_count": 5000, "suspicious_flags": flags},
            "mdna": {"status": "ok", "word_count": 4000, "suspicious_flags": []},
        }))
    conn.commit()

    csv_path = sx.pick_validation_set(conn, cfg.paths.clean, 4)
    assert csv_path.exists()
    content = csv_path.read_text()
    # Headers present
    assert "risk_factors_start_substring" in content.splitlines()[0]
    # At least one row written
    assert len(content.splitlines()) >= 2


def test_compute_stats_summary(tmp_path, conn):
    cfg = _make_cfg(tmp_path)
    # Insert 3 ok filings with different word counts.
    for i, wc in enumerate([5000, 10000, 20000]):
        accession = f"00000000{20+i}-23-00000{i}"
        conn.execute(
            "INSERT INTO filings(accession, cik, form_type, acceptance_dt, filing_date, raw_path, downloaded_at, parse_status) "
            "VALUES (?, 20, '10-K', ?, '2023-03-01', 'x', '2026-06-14T00:00:00Z', 'ok')",
            (accession, "2023-03-01T18:00:00Z"),
        )
        d = cfg.paths.clean / "20" / accession
        d.mkdir(parents=True)
        (d / "sections.json").write_text(json.dumps({
            "parser_version": "2",
            "section_extractor_version": sx.EXTRACTOR_VERSION,
            "status": "ok",
            "full": {"status": "ok", "word_count": 40000},
            "risk_factors": {"status": "ok", "word_count": wc, "suspicious_flags": []},
            "mdna": {"status": "ok", "word_count": wc // 2, "suspicious_flags": []},
        }))
    conn.commit()
    stats = sx.compute_stats(conn, cfg.paths.clean)
    assert stats["both_ok"] == 3
    assert stats["per_section_words"]["risk_factors"]["n"] == 3
    rendered = sx.render_stats(stats)
    assert "risk_factors" in rendered
    assert "mdna" in rendered


def test_extract_worker_is_picklable():
    """Subprocess dispatch via ProcessPoolExecutor requires the worker function
    (and its args) to be picklable. Cheap insurance for future refactors."""
    import pickle
    blob = pickle.dumps(sx._extract_worker)
    fn = pickle.loads(blob)
    assert callable(fn)


def test_extract_all_workers_2_matches_workers_1(tmp_path, conn):
    """workers=1 (inline path) and workers=2 (process pool) must produce identical
    sections.json output (ignoring the parsed_at timestamp)."""
    cfg = _make_cfg(tmp_path)
    cik, accession = 1, "0000000001-23-000001"

    html = (b"<html><body>"
            b"<p>Item 1A. Risk Factors</p>"
            + (b"<p>We face significant competition and material risks. </p>" * 200)
            + b"<p>Item 1B. Unresolved Staff Comments</p><p>None.</p>"
            + b"<p>Item 7. Management's Discussion and Analysis of Financial Condition.</p>"
            + (b"<p>Revenue grew this year. Margins remained strong. </p>" * 200)
            + b"<p>Item 7A. Quantitative Disclosures</p></body></html>")
    raw_dir = cfg.paths.raw / str(cik) / accession
    raw_dir.mkdir(parents=True)
    with gzip.open(raw_dir / "primary.html.gz", "wb") as f:
        f.write(html)
    clean_dir = cfg.paths.clean / str(cik) / accession
    clean_dir.mkdir(parents=True)
    (clean_dir / "sections.json").write_text(json.dumps({
        "parser_version": "2", "section_extractor_version": None,
        "status": "ok", "full": {"status": "ok", "word_count": 4000, "char_count": 25000},
    }))
    conn.execute(
        "INSERT INTO filings(accession, cik, form_type, acceptance_dt, raw_path, downloaded_at, parse_status) "
        "VALUES (?, ?, '10-K', '2023-11-02T18:08:38Z', 'x', '2026-06-14T00:00:00Z', 'ok')",
        (accession, cik),
    )
    conn.commit()

    # Pass 1 — inline (workers=1)
    sx.extract_all(conn, cfg, workers=1)
    sections_w1 = json.loads((clean_dir / "sections.json").read_text())

    # Pass 2 — process pool (workers=2), forced to re-run
    sx.extract_all(conn, cfg, workers=2, force=True)
    sections_w2 = json.loads((clean_dir / "sections.json").read_text())

    # parsed_at will differ; everything else must match
    sections_w1.pop("parsed_at", None)
    sections_w2.pop("parsed_at", None)
    assert sections_w1 == sections_w2


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
