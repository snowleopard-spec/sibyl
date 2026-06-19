import json
import math
import sqlite3
from pathlib import Path

import pytest

from sibyl import diff as df
from sibyl import score as sc
from sibyl.db import init_schema


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    init_schema(c)
    return c


# --- cosine + tfidf vector ----------------------------------------------------

def test_cosine_identical_vectors_is_one():
    v = {"a": 1.0, "b": 2.0, "c": 3.0}
    assert df.cosine_sim(v, v) == pytest.approx(1.0)


def test_cosine_orthogonal_is_zero():
    assert df.cosine_sim({"a": 1.0}, {"b": 1.0}) == 0.0


def test_cosine_empty_inputs():
    assert df.cosine_sim({}, {"a": 1.0}) == 0.0
    assert df.cosine_sim({"a": 1.0}, {}) == 0.0


def test_cosine_partial_overlap():
    # |v1|=sqrt(2), |v2|=sqrt(5), dot = 1*1 + 1*0 + 0*2 = 1
    v1 = {"a": 1.0, "b": 1.0}
    v2 = {"a": 1.0, "c": 2.0}
    assert df.cosine_sim(v1, v2) == pytest.approx(1.0 / (math.sqrt(2) * math.sqrt(5)))


def test_tfidf_vector_skips_unknown_and_ubiquitous_tokens():
    # df includes "rare" (df=10, N=1000) and "common" (df=1000)
    df_table = {"rare": 10, "common": 1000}
    v = df.tfidf_vector(["rare", "rare", "common", "unknown"], df_table, n_docs=1000)
    # "common" → idf=0 → excluded. "unknown" → no df → excluded.
    assert set(v.keys()) == {"rare"}
    # weight = (1 + log10(2)) * log10(1000/10) = (1 + 0.30103) * 2
    assert v["rare"] == pytest.approx((1 + math.log10(2)) * 2)


def test_tfidf_vector_empty_inputs():
    assert df.tfidf_vector([], {"a": 1}, n_docs=10) == {}
    assert df.tfidf_vector(["a"], {"a": 1}, n_docs=0) == {}


# --- prior matching -----------------------------------------------------------

def _insert_filing(conn, accession, cik, form_type, period, accepted):
    conn.execute(
        "INSERT INTO filings (accession, cik, form_type, period_of_report, "
        "acceptance_dt, raw_path, parse_status, downloaded_at) "
        "VALUES (?, ?, ?, ?, ?, '/dev/null', 'ok', ?)",
        (accession, cik, form_type, period, accepted, accepted),
    )


def test_match_priors_chains_consecutive_10ks(conn):
    _insert_filing(conn, "0000000001-20-000001", 100, "10-K", "2019-12-31", "2020-01-15")
    _insert_filing(conn, "0000000001-21-000001", 100, "10-K", "2020-12-31", "2021-01-15")
    _insert_filing(conn, "0000000001-22-000001", 100, "10-K", "2021-12-31", "2022-01-15")
    conn.commit()
    priors = df.match_prior_filings(conn)
    assert priors == {
        "0000000001-21-000001": "0000000001-20-000001",
        "0000000001-22-000001": "0000000001-21-000001",
    }


def test_match_priors_does_not_cross_form_types(conn):
    _insert_filing(conn, "k20", 100, "10-K", "2019-12-31", "2020-01-15")
    _insert_filing(conn, "q20", 100, "10-Q", "2020-03-31", "2020-04-15")
    _insert_filing(conn, "k21", 100, "10-K", "2020-12-31", "2021-01-15")
    conn.commit()
    priors = df.match_prior_filings(conn)
    assert priors == {"k21": "k20"}   # 10-Q does not link to or from 10-K


def test_match_priors_first_per_cik_has_no_prior(conn):
    _insert_filing(conn, "a", 100, "10-K", "2020-12-31", "2021-01-15")
    _insert_filing(conn, "b", 200, "10-K", "2020-12-31", "2021-01-15")
    conn.commit()
    priors = df.match_prior_filings(conn)
    assert priors == {}


def test_match_priors_respects_cik_filter(conn):
    _insert_filing(conn, "a1", 100, "10-K", "2019-12-31", "2020-01-15")
    _insert_filing(conn, "a2", 100, "10-K", "2020-12-31", "2021-01-15")
    _insert_filing(conn, "b1", 200, "10-K", "2019-12-31", "2020-01-15")
    _insert_filing(conn, "b2", 200, "10-K", "2020-12-31", "2021-01-15")
    conn.commit()
    assert df.match_prior_filings(conn, ciks=[100]) == {"a2": "a1"}


# --- end-to-end: 2 filings of one CIK ----------------------------------------

