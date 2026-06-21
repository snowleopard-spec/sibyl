# Sibyl Research Tool — Build Spec

*Branch: `research-tool`. Pivot from the small-cap signal engine documented in `SIBYL BUILD SPEC.md`. That earlier spec stays in git history as the reference for the EDGAR pipeline modules (download / parse / sections / score / diff) which this tool reuses ~70% as-is.*

---

## 1. What this is

A query-driven research tool that compares the L&M sentiment and yoy textual-similarity signals of any listed stock against:
- The S&P 500 cross-sectional average
- The S&P 500 same-sector average

For every queried ticker the user gets six time-series chart panels (Risk Factors and MD&A, each with Δneg, Δunc, and yoy similarity) showing the queried stock against the two reference series.

The S&P 500 acts as a **continuously-refreshed baseline corpus**. Queried tickers are scored on demand and cached. Both stacks are persistent.

This is a research / analyst tool, not a tradeable signal generator. There is no IC harness, no backtest, no signal export to Unicorn Hunt. Those Phase 2/3 plans from the original Sibyl spec are shelved.

---

## 2. Architecture principles

1. **Two stacks, fully isolated on disk**: `data/sp500/` and `data/queried/`. Each stack has its own `raw/`, `clean/`, and record file. One stack can be tar'd / moved / wiped independently.
2. **Dedup across stacks via cross-reference**: when a queried ticker is also an S&P member, the queried record points at the S&P stack's files instead of duplicating bytes.
3. **Record file is the human-readable source of truth**; SQLite is the queryable index. Record files (`record.jsonl`) are append-only JSON Lines, one row per filing. The SQLite `filings` table mirrors them and gains a `stack` column.
4. **Rolling 5-year window**. Filings older than 5 years from "today" are evicted on each refresh. Steady-state disk size.
5. **Use most-recent S&P membership only**. IVV CSV snapshots are saved each pull for audit, but all aggregation uses the latest snapshot. PIT membership analysis is a future option, not v1 scope.
6. **Reuse Sibyl's EDGAR pipeline**. The `download / parse / sections / score / diff` modules are kept; the only structural changes are stack-awareness in I/O paths and form-type-aware behavior (10-Q support in `sections.py` and `diff.py`).
7. **Local-first, deploy-second**. Develop on the Mac. Production target is the existing Unicorn droplet (`161.35.122.12`); add Sibyl alongside `unicornhunt.service`.
8. **Fail loud on edge cases**. Unmappable tickers, missing IVV columns, 10-Q parsing failures — surface immediately. Better to know than silently degrade.

---

## 3. Project layout

