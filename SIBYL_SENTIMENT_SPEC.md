# Sibyl — `sentiment-tool-only` Branch Build Spec

## Purpose

This document is a complete build specification for Claude Code. It describes the creation of a new Git branch `sentiment-tool-only` of the Sibyl codebase, which is a stripped-down, self-contained S&P 500 sentiment monitor using Loughran-McDonald (LM) negative sentiment scoring. The tool scores MD&A and Risk Factors sections of each S&P 500 constituent's last 4 10-Q filings, ranks constituents into deciles, and serves results via a local Flask web page.

---

## 1. Branch Setup

### 1.1 Source branches

- The main codebase lives at: `https://github.com/snowleopard-spec/sibyl`
- Branch off **`main`**, not `research-tool`
- Cherry-pick the following files from the `research-tool` branch (do not copy files not listed here)

### 1.2 Files to carry across from `research-tool`

| File | Notes |
|---|---|
| `sibyl/wiki.py` | Wikipedia S&P 500 scraper — carry across unchanged |
| `sibyl/sp500.py` | Membership refresh orchestration — carry across unchanged |
| `sibyl/tickers.py` | CIK resolution — carry across unchanged |
| `sibyl/edgar.py` | EDGAR HTTP + rate limiter — carry across unchanged |
| `sibyl/download.py` | Filing downloader — carry across unchanged |
| `sibyl/parse.py` | HTML→text parser — carry across unchanged |
| `sibyl/sections.py` | MD&A + Risk Factors extraction — carry across unchanged |
| `sibyl/score.py` | LM scoring — carry across unchanged |
| `sibyl/lm_dictionary.py` | LM dictionary loader — carry across unchanged |
| `sibyl/config.py` | Two-stack path config — carry across, no changes needed |
| `sibyl/db.py` | Schema — carry across with modifications per §3 |
| `sibyl/cli.py` | CLI entrypoint — carry across with modifications per §6 |

### 1.3 Files to drop entirely (do not carry across)

- `sibyl/diff.py`
- `sibyl/aggregate.py`
- `sibyl/chart.py`
- `sibyl/queried.py`
- `sibyl/runner.py`

### 1.4 New files to create

- `sibyl/rank.py`
- `sibyl/serve.py`
- `templates/report.html`

---

## 2. Corpus / Data Directory

The existing 5-year filing corpus was built on `research-tool`. **Do not re-pull.** The `data/` directory from `research-tool` should be copied across and reused as-is. The database (`data/sibyl.db`) and all raw/clean filing directories are compatible — the schema changes in §3 are additive-only (table removal) and do not require migration.

---

## 3. Database Schema (`sibyl/db.py`)

### 3.1 Tables to remove

Remove the following table definitions and their associated indices from the `SCHEMA` string:

- `filing_signals`
- `sp500_aggregates`

These tables will no longer be created on `init_schema()`. If they already exist in a legacy database, they can be left in place (no `DROP TABLE` required — just stop creating them).

### 3.2 Tables to retain (unchanged)

- `filings` — unchanged
- `sp500_membership` — unchanged
- `filing_scores` — unchanged

### 3.3 Schema summary (what `SCHEMA` should contain after edits)

```sql
CREATE TABLE IF NOT EXISTS filings (
    accession         TEXT PRIMARY KEY,
    cik               INTEGER NOT NULL,
    ticker            TEXT,
    form_type         TEXT NOT NULL,
    period_of_report  TEXT,
    acceptance_dt     TEXT NOT NULL,
    filing_date       TEXT,
    primary_doc       TEXT,
    raw_path          TEXT NOT NULL,
    parse_status      TEXT,
    stack             TEXT NOT NULL DEFAULT 'sp500',
    downloaded_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_filings_cik    ON filings(cik);
CREATE INDEX IF NOT EXISTS idx_filings_form   ON filings(form_type);
CREATE INDEX IF NOT EXISTS idx_filings_accept ON filings(acceptance_dt);

CREATE TABLE IF NOT EXISTS sp500_membership (
    ticker      TEXT PRIMARY KEY,
    cik         INTEGER,
    name        TEXT,
    sector      TEXT,
    weight_pct  REAL,
    updated_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sp500_membership_sector ON sp500_membership(sector);
CREATE INDEX IF NOT EXISTS idx_sp500_membership_cik    ON sp500_membership(cik);

CREATE TABLE IF NOT EXISTS filing_scores (
    accession      TEXT NOT NULL,
    section        TEXT NOT NULL,
    weighting      TEXT NOT NULL,
    scorer_version TEXT NOT NULL DEFAULT '1',
    total_words    INTEGER,
    neg            REAL,
    pos            REAL,
    unc            REAL,
    lit            REAL,
    strong_modal   REAL,
    weak_modal     REAL,
    constraining   REAL,
    scored_at      TEXT,
    PRIMARY KEY (accession, section, weighting),
    FOREIGN KEY (accession) REFERENCES filings(accession)
);
```

