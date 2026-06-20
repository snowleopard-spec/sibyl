import json
import math
import sqlite3
from pathlib import Path

import pytest

from sibyl import score as sc
from sibyl.db import init_schema
from sibyl.lm_dictionary import CATEGORIES


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    init_schema(c)
    return c


# --- tokenize -----------------------------------------------------------------

def test_tokenize_lowercases_and_strips_punctuation():
    assert sc.tokenize("The Company's revenue.") == ["the", "company", "s", "revenue"]


def test_tokenize_strips_numbers():
    assert sc.tokenize("$1,234 USD in 2023.") == ["usd", "in"]


def test_tokenize_handles_unicode_quotes_and_dashes():
    assert sc.tokenize("forward—looking 'statement'") == ["forward", "looking", "statement"]


def test_tokenize_empty():
    assert sc.tokenize("") == []
    assert sc.tokenize("12345 -- !!") == []


# --- proportional --------------------------------------------------------------

def test_proportional_scores_basic():
    lm = {
        "Negative": {"risk", "loss"},
        "Positive": {"strong"},
        "Uncertainty": set(),
    }
    tokens = ["the", "risk", "is", "loss", "and", "loss"]
    out = sc.proportional_scores(tokens, lm)
    assert out["Negative"] == pytest.approx(3 / 6)   # "risk" + 2× "loss"
    assert out["Positive"] == 0.0
    assert out["Uncertainty"] == 0.0


def test_proportional_scores_empty_returns_zero():
    lm = {"Negative": {"risk"}}
    assert sc.proportional_scores([], lm) == {"Negative": 0.0}


# --- tfidf ---------------------------------------------------------------------

def test_tfidf_zero_when_df_missing():
    """Tokens not present in DF carry no signal — should not contribute."""
    lm = {"Negative": {"risk"}}
    df: dict[str, int] = {}
    out = sc.tfidf_scores(["risk", "risk"], lm, df, n_docs=10)
    assert out["Negative"] == 0.0


def test_tfidf_zero_when_token_in_every_doc():
    """idf = log10(N/df) = 0 when df == N → contributes nothing."""
    lm = {"Negative": {"risk"}}
    out = sc.tfidf_scores(["risk"], lm, df={"risk": 10}, n_docs=10)
    assert out["Negative"] == 0.0


def test_tfidf_formula_matches_spec():
    """(1 + log10(tf)) * log10(N/df), summed over category, /total_words."""
    lm = {"Negative": {"risk"}}
    tokens = ["risk", "risk", "noise"]  # tf("risk")=2
    df = {"risk": 100, "noise": 1000}
    n_docs = 10_000
    # weight = (1 + log10(2)) * log10(10000/100) = (1 + 0.30103) * 2 = 2.60206
    expected = (1 + math.log10(2)) * math.log10(10_000 / 100) / 3
    out = sc.tfidf_scores(tokens, lm, df, n_docs)
    assert out["Negative"] == pytest.approx(expected)


# --- score_filing end-to-end --------------------------------------------------

def _write_filing(clean_root: Path, cik: int, accession: str, *,
                  full: str, rf: str = "", mdna: str = "",
                  rf_status: str = "ok", mdna_status: str = "ok"):
    d = clean_root / str(cik) / accession
    d.mkdir(parents=True)
    (d / "full.txt").write_text(full, encoding="utf-8")
    (d / "risk_factors.txt").write_text(rf, encoding="utf-8")
    (d / "mdna.txt").write_text(mdna, encoding="utf-8")
    (d / "sections.json").write_text(json.dumps({
        "status": "ok",
        "full": {"status": "ok"},
        "risk_factors": {"status": rf_status},
        "mdna": {"status": mdna_status},
    }))


def test_score_filing_writes_6_rows_when_all_sections_ok(tmp_path):
    lm = {"Negative": {"risk"}, "Positive": {"strong"}, "Uncertainty": set(),
          "Litigious": set(), "Strong_Modal": set(), "Weak_Modal": set(),
          "Constraining": set()}
    _write_filing(tmp_path, 1, "0000000001-23-000001",
                  full="Risk and strong",
                  rf="The risk is real",
                  mdna="Strong performance")
    rows = sc.score_filing(
        1, "0000000001-23-000001",
        clean_root=tmp_path, lm=lm, df={"risk": 1, "strong": 1, "the": 1, "is": 1,
                                         "real": 1, "performance": 1, "and": 1},
        n_docs=100,
    )
    assert len(rows) == 6
    sections = {r["section"] for r in rows}
    weightings = {r["weighting"] for r in rows}
    assert sections == {"full", "risk_factors", "mdna"}
    assert weightings == {"proportional", "tfidf"}
    for r in rows:
        assert r["scorer_version"] == sc.SCORER_VERSION
        assert r["total_words"] > 0