```
sibyl/
├── pyproject.toml           # adds: pandas (charting), matplotlib
├── README.md
├── config.example.yaml
├── config.yaml              # gitignored
├── .env                     # gitignored
│
├── sibyl/                   # the package
│   ├── __init__.py
│   ├── cli.py               # argparse entrypoint (existing + new commands)
│   ├── config.py            # adds: sp500/queried path resolution
│   ├── db.py                # adds: `stack` column on filings + helpers
│   ├── edgar.py             # unchanged
│   │
│   ├── runner.py            # NEW: full-workflow orchestrator (see §6)
│   │
│   # --- Stack acquisition ---
│   ├── sp500.py             # NEW: S&P 500 universe from IVV CSV
│   ├── queried.py           # NEW: queried-stack management
│   ├── tickers.py           # NEW: ticker → CIK resolution
│   │
│   # --- Reused pipeline (lightly extended) ---
│   ├── download.py          # extended: --stack arg, target folder per stack
│   ├── parse.py             # unchanged
│   ├── sections.py          # extended: 10-Q path via edgartools.TenQ
│   ├── score.py             # extended: --stack arg
│   ├── diff.py              # extended: --stack arg + same-quarter prior matching for 10-Q
│   ├── lm_dictionary.py     # unchanged
│   │
│   # --- New analysis layer ---
│   ├── aggregate.py         # NEW: rolling sector + S&P averages per date
│   ├── chart.py             # NEW: matplotlib PNG output
│   │
│   # --- Shelved (kept in tree, not invoked) ---
│   ├── signals.py           # was Phase-2 stub; left as-is
│   ├── prices.py            # was Phase-2 stub; left as-is
│   ├── export.py            # was Phase-3 stub; left as-is
│   └── eval/__init__.py     # stub
│
├── scripts/
│   ├── prepare_audit.py     # carry over from main branch
│   ├── aggregate_audit.py
│   └── remediate_incorp_ref.py
│
├── tests/                   # extend existing suite
│   ├── test_sp500.py        # NEW
│   ├── test_queried.py      # NEW
│   ├── test_tickers.py      # NEW
│   ├── test_aggregate.py    # NEW
│   ├── test_runner.py       # NEW
│   ├── test_sections_10q.py # NEW
│   └── (existing tests carry over)
│
└── data/                    # GITIGNORED
    ├── sibyl.db                       # SQLite index (both stacks; `stack` column)
    ├── company_tickers.json           # SEC ticker → CIK cache
    ├── lm_master_dictionary.csv
    ├── logs/
    │
    ├── sp500/                         # S&P 500 stack
    │   ├── raw/<CIK>/<accession>/primary.html.gz + metadata.json
    │   ├── clean/<CIK>/<accession>/full.txt, risk_factors.txt, mdna.txt, sections.json
    │   ├── record.jsonl               # append-only filing log
    │   └── membership_snapshots/      # ivv_<YYYY-MM-DD>.csv per pull
    │
    └── queried/                       # On-demand stack
        ├── raw/<CIK>/<accession>/...  # only for queried tickers NOT in S&P
        ├── clean/<CIK>/<accession>/...
        └── record.jsonl
```

---

## 4. Data model

### 4.1 Record file format

One JSON object per line, append-only. Schema:

```jsonl
{"stack":"sp500","cik":320193,"ticker":"AAPL","sector":"Information Technology","accession":"0000320193-23-000106","form_type":"10-K","period_of_report":"2023-09-30","acceptance_dt":"2023-11-02T18:08:43-04:00","downloaded_at":"2026-06-20T12:00:00Z","parsed_at":"2026-06-20T12:01:00Z","scored_at":"2026-06-20T12:02:00Z","diffed_at":"2026-06-20T12:03:00Z","raw_ref":"data/sp500/raw/320193/0000320193-23-000106/primary.html.gz"}
```

For queried-stack records pointing at an S&P file, `raw_ref` starts with `data/sp500/...` — that's how cross-stack dedup is encoded. The queried record exists; the bytes don't.

**Why JSONL not CSV/JSON?** Append-only writes are crash-safe (no rewrite of full file). Easy to grep / `jq` for ad-hoc inspection. Each row is self-describing. The corpus is small (~12K rows for S&P + few hundred for queried) so single-file scans are fine.

**Why also SQLite?** The `filings` table already exists and the rest of the pipeline reads from it. JSONL is for humans; SQLite is for queries. They're kept in sync: the runner re-derives SQLite from the JSONL on every refresh (idempotent: `INSERT OR REPLACE` keyed on accession).

### 4.2 SQLite schema additions

```sql
-- New column on existing filings table:
ALTER TABLE filings ADD COLUMN stack TEXT NOT NULL DEFAULT 'sp500';
CREATE INDEX IF NOT EXISTS idx_filings_stack ON filings(stack);

-- New table for sector membership (refreshed from IVV monthly):
CREATE TABLE IF NOT EXISTS sp500_membership (
    ticker        TEXT PRIMARY KEY,
    cik           INTEGER,
    name          TEXT,
    sector        TEXT,
    weight_pct    REAL,
    updated_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sp500_sector ON sp500_membership(sector);

-- New table for rolling aggregates (populated by aggregate.py):
CREATE TABLE IF NOT EXISTS sp500_aggregates (
    as_of_date     TEXT NOT NULL,         -- end of quarter, e.g. '2024-Q3' → '2024-09-30'
    scope          TEXT NOT NULL,         -- 'sp500' or sector name
    section        TEXT NOT NULL,         -- 'risk_factors' | 'mdna'
    metric         TEXT NOT NULL,         -- 'd_neg' | 'd_unc' | 'similarity_yoy'
    mean_value     REAL,
    median_value   REAL,
    n_filings      INTEGER,
    computed_at    TEXT,
    PRIMARY KEY (as_of_date, scope, section, metric)
);
```

