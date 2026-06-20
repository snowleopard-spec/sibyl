import gzip
import json
import sqlite3
from pathlib import Path

import pytest
import responses

from sibyl import download as dl
from sibyl import edgar
from sibyl.db import init_schema

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def submissions() -> dict:
    return json.loads((FIXTURES / "submissions_sample.json").read_text())


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    init_schema(c)
    return c


def test_iter_target_filings_filters_form_date_amendments(submissions):
    rows = list(dl.iter_target_filings(
        submissions, [],
        form_types=["10-K"],
        history_start="2016-01-01",
        include_amendments=False,
    ))
    accs = [r.accession for r in rows]
    # 2023 10-K kept; 2022 10-K kept; 10-K/A dropped; 10-Q dropped; 2015 10-K dropped.
    assert accs == ["0000320193-23-000106", "0000320193-22-000108"]
    assert all(r.form_type == "10-K" for r in rows)


def test_iter_target_filings_amendments_when_enabled(submissions):
    rows = list(dl.iter_target_filings(
        submissions, [],
        form_types=["10-K"],
        history_start="2016-01-01",
        include_amendments=True,
    ))
    forms = sorted({r.form_type for r in rows})
    assert "10-K/A" in forms


def test_acceptance_dt_normalized(submissions):
    rows = list(dl.iter_target_filings(
        submissions, [],
        form_types=["10-K"],
        history_start="2016-01-01",
        include_amendments=False,
    ))
    # Z suffix preserved; .000 fraction dropped via strftime.
    assert rows[0].acceptance_dt == "2023-11-02T18:08:38Z"


@responses.activate
def test_download_filing_writes_files_atomically(tmp_path):
    cik = 320193
    row = dl.FilingRow(
        accession="0000320193-23-000106",
        form_type="10-K",
        filing_date="2023-11-03",
        acceptance_dt="2023-11-02T18:08:38Z",
        period_of_report="2023-09-30",
        primary_doc="aapl-20230930.htm",
    )
    url = edgar.archives_doc_url(cik, row.accession, row.primary_doc)
    responses.add(responses.GET, url, body=b"<html>filing body</html>", status=200)

    limiter = edgar.RateLimiter(8)
    doc_path = dl.download_filing(
        cik, row, raw_root=tmp_path,
        limiter=limiter, user_agent="Test ua@example.com", gzip_compress=True,
    )

    assert doc_path.name == "primary.html.gz"
    assert doc_path.exists()
    with gzip.open(doc_path, "rb") as f:
        assert f.read() == b"<html>filing body</html>"

    meta = json.loads((doc_path.parent / "metadata.json").read_text())
    assert meta["accession"] == row.accession
    assert meta["cik"] == cik
    assert meta["doc_url"] == url
    assert meta["downloaded_at"].endswith("Z")


@responses.activate
def test_download_all_end_to_end(conn, tmp_path, submissions, monkeypatch):
    cik = 320193
    conn.execute(
        "INSERT INTO sp500_membership(ticker, cik, name, sector, weight_pct, updated_at) "
        "VALUES (?, ?, 'Apple', 'Information Technology', 3.0, ?)",
        ("AAPL", cik, "2026-06-11"),
    )
    conn.commit()

    responses.add(responses.GET, edgar.submissions_url(cik), json=submissions, status=200)
    for acc, primary in [
        ("0000320193-23-000106", "aapl-20230930.htm"),
        ("0000320193-22-000108", "aapl-20220924.htm"),
    ]:
        responses.add(
            responses.GET,
            edgar.archives_doc_url(cik, acc, primary),
            body=b"<html>%b</html>" % acc.encode(),
            status=200,
        )

    cfg = _make_cfg(tmp_path)
    counts = dl.download_all(conn, cfg)

    assert counts.new_filings == 2
    assert counts.skipped == 0
    assert counts.failed == 0

    db_rows = list(conn.execute(
        "SELECT accession, form_type, acceptance_dt, raw_path FROM filings ORDER BY accession"
    ))
    assert [r["accession"] for r in db_rows] == [
        "0000320193-22-000108",
        "0000320193-23-000106",
    ]
    assert all(r["form_type"] == "10-K" for r in db_rows)
    # raw_path stored relative to data_root
    for r in db_rows:
        assert r["raw_path"].startswith("sp500/raw/320193/")
    # log file written
    assert (cfg.paths.logs / "downloaded.txt").exists()
    assert (cfg.paths.logs / "downloaded.txt").read_text().count("\n") == 2


