# Sibyl — Handover Note

*Last updated: 2026-06-21 (full S&P 500 backfill complete + v2 status taxonomy applied; 96.8% scoring coverage).*

This document is enough context for someone (or a fresh Claude Code
window) to pick up Sibyl exactly where the prior session left off.

---

## Where you are in one paragraph

Sibyl has **pivoted from a small-cap signal engine to an S&P-comparison
research tool.** Work happens on the `research-tool` branch (currently 18
commits ahead of `main`). All Layer-1 modules — download, parse,
sections, score, diff — were re-used as-is and made stack-aware. New
modules added: `wiki.py` (Wikipedia S&P 500 scrape), `sp500.py` (membership
orchestration), `tickers.py` (ticker→CIK), `queried.py` (queried-stack
manager), `aggregate.py` (rolling sector + S&P means), `chart.py` (6-panel
PNG), `runner.py` (orchestrator). Two top-level workflows live:
`sibyl sp500 refresh` and `sibyl research TICKER`. **Full backfill done
2026-06-21**: 503 S&P 500 names × 5 years of 10-K + 10-Q = 9,837 filings,
end-to-end pipeline run. **96.8% of filings produce usable scores**
(8,630 ok + 895 partial; 312 section_fail still get full-text scoring).
End-to-end `sibyl research AAPL` verified.

The canonical spec for the current product is **`docs/RESEARCH_TOOL_SPEC.md`**.
The original signal-engine spec (`docs/Aux/SIBYL BUILD SPEC.md`) is kept as
historical reference; it still accurately describes the Layer-1 EDGAR
pipeline modules the research tool reuses.

---

## Backfill summary (2026-06-21)

| Stage | Where | Wall clock | Notes |
|---|---|---|---|
| Download | Droplet (tmux: `sibyl-backfill`) | 29 min | 0 failures; rate-limited at 8 req/s |
| rsync to Mac | Mac | 5 min | 2.41 GB at ~6.6 MB/s |
| Parse | Mac (workers=8) | 16 min | 0 failures; 4 suspicious flags |
| Sections | Mac (workers=7) | **3h 5min** | the long pole — see breakdown below |
| Score + Diff (initial) | Mac | 6 min | 51,780 + 20,325 rows |
| **v2 taxonomy relabel + rescore** | Mac | 3 min | +4,582 score rows, +1,935 signal rows |
| Aggregate | Mac | <1 min | 1,194 sector-quarter rolling rows |

**Final dataset:**
- `filings` 9,837 — 2,462 × 10-K + 7,375 × 10-Q across 500 of 503 CIKs (3 had no qualifying filings in window)
- `filing_scores` 56,362 — LM sentiment counts by section
- `filing_signals` 22,260 — diff vs prior filing
- `sp500_membership` 503 across 11 sectors
- `sp500_aggregates` 1,194
- Disk: 2.2 GB raw + 2.8 GB clean

**Filing-level status (v2 taxonomy, see `RESEARCH_TOOL_SPEC.md §6.3`):**
- `ok` (both sections extracted): **8,630 (87.7%)**
- `partial` (one ok; other is legitimate `missing`/`incorp_ref`): **895 (9.1%)**
- `section_fail` (any `over_extracted` or both unusable): **312 (3.2%)**
- **Scoring coverage: 9,837/9,837 filings have at least full-text scored = 100%; 9,525/9,837 also have ≥1 section scored = 96.8%**

The 312 remaining `section_fail` are concentrated in ~30 bank/financial tickers
(JPM, AXP, FITB, HBAN, KEY, NTRS, SYF, CFG, STT, AEP …). Root cause: their
10-Q MD&A has huge financial tables, and `edgartools`' boundary detection
loses the next-section terminator (Item 3) when it's hidden inside a table —
section then runs forward to end-of-doc. The MD&A text balloons to 100-160%
of the table-stripped full word count, triggering `over_extracted`. The
sections module correctly flags these; downstream still scores the full text.