The existing `filing_scores` and `filing_signals` tables are unchanged. Queries filter by `accession IN (SELECT accession FROM filings WHERE stack = ?)`.

### 4.3 Stack-aware filesystem paths

```python
# sibyl/config.py additions
@dataclass(frozen=True)
class Paths:
    # ... existing fields ...
    sp500_raw:    Path  # data/sp500/raw
    sp500_clean:  Path  # data/sp500/clean
    sp500_record: Path  # data/sp500/record.jsonl
    sp500_snapshots: Path  # data/sp500/membership_snapshots
    queried_raw:    Path
    queried_clean:  Path
    queried_record: Path

def stack_raw(cfg: Config, stack: str) -> Path:
    return cfg.paths.sp500_raw if stack == "sp500" else cfg.paths.queried_raw
# (similar for clean / record)
```

The legacy single-stack `cfg.paths.raw` / `cfg.paths.clean` are removed.

---

## 5. New modules

### 5.1 `sibyl/sp500.py` — S&P universe acquisition

Pulls iShares IVV holdings CSV from BlackRock's public URL. Format (current as of 2026):

```
https://www.ishares.com/us/products/239726/ishares-core-sp-500-etf/1467271812596.ajax?fileType=csv&fileName=IVV_holdings&dataType=fund
```

The CSV header is a stanza of fund metadata, then a blank line, then the holdings table. Columns of interest:
- `Ticker`
- `Name`
- `Sector` (GICS-ish, ~11 distinct values)
- `Weight (%)`