@responses.activate
def test_resumability_skips_complete_filings(conn, tmp_path, submissions):
    cik = 320193
    conn.execute(
        "INSERT INTO sp500_membership(ticker, cik, name, sector, weight_pct, updated_at) "
        "VALUES (?, ?, 'Apple', 'Information Technology', 3.0, ?)", ("AAPL", cik, "2026-06-11"),
    )
    conn.commit()

    cfg = _make_cfg(tmp_path)
    # Pretend the 2023 filing was already downloaded last run: DB row + metadata.json.
    acc_done = "0000320193-23-000106"
    folder = cfg.paths.raw / str(cik) / acc_done
    folder.mkdir(parents=True)
    (folder / "metadata.json").write_text("{}")
    conn.execute(
        "INSERT INTO filings(accession, cik, form_type, acceptance_dt, raw_path, downloaded_at) "
        "VALUES (?, ?, '10-K', '2023-11-02T18:08:38Z', 'sp500/raw/320193/%s/primary.html.gz', '2026-06-13T00:00:00Z')" % acc_done,
        (acc_done, cik),
    )
    conn.commit()

    responses.add(responses.GET, edgar.submissions_url(cik), json=submissions, status=200)
    # Only the 2022 10-K should be downloaded (the 2023 one is already complete).
    responses.add(
        responses.GET,
        edgar.archives_doc_url(cik, "0000320193-22-000108", "aapl-20220924.htm"),
        body=b"<html>2022</html>", status=200,
    )

    counts = dl.download_all(conn, cfg)
    assert counts.new_filings == 1
    assert counts.skipped == 1


@responses.activate
def test_resumability_redownloads_orphan_files(conn, tmp_path, submissions):
    cik = 320193
    conn.execute(
        "INSERT INTO sp500_membership(ticker, cik, name, sector, weight_pct, updated_at) "
        "VALUES (?, ?, 'Apple', 'Information Technology', 3.0, ?)", ("AAPL", cik, "2026-06-11"),
    )
    conn.commit()

    cfg = _make_cfg(tmp_path)
    # Orphan: .gz on disk but no metadata.json and no DB row.
    acc = "0000320193-23-000106"
    folder = cfg.paths.raw / str(cik) / acc
    folder.mkdir(parents=True)
    (folder / "primary.html.gz").write_bytes(b"stale-junk")

    responses.add(responses.GET, edgar.submissions_url(cik), json=submissions, status=200)
    for a, p in [
        ("0000320193-23-000106", "aapl-20230930.htm"),
        ("0000320193-22-000108", "aapl-20220924.htm"),
    ]:
        responses.add(responses.GET, edgar.archives_doc_url(cik, a, p),
                      body=b"<html>fresh</html>", status=200)

    counts = dl.download_all(conn, cfg)
    assert counts.new_filings == 2
    # Stale orphan replaced with fresh content.
    with gzip.open(folder / "primary.html.gz", "rb") as f:
        assert f.read() == b"<html>fresh</html>"


@responses.activate
def test_submissions_cache_round_trip(tmp_path, submissions):
    cik = 320193
    responses.add(responses.GET, edgar.submissions_url(cik), json=submissions, status=200)
    limiter = edgar.RateLimiter(8)
    first = dl.fetch_submissions(cik, limiter=limiter, user_agent="x x@x", raw_root=tmp_path)
    assert first["cik"] == "0000320193"
    assert (tmp_path / "320193" / "submissions.json").exists()
    # Second call must not hit HTTP (responses.assert_all_requests_are_fired is on by default,
    # so adding a single mock and calling twice would FAIL if both calls went out).
    second = dl.fetch_submissions(cik, limiter=limiter, user_agent="x x@x", raw_root=tmp_path)
    assert second == first