def _write_filing_files(clean_root, cik, accession, *, full, rf, mdna):
    d = clean_root / str(cik) / accession
    d.mkdir(parents=True)
    (d / "full.txt").write_text(full, encoding="utf-8")
    (d / "risk_factors.txt").write_text(rf, encoding="utf-8")
    (d / "mdna.txt").write_text(mdna, encoding="utf-8")
    (d / "sections.json").write_text(json.dumps({
        "status": "ok",
        "full": {"status": "ok"},
        "risk_factors": {"status": "ok"},
        "mdna": {"status": "ok"},
    }))


@pytest.fixture
def two_year_corpus(tmp_path, conn):
    """Two 10-Ks for CIK 100: 2020 + 2021."""
    from sibyl.config import Config, Paths, SecConfig, UnicornConfig, UniverseConfig
    paths = Paths(
        data_root=tmp_path, raw=tmp_path / "raw", clean=tmp_path / "clean",
        logs=tmp_path / "logs", snapshots=tmp_path / "snapshots",
        universe_json=tmp_path / "universe.json", db=tmp_path / "sibyl.db",
        company_tickers=tmp_path / "company_tickers.json",
        lm_dictionary=tmp_path / "lm.csv",
        prices=tmp_path / "prices", exports=tmp_path / "exports",
    )
    cfg = Config(
        paths=paths,
        sec=SecConfig(user_agent="t", rate_limit_per_sec=1),
        unicorn=UnicornConfig(base_url="", universe_path="", expected_contract_version="1.0", token=None),
        universe=UniverseConfig(form_types=[], include_amendments=False, history_start="2016"),
        download_gzip=True,
    )
    (tmp_path / "lm.csv").write_text(
        "Word,Negative,Positive,Uncertainty,Litigious,Strong_Modal,Weak_Modal,Constraining\n"
        "risk,2009,0,0,0,0,0,0\n"
        "loss,2009,0,0,0,0,0,0\n"
        "may,0,0,2009,0,0,0,0\n"
        "court,0,0,0,2009,0,0,0\n"
    )
    # Prior year: low risk language. Current year: high.
    _write_filing_files(
        paths.clean, 100, "p20",
        full="stable business steady growth filler text",
        rf="stable business outlook",
        mdna="growth steady operations",
    )
    _write_filing_files(
        paths.clean, 100, "c21",
        full="risk loss court may filler text",
        rf="risk loss exposure increasing",
        mdna="loss provisions may increase",
    )
    _insert_filing(conn, "p20", 100, "10-K", "2019-12-31", "2020-01-15")
    _insert_filing(conn, "c21", 100, "10-K", "2020-12-31", "2021-01-15")
    conn.commit()
    sc.score_all(conn, cfg)
    return conn, cfg


def test_compute_all_writes_3_rows_for_paired_filings(two_year_corpus):
    conn, cfg = two_year_corpus
    counts = df.compute_all(conn, cfg)
    assert counts.processed == 1   # only "c21" has a prior
    assert counts.no_prior == 1
    assert counts.rows_written == 3   # 3 sections
    rows = list(conn.execute(
        "SELECT section, prior_accession, d_neg, d_unc, d_lit, similarity_yoy "
        "FROM filing_signals WHERE accession = 'c21' ORDER BY section"
    ))
    assert [r["section"] for r in rows] == ["full", "mdna", "risk_factors"]
    for r in rows:
        assert r["prior_accession"] == "p20"
        # All sections went from low-risk to high-risk → d_neg > 0.
        assert r["d_neg"] > 0
        # Similarity should be < 1 since text changed (and > 0 since some overlap).
        assert 0.0 <= r["similarity_yoy"] < 1.0


def test_compute_all_is_idempotent(two_year_corpus):
    conn, cfg = two_year_corpus
    df.compute_all(conn, cfg)
    counts = df.compute_all(conn, cfg)
    assert counts.processed == 0
    assert counts.skipped == 1


def test_compute_all_force_rewrites(two_year_corpus):
    conn, cfg = two_year_corpus
    df.compute_all(conn, cfg)
    counts = df.compute_all(conn, cfg, force=True)
    assert counts.processed == 1
    n = conn.execute("SELECT COUNT(*) FROM filing_signals").fetchone()[0]
    assert n == 3   # INSERT OR REPLACE → no duplicates


def test_skip_section_when_score_row_missing(two_year_corpus):
    """If prior's mdna proportional row is missing, only full + rf get diffed."""
    conn, cfg = two_year_corpus
    conn.execute(
        "DELETE FROM filing_scores WHERE accession='p20' AND section='mdna'"
    )
    conn.commit()
    counts = df.compute_all(conn, cfg, force=True)
    assert counts.rows_written == 2   # full + risk_factors only
    secs = {
        r["section"]
        for r in conn.execute("SELECT section FROM filing_signals WHERE accession='c21'")
    }
    assert secs == {"full", "risk_factors"}