---

## 4. Corpus Pipeline (`sibyl refresh`)

### 4.1 What it does

Runs the full update sequence in order:

1. Refresh S&P 500 membership from Wikipedia → upsert `sp500_membership`
2. Download missing 10-Q filings for all CIKs in `sp500_membership`
3. Parse HTML → plain text
4. Extract `mdna` and `risk_factors` sections
5. Score both sections with LM proportional weighting → write to `filing_scores`

### 4.2 Notes

- Form type filter: **10-Q only**. Do not download or process 10-K filings for this branch.
- Run manually from the command line. No scheduling in this phase.
- The command should log progress at each stage to stderr.
- On completion, write a refresh timestamp to `data/last_refresh.txt` (ISO 8601 UTC). This is read by the Flask server for the "Last updated" display.

### 4.3 CLI invocation

```bash
sibyl refresh
```

---

## 5. Ranking Logic (`sibyl/rank.py`)

### 5.1 Function signature

```python
def compute_ranks(conn: sqlite3.Connection, *, n_filings: int = 4) -> pd.DataFrame:
    ...
```

Returns a pandas DataFrame with one row per ticker.

### 5.2 Query logic

For each ticker in `sp500_membership`:

1. Join `filing_scores` → `filings` → `sp500_membership` on CIK
2. Filter: `filings.form_type = '10-Q'`
3. Filter: `filing_scores.weighting = 'proportional'`
4. Filter: `filing_scores.section IN ('mdna', 'risk_factors')`
5. Order by `filings.acceptance_dt DESC`
6. Take the **4 most recent filings** per ticker per section
7. Compute `mean(neg)` across those filings, separately for `mdna` and `risk_factors`
8. Also store `filing_count_mdna` and `filing_count_risk` (number of filings that contributed to each mean — may be <4 for recent index additions)

### 5.3 Output DataFrame columns

| Column | Type | Description |
|---|---|---|
| `ticker` | str | Ticker symbol |
| `name` | str | Company name from `sp500_membership` |
| `sector` | str | GICS sector from `sp500_membership` |
| `mean_neg_mdna` | float | Mean LM neg score across last 4 10-Qs, MD&A section |
| `mean_neg_risk` | float | Mean LM neg score across last 4 10-Qs, Risk Factors section |
| `filing_count_mdna` | int | Number of filings used for mdna mean |
| `filing_count_risk` | int | Number of filings used for risk mean |
| `decile_mdna` | int | Decile rank 1–10 on mean_neg_mdna (1=least negative, 10=most negative) |
| `decile_risk` | int | Decile rank 1–10 on mean_neg_risk (1=least negative, 10=most negative) |

### 5.4 Exclusion filter

Exclude tickers where `filing_count_mdna < 2` OR `filing_count_risk < 2`. For the S&P 500 this filter is expected to be a no-op except for very recent index additions.

### 5.5 Decile assignment

Use `pd.qcut` with `q=10` and `labels=range(1, 11)` for decile assignment. Apply separately to `mean_neg_mdna` and `mean_neg_risk`. Use `duplicates='drop'` to handle ties gracefully.

### 5.6 Individual filing scores

In addition to the aggregated DataFrame, expose a second function:

```python
def get_filing_detail(conn: sqlite3.Connection, ticker: str) -> pd.DataFrame:
    ...
```

Returns the individual filing-level scores for a single ticker — one row per (filing, section), columns: `accession`, `acceptance_dt`, `period_of_report`, `section`, `neg`, `total_words`. Used by the Flask server to populate the per-ticker detail rows.

---

## 6. CLI (`sibyl/cli.py`)

### 6.1 Remove from cli.py