@responses.activate
def test_submissions_refresh_busts_cache(tmp_path, submissions):
    cik = 320193
    responses.add(responses.GET, edgar.submissions_url(cik), json=submissions, status=200)
    responses.add(responses.GET, edgar.submissions_url(cik), json={"cik": "0000320193", "filings": {"recent": {}, "files": []}}, status=200)
    limiter = edgar.RateLimiter(8)
    dl.fetch_submissions(cik, limiter=limiter, user_agent="x x@x", raw_root=tmp_path)
    refreshed = dl.fetch_submissions(cik, limiter=limiter, user_agent="x x@x", raw_root=tmp_path, refresh=True)
    assert refreshed["filings"]["recent"] == {}


def test_archives_doc_url():
    url = edgar.archives_doc_url(320193, "0000320193-23-000106", "aapl-20230930.htm")
    assert url == "https://www.sec.gov/Archives/edgar/data/320193/000032019323000106/aapl-20230930.htm"


def test_submissions_url():
    assert edgar.submissions_url(320193) == "https://data.sec.gov/submissions/CIK0000320193.json"


# --- stack-awareness ---------------------------------------------------------

def test_select_targets_sp500_stack(conn):
    """sp500 stack with no explicit ciks reads from sp500_membership."""
    conn.execute(
        "INSERT INTO sp500_membership(ticker, cik, name, sector, weight_pct, updated_at) "
        "VALUES ('AAPL', 320193, 'Apple', 'IT', 3.0, '2026-06-20'), "
        "       ('MSFT', 789019, 'Microsoft', 'IT', 2.5, '2026-06-20')"
    )
    conn.commit()
    from sibyl.download import _select_targets
    targets = _select_targets(conn, None, stack="sp500")
    assert sorted(t[0] for t in targets) == [320193, 789019]


def test_select_targets_queried_stack_requires_ciks(conn):
    """queried stack without explicit ciks returns empty (no implicit universe)."""
    from sibyl.download import _select_targets
    assert _select_targets(conn, None, stack="queried") == []
    assert _select_targets(conn, [320193], stack="queried") == [(320193, None)]


def test_select_targets_rejects_unknown_stack(conn):
    import pytest
    from sibyl.download import _select_targets
    with pytest.raises(ValueError, match="unknown stack"):
        _select_targets(conn, None, stack="bogus")


def test_insert_filing_writes_stack_column(conn, tmp_path):
    """Verify the stack column is populated correctly per the stack arg."""
    from sibyl.download import FilingRow, insert_filing
    row = FilingRow(
        accession="x-23-1", form_type="10-K", filing_date="2023-01-01",
        acceptance_dt="2023-01-01T00:00:00Z", period_of_report="2022-12-31",
        primary_doc="primary.htm",
    )
    raw_path = tmp_path / "queried" / "raw" / "100" / "x-23-1" / "primary.html.gz"
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    raw_path.touch()
    insert_filing(
        conn, cik=100, ticker="ZZZ", row=row, raw_path=raw_path,
        data_root=tmp_path, stack="queried",
    )
    got = conn.execute("SELECT stack FROM filings WHERE accession = ?", ("x-23-1",)).fetchone()
    assert got["stack"] == "queried"


# --- helpers ---

def _make_cfg(tmp_path: Path):
    """Build a minimal Config object pointing at tmp_path."""
    from sibyl.config import Config, SecConfig, UnicornConfig, UniverseConfig, _resolve_paths
    paths = _resolve_paths(tmp_path, None, None)
    for p in (paths.raw, paths.logs):
        p.mkdir(parents=True, exist_ok=True)
    return Config(
        paths=paths,
        sec=SecConfig(user_agent="Test ua@example.com", rate_limit_per_sec=8),
        unicorn=UnicornConfig(base_url="https://example", universe_path="/api/universe",
                              expected_contract_version="1.0", token="t"),
        universe=UniverseConfig(form_types=["10-K"], include_amendments=False,
                                history_start="2016-01-01"),
        download_gzip=True,
    )