Procedure:
1. HTTP GET the URL; respect a reasonable User-Agent (we already set one for SEC).
2. Save raw CSV to `data/sp500/membership_snapshots/ivv_<YYYY-MM-DD>.csv`.
3. Parse the holdings table.
4. Filter to equity rows (drops cash, derivatives, futures — `Asset Class == "Equity"`).
5. Resolve each ticker → CIK via `sibyl/tickers.py`.
6. Log + report unmappable tickers (loud; non-fatal — they're skipped from the analysis stack but recorded).
7. Upsert `sp500_membership` table in SQLite.
8. Return the list of resolved `(cik, ticker, sector)` for the runner.

Module-level flag at the top of the file:

```python
DOWNLOAD_MISSING_FILINGS = True  # set False to refresh membership only
```

(Also exposed as `--download / --no-download` on the CLI.)

### 5.2 `sibyl/tickers.py` — Ticker → CIK resolution

Wraps SEC's `company_tickers.json` (existing cache at `data/company_tickers.json`). Refresh cadence: weekly (timestamp check on file mtime).

```python
def resolve(ticker: str, *, refresh: bool = False) -> int:
    """Return CIK for ticker. Raises LookupError if unmappable."""
```

Edge cases:
- **Dotted share classes** (BRK.B, BF.B): SEC uses dashes (BRK-B); the resolver normalizes `.` → `-` automatically.
- **Foreign listings / ADRs**: most have CIKs (e.g., BABA = CIK 1577552). Fail-loud if no CIK exists; user can investigate.
- **Recent IPOs**: SEC's file lags by ~24h. Fail-loud; suggest re-running tomorrow.

### 5.3 `sibyl/queried.py` — Queried-stack management

```python
def get_or_fetch(ticker: str, *, download: bool = True) -> list[FilingRecord]:
    """
    1. Resolve ticker → CIK.
    2. Look up CIK in the SP500 stack — if present, return its FilingRecords (cross-ref).
    3. Look up CIK in the queried stack — return what's there.
    4. If `download` and history is incomplete (less than 5y of 10-Ks + 10-Qs):
         a. Pull SEC submissions JSON
         b. Filter to 10-K + 10-Q within rolling 5y
         c. Download missing filings into data/queried/raw/<CIK>/...
         d. Run parse → sections → score → diff for the new filings
         e. Append rows to data/queried/record.jsonl
    5. Return the consolidated list of FilingRecords for charting.
    """
```

Module-level flag: `DOWNLOAD_MISSING_FILINGS = True`.

### 5.4 `sibyl/aggregate.py` — Rolling averages

Computes the S&P-wide and per-sector mean / median of each metric per quarter. Materializes to `sp500_aggregates` table for fast chart lookups.

```python
def rebuild_aggregates(conn, *, as_of: str | None = None) -> None:
    """Recompute all aggregates. Cheap: ~12K rows GROUP BY."""

def aggregates_for(conn, *, scope: str, section: str, metric: str) -> list[Point]:
    """Time-series of (as_of_date, mean, median, n) for a given scope/section/metric."""
```

Called after every refresh and after every queried-stack update (since queried filings don't affect S&P aggregates, the second call is a no-op except on cross-ref S&P tickers).

### 5.5 `sibyl/chart.py` — PNG output

matplotlib (Agg backend, no display required). Produces a 6-panel PNG: 2 sections (RF, MDNA) × 3 metrics (d_neg, d_unc, similarity_yoy). Each panel has three lines: queried stock, sector average, S&P average. X-axis = filing date (acceptance_dt). Y-axis = metric value.

Output: `data/queried/<TICKER>/chart_<UTC-stamp>.png` (chart filename never overwritten; tool keeps a history per ticker).

Latest-z-score annotation in the chart title for each panel: e.g., `"AAPL Risk Factors: Δneg = +0.0021 (+1.4σ vs S&P, +0.8σ vs sector)"`.

### 5.6 `sibyl/runner.py` — Orchestrator

Top-level entry. Modes:

```python
def refresh_sp500(*, download: bool) -> None:
    """1. Pull IVV CSV; snapshot.
       2. Resolve members → CIKs.
       3. If download: pull missing S&P 10-K/10-Q within rolling 5y; evict >5y.
       4. parse → sections → score → diff for new filings.
       5. Rebuild aggregates.
    """

def query(ticker: str, *, download: bool = True, chart: bool = True) -> dict:
    """1. queried.get_or_fetch(ticker, download=download)
       2. Look up matching filings in DB
       3. Pull aggregate series for sector + S&P
       4. If chart: render PNG → data/queried/<TICKER>/chart_<stamp>.png
       5. Return dict with paths + summary stats.
    """
```

---

## 6. 10-Q extension to `sections.py` and `diff.py`

### 6.1 Section isolation for 10-Q

`edgartools` exposes a `TenQ` class with the same `risk_factors` / `management_discussion` properties as `TenK`. `sibyl/sections.py` dispatches on the filing's `form_type`:

```python
from edgar.company_reports import TenK, TenQ

def _make_report(filing, form_type):
    if form_type == "10-K":
        return TenK(filing)
    if form_type == "10-Q":
        return TenQ(filing)
    raise ValueError(f"unsupported form_type: {form_type}")
```

10-Q semantics caveats baked into the section status thresholds:
- **10-Q Risk Factors are typically minimal.** Most quarters: "There have been no material changes from Item 1A in our most recent Form 10-K." That short text *is* the section — not a stub. Adjust `MDNA_MIN_REAL_WORDS` (currently MDNA-only) to **not** apply to 10-Q RF. Maybe introduce `RF_10Q_MIN_WORDS = 20` (low — we want to keep the "no changes" placeholder, it's a real signal).
- **10-Q MD&A** is typically smaller than a 10-K's (single quarter vs full year) — keep the 100-word MDNA floor.

### 6.3 Filing-level status taxonomy (v2, introduced 2026-06-21)

After the full 2026-06-21 backfill (9,837 filings) revealed that ~12% were being
classified as `section_fail` under the original binary rule — even when one
section extracted cleanly — the taxonomy split into **three buckets**:

| Status | Meaning | When | Scored? |
|---|---|---|---|
| `ok` | both RF and MD&A extracted cleanly | both per-section statuses are `ok` | yes (full + RF + MDNA) |
| `partial` | one section extracted; other is *legitimately* missing | the other side is `missing` or `incorp_ref` (filer says "see prior 10-K" or "see Annual Report") | yes (full + whichever section is OK) |
| `section_fail` | extraction is broken or both sides unusable | any side is `over_extracted` (boundary detection failed); OR neither side is `ok` | yes (full only — RF/MDNA polluted or empty) |

The single source of truth is `sections._compute_filing_status(rf, mdna, full)`.

**Downstream gating:** `score.py` and `diff.py` filter `parse_status IN ('ok', 'partial', 'section_fail')` so that the *full text* gets scored regardless. Per-section gating is delegated to `score._section_eligible`, which only emits rows for sections with `status='ok'`.

Result on the 2026-06-21 backfill: 8,630 `ok` (87.7%), 895 `partial` (9.1%), 312 `section_fail` (3.2%) → **96.8% of filings produce usable scores**.

The 312 remaining `section_fail` are mostly **bank/financial 10-Q MD&A `over_extracted`** — boundary detection in `edgartools` fails on banks because their MD&A has huge financial tables and the next-section terminator (Item 3) is hidden inside a table. A robust fix would require either custom terminator regex or table-aware section extraction; see §12.

### 6.2 Same-quarter prior matching in `diff.py`

For 10-K-to-10-K, the existing `(cik, form_type, period_of_report)` ordering works because annual reports come once a year. For 10-Q, we want **same fiscal quarter** prior year:
- Q1 2024 (period ~2024-03-31) ↔ Q1 2023 (period ~2023-03-31)
- Not Q1 2024 ↔ Q4 2023.

Refine `match_prior_filings`:

```python
def _quarter_key(period_of_report: str) -> int:
    """Returns 0..3 for the calendar quarter of the period_of_report (best-effort)."""

# Match: for each filing, prior = most recent earlier filing of same (cik, form_type, quarter_key).
```

For 10-K filers with non-Dec fiscal year-ends (Apple: Sept; Microsoft: June; etc.), there's only one 10-K per year, so the quarter logic is a no-op. The refinement only changes 10-Q matching.

---

## 7. CLI surface

```bash
# S&P refresh (membership + filings)
sibyl sp500 refresh                  # pull IVV + missing filings + re-aggregate
sibyl sp500 refresh --no-download    # update membership only; don't pull filings
sibyl sp500 evict                    # explicit eviction pass (>5y filings)

# Single ticker
sibyl research AAPL                  # full workflow: resolve, fetch missing, score, chart
sibyl research AAPL --no-download    # use whatever's cached only; chart from current data
sibyl research AAPL --no-chart       # return summary JSON only

# Inspection
sibyl status                         # disk + DB counts per stack
sibyl sp500 status                   # S&P specifically: membership, coverage, gaps
sibyl queried status                 # queried tickers cached so far

# Reused from main branch (still useful for debug)
sibyl sections --sample 5 --stack sp500
sibyl score --stats --stack sp500
sibyl diff --stats --stack sp500
```

---

## 8. Refresh cadence (droplet, post-deployment)

Cron on `161.35.122.12`, under the existing Unicorn deployment user:

```
# Weekly: pull new S&P filings + re-aggregate (Sunday 04:00 UTC)
0 4 * * 0  cd /home/sibyl && /home/sibyl/.venv/bin/sibyl sp500 refresh >> data/logs/cron_weekly.log 2>&1

# Monthly: re-check IVV membership (1st of month, 03:00 UTC)
0 3 1 * *  cd /home/sibyl && /home/sibyl/.venv/bin/sibyl sp500 refresh --no-download >> data/logs/cron_monthly.log 2>&1
```

Weekly job typically pulls 10-50 new filings (S&P filings cluster after quarter-ends Feb / May / Aug / Nov). Wall clock: <15 min. Monthly job is just a membership pull + aggregate rebuild: <2 min.

---

## 9. Deployment plan

1. **Build + test locally** against a small subset (e.g., 20 S&P names, 2y history). Validate end-to-end.
2. **Smoke test full S&P refresh locally** with `--no-download` and a manually-staged IVV CSV to confirm the membership pull + aggregation path.
3. **Move to droplet**: clone `research-tool` branch, set up venv, copy `config.yaml` + `.env`, run initial backfill in `tmux`:
   ```
   tmux new -s sibyl-backfill
   sibyl sp500 refresh
   # detach with Ctrl-b d; reattach with `tmux a -t sibyl-backfill`
   ```
4. **Install cron** (see §8) once initial backfill completes.
5. **Smoke-test a research query end-to-end** on the droplet (`sibyl research AAPL`).
6. **Document the deployment** in `docs/DEPLOYMENT.md` (created during step 3).

Initial backfill estimate: S&P ~500 names × ~25 filings each (5y of 10-Ks + 10-Qs) = ~12,500 filings × ~250 KB ≈ **3-4 GB raw**, ~4-6h wall clock at SEC's 10 req/s (we use 8). Clean side another 1-2 GB. Plus DB and logs. Should fit comfortably in 50 GB.

---

## 10. Implementation order (suggested branches of work)

A linear-ish build path. Each item is a commit's worth of work.

| Order | Item | Notes |
|---|---|---|
| 1 | Branch `research-tool` exists | Done |
| 2 | Schema: add `stack` column + new tables | Idempotent ALTER pattern; existing `_ensure_column` helper |
| 3 | `Paths` dataclass: split sp500 / queried | Touches `config.py`, all path consumers |
| 4 | `sibyl/tickers.py` | Simple wrapper; tests with mocked SEC file |
| 5 | `sibyl/sp500.py` | HTTP + parse IVV; snapshot; upsert membership |
| 6 | Extend `download.py` for `--stack` | Stack-aware target paths |
| 7 | Extend `sections.py` for 10-Q | TenQ dispatch; 10-Q RF threshold |
| 8 | Extend `diff.py` for same-quarter prior | `_quarter_key` helper; tests |
| 9 | `sibyl/queried.py` | Cross-ref + cache + per-ticker fetch |
| 10 | `sibyl/aggregate.py` | SQL GROUP BY + table population |
| 11 | `sibyl/chart.py` | matplotlib 6-panel layout |
| 12 | `sibyl/runner.py` | Tie everything together |
| 13 | New CLI subcommands | `sp500`, `research`, `queried` |
| 14 | Migrate `sibyl/cli.py` from old top-level commands | Old `download` / `parse` / `sections` / `score` / `diff` become stack-aware or are removed |
| 15 | Tests for all new modules | Hit 90%+ pass-rate target before droplet move |
| 16 | Local smoke test (20-name subset) | Sanity check whole flow |
| 17 | **Cleanup pass: delete redundant code + docs** | See §10.1 below |
| 18 | Deploy to droplet + initial backfill | tmux session; ~5h |
| 19 | Install cron jobs | Per §8 |
| 20 | Update HANDOVER.md | Document the new orientation |

### 10.1 Cleanup pass — what to delete / archive

Run this *after* the new tool is end-to-end working locally but *before* droplet deployment. The principle: anything that won't be invoked by the research tool and isn't load-bearing reference material gets removed. Git history preserves everything that's deleted.

**Definite deletions** (shelved Phase 2/3 stubs that will never be developed under the new direction):
- `sibyl/signals.py` (Layer-2 panel stub — was for the IC harness)
- `sibyl/prices.py` (Polygon cache stub — research tool doesn't need price data)
- `sibyl/export.py` (Unicorn export stub — no longer the integration path)
- `sibyl/eval/` (entire IC/backtest engine directory; was Phase 2 stub)
- The `prices`, `panel`, `eval`, `export` stub subcommands in `cli.py`

**Probable deletions** (existing scripts that were one-off Stage-3 remediation tools, already run):
- `scripts/prepare_audit.py`, `scripts/aggregate_audit.py`, `scripts/remediate_incorp_ref.py` — produced their value, won't be re-run on the new pipeline. Move to `archive/` if there's any chance of needing them again, otherwise delete.

**Probable deletions** (legacy single-stack CLI commands superseded by stack-aware versions):
- The old top-level `sibyl universe` (was Unicorn-endpoint pull; replaced by `sibyl sp500 refresh`)
- The old top-level `sibyl download` / `sibyl sections` / `sibyl score` / `sibyl diff` if their stack-aware variants fully cover the use cases. Keep the `--stats` and `--sample` debug paths but route through the new commands.

**Docs to evaluate**:
- `docs/SIBYL BUILD SPEC.md` — keep, as historical reference for the EDGAR pipeline modules we reused. Add a banner at the top: "This describes the original signal-engine direction. See `RESEARCH_TOOL_SPEC.md` for the current product."
- `docs/llm_audit_plan.md` — keep; the audit pattern may apply to the new tool's 10-Q extractions too.
- `docs/validation_labelling_guide.md` — keep; deferred Layer-3 work that could still be useful if Stage 3 ever gets revisited.
- `docs/parallel_processing.md` — keep; still describes how Stage 3 works.
- `docs/scaling_10q_with_cloud_vm.md` — keep; directly relevant to the droplet deployment for this tool. Rename it if useful.

**Configuration / data**:
- `data/exports/` directory — delete (was for Phase 3 panel exports; never populated).
- The `filing_scores` / `filing_signals` rows from the small-cap universe in `data/sibyl.db` — keep for now (they're not in the way); decide on droplet whether to migrate or start fresh.

**Commit plan**: do the cleanup as **one commit** with a clear message ("Cleanup: remove Phase 2/3 stubs and pre-pivot scripts"). Easier to revert as a unit if anything turns out to be load-bearing.

---

## 11. Out of scope for v1

- PIT membership analysis (IVV snapshots are saved but unused for analysis)
- Batch queries (single ticker per invocation; list-file support deferred)
- Streamlit / web UI (PNG output only)
- Amendments (`10-K/A`, `10-Q/A`)
- Non-US listings without SEC CIKs
- Forward-return / IC analysis (this is a research tool, not a backtester)
- Export contract to Unicorn Hunt (shelved with the rest of Phase 3)
- Multi-threaded refresh (single-threaded; matches existing Sibyl)

---

## 12. Open follow-ups / known unknowns

| Item | Severity | When to revisit |
|---|---|---|
| **Cron not installed** — data won't refresh on its own | required | Before the dataset goes stale (~1 week from 2026-06-21) |
| **312 `section_fail` filings (mostly bank 10-Q MD&A `over_extracted`)** — boundary detection in edgartools fails when the next-section terminator is hidden inside a financial table. Affects ~30 specific tickers (JPM, AXP, FITB, HBAN, KEY, NTRS, SYF, CFG, STT, AEP, and ~20 others). Full text is still scored; only the MD&A delta signal is lost for these names. | medium | A custom post-process trim (regex for "Item 3" / "Quantitative and Qualitative") could reclaim most. ~half-day project. |
| **Wikipedia membership lag** — constituents page is editor-maintained; S&P index changes show up hours-to-days late | low | If realtime accuracy ever needed (it isn't for a monthly-refresh research tool) |
| **3 S&P members with zero qualifying filings** — probably recent IPOs that postdate the 5y window | low | When considering window-width changes |
| **Foreign / ADR ticker mappings** (BABA, TSM, etc.) — may need a hand-curated overlay | low | When a user reports a failed query |
| **Chart visual design** — first version is functional, may need iteration | low | After first analyst use |
| **iShares CSV URL stability** — replaced by Wikipedia in `sibyl/wiki.py`; iShares is no longer a dependency | resolved | n/a |
| **Backfill disk pressure on droplet** — measured: ~2.4 GB after full backfill; plenty of headroom | resolved | n/a |