Remove all imports and command functions referencing:
- `diff`
- `aggregate`
- `chart`
- `queried`
- `runner`

### 6.2 Commands to retain

- `sibyl universe` — retain if present on main, otherwise skip
- `sibyl status` — retain
- `sibyl download` — retain
- `sibyl parse` — retain
- `sibyl score` — retain

### 6.3 New commands to add

| Command | Function | Description |
|---|---|---|
| `sibyl refresh` | `cmd_refresh` | Full pipeline: membership → download → parse → sections → score. Writes `data/last_refresh.txt` on completion. |
| `sibyl rank` | `cmd_rank` | Compute deciles, print a terminal summary table (ticker, sector, decile_mdna, decile_risk). Useful for quick checks without starting the server. |
| `sibyl serve` | `cmd_serve` | Start local Flask server at `localhost:5000`. |

---

## 7. Flask Server (`sibyl/serve.py`)

### 7.1 Dependencies

Add to `pyproject.toml` dependencies:
- `flask>=3.0`
- `pandas>=2.0`

### 7.2 Routes

| Route | Description |
|---|---|
| `/` | Main report page — renders `templates/report.html` |

### 7.3 Data passed to template

```python
{
    "last_refresh": str,          # contents of data/last_refresh.txt, or "Never"
    "deciles": {                  # dict keyed 1–10
        1: {
            "mdna": [...],        # list of row dicts for decile 1, mdna ranking
            "risk": [...],        # list of row dicts for decile 1, risk ranking
        },
        ...
        10: { ... }
    },
    "sector_colours": { ... },    # dict mapping sector name → hex colour string
    "score_summary": {
        "mdna": {
            "decile_boundaries": [float, ...],   # 11 boundary values (min + 9 cuts + max)
            "n_scored": int,
        },
        "risk": { ... }
    }
}
```

Each row dict in `deciles[n]["mdna"]` and `deciles[n]["risk"]`:

```python
{
    "rank": int,               # rank within decile (1 = highest neg in decile)
    "ticker": str,
    "name": str,
    "sector": str,
    "mean_neg": float,         # mean_neg_mdna or mean_neg_risk depending on table
    "filing_count": int,
    "filings": [               # individual filing rows from get_filing_detail()
        {
            "acceptance_dt": str,
            "period_of_report": str,
            "section": str,
            "neg": float,
            "total_words": int,
        },
        ...
    ]
}
```

### 7.4 Sector colour palette

Assign one distinct hex colour per GICS sector. Use a fixed mapping so colours are stable across refreshes. Suggested palette (11 GICS sectors):

```python
SECTOR_COLOURS = {
    "Information Technology":  "#4C72B0",
    "Health Care":             "#DD8452",
    "Financials":              "#55A868",
    "Consumer Discretionary":  "#C44E52",
    "Industrials":             "#8172B3",
    "Communication Services":  "#937860",
    "Consumer Staples":        "#DA8BC3",
    "Energy":                  "#8C8C8C",
    "Utilities":               "#CCB974",
    "Real Estate":             "#64B5CD",
    "Materials":               "#A6D854",
}
```

### 7.5 Run command

```bash
sibyl serve
# or equivalently:
flask --app sibyl.serve run --port 5000
```

---

## 8. HTML Template (`templates/report.html`)

### 8.1 Layout

```
┌─────────────────────────────────────────────────┐
│  Sibyl — S&P 500 Sentiment Monitor              │
│  Last updated: {last_refresh}                   │
│  {n_scored} constituents scored                 │
├──────────┬──────────────────────────────────────┤
│ Sidebar  │  Main content area                   │
│          │                                      │
│ Decile 1 │  [Decile N — MD&A]  [Decile N — RF] │
│ Decile 2 │                                      │
│ ...      │  MD&A table | Risk Factors table     │
│ Decile 10│  (side by side)                      │
└──────────┴──────────────────────────────────────┘
```

### 8.2 Sidebar

- Fixed left sidebar listing "Decile 1" through "Decile 10" as anchor links
- Clicking scrolls to the relevant decile section
- Active decile highlighted on scroll (use Intersection Observer in vanilla JS)

### 8.3 Decile sections

Each decile section contains:
- A heading: "Decile N"
- Two tables side by side: **MD&A** and **Risk Factors**
- Each table has columns: Rank | Ticker | Name | Sector | Mean Neg Score | Filings Used
- Each row has a left border or background tint in the sector colour
- Each row is expandable (click to expand) to reveal the individual filing scores sub-table