def test_score_filing_skips_section_when_status_not_ok(tmp_path):
    lm = {c: set() for c in CATEGORIES}
    _write_filing(tmp_path, 1, "0000000001-23-000001",
                  full="hello world",
                  rf="some risk text",
                  mdna_status="incorp_ref")
    rows = sc.score_filing(
        1, "0000000001-23-000001",
        clean_root=tmp_path, lm=lm, df={"hello": 1, "world": 1, "some": 1,
                                         "risk": 1, "text": 1}, n_docs=10,
    )
    # full + risk_factors only, both weightings = 4 rows
    assert len(rows) == 4
    assert all(r["section"] in ("full", "risk_factors") for r in rows)


# --- corpus-level (compute_doc_frequencies + score_all) -----------------------

@pytest.fixture
def populated_corpus(tmp_path, conn) -> tuple[sqlite3.Connection, "Config"]:  # noqa: F821
    """Two fake filings on disk, both rows in `filings` table."""
    from dataclasses import replace
    from sibyl.config import Config, SecConfig, UniverseConfig, _resolve_paths
    paths = replace(
        _resolve_paths(tmp_path, None, None),
        lm_dictionary=tmp_path / "lm.csv",
    )
    cfg = Config(
        paths=paths,
        sec=SecConfig(user_agent="t", rate_limit_per_sec=1),
        universe=UniverseConfig(form_types=[], include_amendments=False, history_start="2016"),
        download_gzip=True,
    )
    # Write tiny L&M dictionary CSV so load_master_dictionary works.
    (tmp_path / "lm.csv").write_text(
        "Word,Negative,Positive,Uncertainty,Litigious,Strong_Modal,Weak_Modal,Constraining\n"
        "risk,2009,0,0,0,0,0,0\n"
        "strong,0,2009,0,0,0,0,0\n"
        "may,0,0,2009,0,0,0,0\n"
    )

    _write_filing(paths.clean, 1, "0000000001-23-000001",
                  full="risk strong may filler",
                  rf="risk risk", mdna="strong may")
    _write_filing(paths.clean, 2, "0000000002-23-000002",
                  full="strong filler filler",
                  rf="risk filler", mdna="may filler")

    for cik, acc in ((1, "0000000001-23-000001"), (2, "0000000002-23-000002")):
        conn.execute(
            "INSERT INTO filings (accession, cik, form_type, acceptance_dt, raw_path, parse_status, downloaded_at) "
            "VALUES (?, ?, '10-K', '2023-01-01', '/dev/null', 'ok', '2023-01-01')",
            (acc, cik),
        )
    conn.commit()
    return conn, cfg


def test_compute_doc_frequencies(populated_corpus):
    conn, cfg = populated_corpus
    df, n_docs = sc.compute_doc_frequencies(conn, cfg)
    assert n_docs == 2
    assert df["risk"] == 1     # only in filing 1's full.txt
    assert df["strong"] == 2   # in both
    assert df["filler"] == 2   # in both


def test_score_all_writes_rows_and_is_idempotent(populated_corpus):
    conn, cfg = populated_corpus
    counts1 = sc.score_all(conn, cfg)
    assert counts1.processed == 2
    assert counts1.rows_written == 12   # 2 filings × 3 sections × 2 weightings
    assert counts1.skipped == 0

    # Re-run: everything should skip.
    counts2 = sc.score_all(conn, cfg)
    assert counts2.processed == 0
    assert counts2.skipped == 2

    # --force re-scores everything.
    counts3 = sc.score_all(conn, cfg, force=True)
    assert counts3.processed == 2
    assert counts3.rows_written == 12


def test_score_all_force_does_not_duplicate_rows(populated_corpus):
    conn, cfg = populated_corpus
    sc.score_all(conn, cfg)
    sc.score_all(conn, cfg, force=True)
    n = conn.execute("SELECT COUNT(*) FROM filing_scores").fetchone()[0]
    assert n == 12   # INSERT OR REPLACE prevents duplicates