---

## The path to v1 completion

The tool is functionally working. Three things stand between "works locally"
and "shippable v1":

### 1. Install the cron jobs (**required before next session**, ~30 min)

The data is current as of 2026-06-21 and will rot without scheduled refresh.
Per `RESEARCH_TOOL_SPEC.md §8`:

```cron
# Weekly: pull new S&P filings + re-aggregate (Sunday 04:00 UTC)
0 4 * * 0 cd /root/sibyl && .venv/bin/sibyl sp500 refresh >> data/logs/cron_weekly.log 2>&1

# Monthly: re-check Wikipedia membership (1st of month, 03:00 UTC)
0 3 1 * * cd /root/sibyl && .venv/bin/sibyl sp500 refresh --no-download >> data/logs/cron_monthly.log 2>&1
```

Install on the droplet (`crontab -e` as root). Verify with `crontab -l`.
First weekly run will be incremental — fast (~5-15 min) since most filings
are already cached.

**Note**: the weekly cron runs on the droplet's data, NOT the Mac's. After
each weekly run, rsync the new filings back to the Mac if you want them in
your local DB. Alternative: run the weekly refresh locally on the Mac via
launchd (or just run `sibyl sp500 refresh` manually on Sundays). Decide
based on whether you want the droplet to be the source of truth or the Mac.

### 2. (Optional) Reclaim the bank `over_extracted` cases (~half-day project)

312 filings, mostly bank 10-Q MD&A, are losing their delta-signal because
`edgartools` over-extracts past the section terminator. Approaches in
descending difficulty:

- **Post-process trim** (best): after edgartools returns text, search backwards
  from end for known terminators ("Item 3. Quantitative and Qualitative
  Disclosures", "Item 4. Controls and Procedures") and truncate. Risk: false
  positives on filings that mention these phrases elsewhere; mitigate by only
  trimming when the section is >100K words.
- **Table-aware extraction** (medium): strip `<table>` blocks from the section
  text before measuring; if it's then below the over-extracted threshold,
  promote to `ok`. Won't fix the underlying boundary issue but reclaims the
  measurement.
- **Custom extractor for banks** (worst): hard-code RF/MD&A ranges per CIK.
  Won't generalize.

Recommend the post-process trim. Add as a new `_trim_to_terminator()` in
`sections.py`, gate behind the existing `over_extracted` detection, bump
`EXTRACTOR_VERSION` to `"3"`, re-run sections — `--force` not needed since
version bump triggers reprocessing of the 312 currently-failing filings.

### 3. (Optional) Use the tool for a week before adding features

The temptation will be to build heat maps, sector overlays, multi-ticker
batch queries, etc. Don't. The 6-panel chart is enough to start using the
tool, and a week of actual analysis will tell you what to build next better
than any spec session.

---

## Resume sequence (one command to verify everything still works)

```bash
cd /Users/wessch/Projects/Projects/sibyl
.venv/bin/pytest -q                           # expect 158 passed
.venv/bin/sibyl status                        # expect 9837 filings, 56362 scores
.venv/bin/sibyl research AAPL --no-download   # writes chart_AAPL_<stamp>.png
```

If all three pass, you're picking up exactly where this session left off.

---

## Architecture at a glance

```
                    ┌─────────────────────────────────────┐
   Wikipedia  ─────►│  sibyl/wiki.py                       │
   constituents     │   → parse_constituents               │
   table            └────────────┬────────────────────────┘
                                 │ members
                                 ▼
   ┌─────────────────────────────────────────────────────┐
   │  sibyl/sp500.py                                      │
   │   refresh_membership() → upserts sp500_membership    │
   └─────────────────────────────────────────────────────┘
                                 │ CIKs
                                 ▼
   ┌─────────────────────────────────────────────────────┐
   │  Layer-1 pipeline (existing modules, made stack-aware)│
   │   download → parse → sections → score → diff         │
   │   data/sp500/{raw,clean}/, filings/scores/signals    │
   │   tables tagged stack='sp500'                        │
   └────────────┬────────────────────────────────────────┘
                │
                ▼
   ┌─────────────────────────────────────────────────────┐
   │  sibyl/aggregate.py                                  │
   │   rebuild_aggregates() → sp500_aggregates table      │
   │   (per-date × per-scope × per-section × per-metric)  │
   └────────────┬────────────────────────────────────────┘
                │
                ▼                Single-ticker on demand:
   ┌─────────────────────────────────────────────────────┐
   │  sibyl/queried.py.get_or_fetch(ticker)               │
   │   1. Resolve via sibyl/tickers.py                    │
   │   2. Cross-ref against sp500_membership              │
   │   3. Else: download → parse → sections → score → diff│
   │      in data/queried/ (stack='queried')              │
   └────────────┬────────────────────────────────────────┘
                │
                ▼
   ┌─────────────────────────────────────────────────────┐
   │  sibyl/chart.py                                      │
   │   6-panel PNG: RF + MDNA × d_neg + d_unc + similarity│
   │   queried ticker / sector mean / S&P mean            │
   └─────────────────────────────────────────────────────┘

Orchestration: sibyl/runner.py.refresh_sp500 + query.
CLI: sibyl sp500 refresh, sibyl research TICKER.
```

---

## CLI surface

```
sibyl sp500 refresh [--no-download]   # Wikipedia pull → membership → optionally
                                       # download → parse → sections → score → diff
                                       # → rebuild aggregates
sibyl sp500 status                    # current membership counts + per-sector breakdown
sibyl research TICKER [--no-download] [--no-chart]
                                      # full single-ticker query workflow
sibyl queried status                  # tickers cached in queried stack
sibyl status                          # DB + disk usage across both stacks

# Stack-aware debug commands (operate on either stack via --stack):
sibyl download    --stack {sp500,queried} [--cik N ...]
sibyl parse       --stack {sp500,queried} [--cik N ...] [--stats] [--sample N]
sibyl sections    --stack {sp500,queried} [--cik N ...] [--stats] [--sample N]
sibyl score       --stack {sp500,queried} [--cik N ...] [--stats]
sibyl diff        --stack {sp500,queried} [--cik N ...] [--stats]
```

All take `--config <path>` (default `config.yaml`).

---

## File layout (project root)

```
sibyl/
├── pyproject.toml           # deps incl. matplotlib + openpyxl
├── README.md
├── config.example.yaml      # template (no Unicorn block; form_types: [10-K, 10-Q])
├── config.yaml              # GITIGNORED — local config
├── .env                     # GITIGNORED — secrets (none currently required)
│
├── sibyl/                   # the package
│   ├── cli.py               # argparse entry; 9 subcommands
│   ├── config.py            # Paths (sp500_*/queried_*); stack_raw/stack_clean/stack_record
│   ├── db.py                # SQLite schema + idempotent migrations
│   ├── edgar.py             # SEC HTTP: RateLimiter (8/s), retries, URL builders
│   │
│   ├── runner.py            # refresh_sp500() + query() orchestrators
│   │
│   ├── wiki.py              # NEW: Wikipedia S&P 500 scrape
│   ├── sp500.py             # S&P 500 membership orchestrator (calls wiki.py)
│   ├── tickers.py           # ticker → CIK via SEC's company_tickers.json
│   ├── queried.py           # queried-stack manager (cross-ref + on-demand fetch)
│   ├── aggregate.py         # rolling S&P + per-sector means → sp500_aggregates
│   ├── chart.py             # matplotlib 6-panel PNG
│   │
│   ├── download.py          # Stage 1 (stack-aware)
│   ├── parse.py             # Stage 2 (stack-aware; re-parses when full.txt missing)
│   ├── sections.py          # Stage 3 (TenK + TenQ dispatch)
│   ├── score.py             # Stage 4 (L&M counts; accepts df_override)
│   ├── diff.py              # Stage 5 (10-Q same-quarter prior matching)
│   └── lm_dictionary.py     # L&M master dictionary loader
│
├── scripts/
│   └── smoke_sp500.py       # end-to-end smoke on a 20-name subset
│
├── docs/
│   ├── HANDOVER.md                    # ← this file (active)
│   ├── RESEARCH_TOOL_SPEC.md          # canonical product spec (active)
│   └── Aux/                           # historical / less-frequently-needed
│       ├── SIBYL BUILD SPEC.md        # original signal-engine spec
│       ├── DROPLET_BACKFILL.md        # one-time backfill runbook (done 2026-06-21)
│       ├── parallel_processing.md     # Stage 3 multiprocessing notes
│       ├── scaling_10q_with_cloud_vm.md  # droplet sizing reference
│       ├── llm_audit_plan.md          # audit pattern (reusable for 10-Q quality)
│       └── validation_labelling_guide.md  # deferred Layer-3 reference
│
├── tests/                   # 151 passing
│
└── data/                    # GITIGNORED
    ├── sibyl.db                       # SQLite index (filings tagged by stack)
    ├── company_tickers.json           # SEC ticker → CIK cache
    ├── lm_master_dictionary.csv       # 86,553 L&M words
    ├── logs/
    │
    ├── sp500/                         # S&P 500 stack
    │   ├── raw/<CIK>/<accession>/primary.html.gz + metadata.json
    │   ├── clean/<CIK>/<accession>/{full,risk_factors,mdna}.txt + sections.json
    │   ├── record.jsonl
    │   └── membership_snapshots/wiki_sp500_<YYYY-MM-DD>.html
    │
    └── queried/                       # on-demand stack
        ├── raw/<CIK>/<accession>/...  (only for tickers NOT in S&P)
        ├── clean/<CIK>/<accession>/...
        └── record.jsonl
```

*(Pre-pivot dirs `data/raw/`, `data/clean/`, `data/audits/`, `data/exports/`,
`data/prices/`, `data/universe.json`, `data/universe_snapshots/` were
removed during 2026-06-21 cleanup. ~7 GB freed. If anything in
`audits/` was wanted, it's reachable via `git show` on the audit-related
commits.)*

---

## Key conventions to preserve

1. **Stack is the new isolation boundary.** Anything new that reads or
   writes filing data takes a `stack` arg (`'sp500'` or `'queried'`).
   Use `stack_raw(cfg, stack)` / `stack_clean(cfg, stack)` to resolve
   paths; the DB filters `WHERE stack = ?` on `filings`.
2. **Wikipedia is the membership source.** S&P 500 constituents +
   sectors + CIKs come from one page (see `wiki.py`). Snapshots are
   saved per pull but **analysis only uses the most recent**; PIT
   membership is out of v1 scope.
3. **Cross-stack dedup.** When a queried ticker is also in S&P, the
   queried record doesn't duplicate the S&P-stack files — the S&P-stack
   data is used directly. Implemented in `queried.get_or_fetch`.
4. **tfidf uses the S&P DF.** Queried-stack scoring passes
   `df_override=(sp500_df, sp500_n)` so queried tfidf scores are
   comparable to the S&P benchmark series.
5. **Versioning still applies.** `parser_version`, `section_extractor_version`,
   `scorer_version`, `diff_version` are all live. Bump in the relevant
   module + the existing remediation pattern (or `--force`) re-triggers.
6. **`raw/` is immutable; `clean/` is regenerable.** Within each stack
   subtree. Wiping `data/sp500/clean/` and re-running stages rebuilds
   from raw without re-hitting SEC.
7. **Same-quarter prior matching for 10-Q.** Q3 2024 pairs with Q3 2023,
   not Q2 2024. `_quarter_key` in `diff.py`.

---

## Test suite

- **151 passing** across all modules. `.venv/bin/pytest -q` from project root.
- Coverage spans: db migrations, config Paths + helpers, ticker resolver,
  wiki scrape, sp500 membership upsert, download (stack-aware), parse
  (re-trigger on missing full.txt), sections (10-Q dispatch), score
  (df_override), diff (same-quarter), queried (cross-ref + fetch),
  aggregate, chart rendering, runner orchestration.
- One harmless matplotlib UserWarning ("no artists with labels") on
  the empty-corpus chart test.

---

## Known unknowns (must address eventually)

See "The path to v1 completion" above for the items with clear remediation
paths (cron install, over_extracted reclaim). The rest are background risks:

| Item | Severity | Notes |
|---|---|---|
| **Wikipedia lag** | low | Constituents page is editor-maintained; S&P index changes can show up hours-to-days late. For a monthly-refresh research tool this doesn't matter, but flag if you ever need realtime accuracy. |
| **3 CIKs with zero qualifying filings** | low | 500 of 503 S&P members got filings; 3 didn't (probably recent IPOs that postdate the 5y window). Run `SELECT cik FROM sp500_membership WHERE cik NOT IN (SELECT DISTINCT cik FROM filings WHERE stack='sp500');` to identify, then decide whether to widen the window. |
| **Deprecated `Paths.raw` / `Paths.clean` aliases** | very low | About 30 test sites still reference them. They point at sp500_*; removing them requires test refactoring with no functional benefit. Defer until someone has a reason to touch those tests. |
| **edgartools "legacy parser fallback" warnings** | very low | Chatty WARNING logs during section extraction on older filings (pre-2020-ish). Non-fatal; edgartools still returns the right content. Suppress at logger level if it bothers you. |
| **edgartools "_tcache" warning** | very low | "Failed to clear locale-corrupted cache" — non-fatal; edgartools internal. |
| **DB backup `sibyl.db.pre-rsync-backup-20260621_175325`** | very low | 17 MB snapshot of the Mac DB taken before the droplet rsync overwrite. Safe to delete once the current DB has been used a while. |

---

## How the smoke test was structured

`scripts/smoke_sp500.py` injects 20 hardcoded top-of-S&P names (because
the BlackRock IVV CSV endpoint is broken — see `wiki.py` for context),
runs the full pipeline, and renders an AAPL chart. Takes ~7 min wall
clock on a Mac.

To re-run from scratch:
```
.venv/bin/python scripts/smoke_sp500.py
```

To do a *real* end-to-end run (uses live Wikipedia membership instead
of the hardcoded 20):
```
.venv/bin/sibyl sp500 refresh
```
This is what the droplet's weekly cron will do. ~4-6h on first run;
~5-15 min for incremental updates after.

---

## What I'd say if I were the prior Claude

> "The 30-min diagnosis of the 1,140 'section_fails' turned out to be the
> whole story: 895 of them were never real failures — they were legitimate
> 10-Q filings where the filer said 'no material RF changes since the 10-K'
> (perfectly valid filing pattern, just not extractable as a section).
> Splitting the binary ok/fail status into ok/partial/section_fail
> reclassified those correctly and let the score/diff stages pick them up,
> taking scoring coverage from 88% to 96.8%.
>
> What's left is genuinely hard: 312 bank/financial filings where
> edgartools' boundary detection runs past the section terminator
> because it's hidden inside a financial table. The 'fix' is a
> post-process trim, not a regex tweak — half a day of careful work to
> avoid false positives. NOT urgent: full-text scoring still works on
> these, only the MD&A delta-signal is lost. Reasonable to defer until
> you actually look at bank charts and notice missing data points.
>
> The thing that IS urgent: install the cron jobs. Until they're in,
> every `sibyl research` query is reading data frozen at 2026-06-21.
> ~30 min on the droplet (see `Path to v1 completion` step 1).
>
> Don't add features yet. Use the tool for a week first."