### 8.4 Expandable filing detail

On row click, expand inline to show a sub-table:

| Filing Date | Period | Section | Neg Score | Word Count |
|---|---|---|---|---|
| 2024-11-14 | 2024-09-30 | mdna | 0.0234 | 12450 |
| 2024-08-09 | 2024-06-30 | mdna | 0.0198 | 11823 |
| ... | | | | |

Show both `mdna` and `risk_factors` rows for that ticker in the same expanded section, grouped by section. Implement expand/collapse in vanilla JS — no frameworks required.

### 8.5 Score summary bar

At the top of the main content area, above the decile sections, display a small summary block:

```
MD&A: 487 constituents scored | Decile boundaries: 0.012 | 0.018 | 0.023 | ... | 0.051
Risk: 487 constituents scored | Decile boundaries: 0.031 | 0.044 | ...
```

### 8.6 Styling

- Clean, minimal styling — no CSS frameworks, plain CSS in a `<style>` block
- White background, dark text
- Sidebar fixed-width (~160px), content area takes remaining width
- Tables: alternating row shading within each decile, sector colour as a 4px left border on each row
- Monospace font for score values
- Responsive is not required — desktop only

---

## 9. `pyproject.toml` Changes

Add to `dependencies`:

```toml
"flask>=3.0",
"pandas>=2.0",
```

All other dependencies carry across from `research-tool` unchanged.

---

## 10. File / Directory Structure

```
sibyl/                        ← Python package
    __init__.py
    cli.py                    ← modified
    config.py                 ← from research-tool, unchanged
    db.py                     ← from research-tool, trimmed
    download.py               ← from research-tool, unchanged
    edgar.py                  ← from research-tool, unchanged
    lm_dictionary.py          ← from research-tool, unchanged
    parse.py                  ← from research-tool, unchanged
    rank.py                   ← NEW
    score.py                  ← from research-tool, unchanged
    sections.py               ← from research-tool, unchanged
    serve.py                  ← NEW
    sp500.py                  ← from research-tool, unchanged
    tickers.py                ← from research-tool, unchanged
    wiki.py                   ← from research-tool, unchanged

templates/
    report.html               ← NEW

data/                         ← copied from research-tool; not committed to Git
    sibyl.db
    sp500/
        raw/
        clean/
        membership_snapshots/
    logs/
    company_tickers.json
    lm_master_dictionary.csv
    last_refresh.txt          ← written by `sibyl refresh`

pyproject.toml                ← updated
README.md                     ← update to reflect new commands
```

---

## 11. README Updates

Update `README.md` to reflect:

- Remove all references to `diff`, `aggregate`, `chart`, `queried`, `runner`
- Update CLI table to show only: `sibyl refresh`, `sibyl rank`, `sibyl serve`, `sibyl status`
- Add a "Quickstart" section:

```bash
# 1. Install
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

# 2. Configure
cp config.example.yaml config.yaml
# edit sec.user_agent

# 3. Copy existing corpus (do not re-pull)
# cp -r /path/to/research-tool/data ./data

# 4. Refresh membership + score any new filings
sibyl refresh

# 5. Serve
sibyl serve
# Open http://localhost:5000
```

---

## 12. Out of Scope

The following are explicitly excluded from this build:

- Trend signals (↑↓) across filings
- Sector filtering in the UI (sector column is present in the data; filtering is a future addition)
- Queried stack / ad-hoc ticker lookup for non-S&P names
- `similarity_yoy`, `d_neg`, `d_unc` signals
- Any charting or matplotlib output
- 10-K scoring (10-Q only)
- Droplet deployment (local only in this phase; cron + Caddy + gunicorn to be added post-migration)
- Authentication or multi-user access

---

## 13. Droplet Migration (Post-Local — Not in Scope Now)

When ready to deploy:

1. Copy codebase and `data/` to droplet
2. Add cron for `sibyl refresh` (monthly cadence, aligned to 10-Q filing season)
3. Serve via gunicorn behind Caddy reverse proxy, same pattern as Unicorn Hunt (`api.unicornpunk.org`)
4. Switch Flask from dev server to `gunicorn sibyl.serve:app`
