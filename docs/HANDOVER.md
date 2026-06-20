# Sibyl — Handover Note

*Last updated: 2026-06-20 (research-tool pivot complete; not yet deployed to droplet).*

This document is enough context for someone (or a fresh Claude Code
window) to pick up Sibyl exactly where the prior session left off.

---

## Where you are in one paragraph

Sibyl has **pivoted from a small-cap signal engine to an S&P-comparison
research tool.** Work happens on the `research-tool` branch (currently 13
commits ahead of `main`). All Layer-1 modules — download, parse,
sections, score, diff — were re-used as-is and made stack-aware. New
modules added: `wiki.py` (Wikipedia S&P 500 scrape), `sp500.py` (membership
orchestration), `tickers.py` (ticker→CIK), `queried.py` (queried-stack
manager), `aggregate.py` (rolling sector + S&P means), `chart.py` (6-panel
PNG), `runner.py` (orchestrator). Two top-level workflows live:
`sibyl sp500 refresh` and `sibyl research TICKER`. End-to-end smoke
tested locally on 20 names. **Not yet deployed to droplet** — held per
user direction until you're ready.

The canonical spec for the current product is **`docs/RESEARCH_TOOL_SPEC.md`**.
The original signal-engine spec (`SIBYL BUILD SPEC.md`) is kept as historical
reference; it still accurately describes the Layer-1 EDGAR pipeline modules
the research tool reuses.

---

## The single action to take when resuming

**Deploy to the droplet** (task 37 of the implementation order; held by
user). Sequence:

1. SSH to `161.35.122.12`, clone the repo, check out `research-tool`.
2. Copy `config.example.yaml` → `config.yaml`, set the SEC `user_agent`.
3. `python3 -m venv .venv && .venv/bin/pip install -e .`
4. First real run inside `tmux`:
   ```
   tmux new -s sibyl-backfill
   .venv/bin/sibyl sp500 refresh    # ~4-6h: 503 names × ~25 filings each
   ```
5. Install the two cron jobs per `RESEARCH_TOOL_SPEC.md §8`:
   weekly filings + monthly IVV-style membership re-check.
6. Smoke-test on the droplet: `sibyl research AAPL`. Verify the chart
   PNG lands at `data/queried/AAPL/`.

If unsure whether the droplet has enough disk (50 GB total): backfill
needs ~5 GB raw + ~2 GB clean + DB + logs. Add a volume if `df -h`
shows <15 GB free.

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
│   ├── HANDOVER.md                    # ← this file
│   ├── RESEARCH_TOOL_SPEC.md          # canonical product spec
│   ├── SIBYL BUILD SPEC.md            # historical signal-engine spec (banner at top)
│   ├── parallel_processing.md         # Stage 3 multiprocessing
│   ├── scaling_10q_with_cloud_vm.md   # droplet runbook (still relevant)
│   ├── llm_audit_plan.md              # audit pattern (reusable for 10-Q quality)
│   └── validation_labelling_guide.md  # deferred Layer-3 reference
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
    ├── queried/                       # on-demand stack
    │   ├── raw/<CIK>/<accession>/...  (only for tickers NOT in S&P)
    │   ├── clean/<CIK>/<accession>/...
    │   └── record.jsonl
    │
    └── raw/, clean/                   # ORPHANED — see "Known unknowns" below
```

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

| Item | Severity | Notes |
|---|---|---|
| **Not deployed to droplet yet** | required | Held per user direction. Task 37 of impl order. Steps in "The single action to take when resuming" above. |
| **~20% 10-Q section_fail rate** | medium | Confirmed in the smoke test: of 285 10-Qs, 57 had section extraction fail. 10-Q MD&A is harder than 10-K. Worth running the existing LLM-audit pattern (`scripts/` — deleted in cleanup; reconstruct from git history of `babfbf1`) against a 100-filing 10-Q sample to characterise the failures. |
| **Orphaned `data/raw/` and `data/clean/`** | low | ~7 GB of pre-pivot small-cap filings sitting at the legacy paths (no `sp500/` prefix). Invisible to the new pipeline. Decision needed: delete, archive (`tar -czf`), or migrate the AAPL/etc. that overlap S&P. The new pipeline correctly re-downloads them into `data/sp500/raw/` if not migrated. |
| **Wikipedia lag** | low | Constituents page is editor-maintained; S&P index changes can show up hours-to-days late. For a monthly-refresh research tool this doesn't matter, but flag if you ever need realtime accuracy. |
| **Deprecated `Paths.raw` / `Paths.clean` aliases** | very low | About 30 test sites still reference them. They point at sp500_*; removing them requires test refactoring with no functional benefit. Defer until someone has a reason to touch those tests. |
| **edgartools "legacy parser fallback" warnings** | very low | Chatty WARNING logs during section extraction on older filings (pre-2020-ish). Non-fatal; edgartools still returns the right content. Suppress at logger level if it bothers you. |
| **edgartools "_tcache" warning** | very low | "Failed to clear locale-corrupted cache" — non-fatal; edgartools internal. |

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

## Quick "is everything still wired up" smoke test

```bash
cd /Users/wessch/Projects/Projects/sibyl
.venv/bin/pytest -q                           # expect 151 passed
.venv/bin/sibyl sp500 status                  # expect ~503 members
.venv/bin/sibyl sp500 refresh --no-download   # ~5s; refreshes membership, rebuilds aggregates
.venv/bin/sibyl research AAPL --no-download   # writes data/queried/AAPL/chart_<stamp>.png
```

If anything in the live data has changed since 2026-06-20, the
membership refresh will pick it up. The chart will use whatever
filings happen to be in the DB.

---

## What I'd say if I were the prior Claude

> "Everything works locally. The thing that's actually unproven is the
> droplet deployment — you might hit ops surprises (Python version, disk,
> firewall, cron environment) that the local-only smoke didn't catch.
> Don't waste a session writing more features before deploying; the
> features that aren't on a server aren't shippable. Run the backfill in
> a tmux session and walk away; the SEC rate limiter does the right thing
> on its own. Once it's up, install the cron, run `sibyl research AAPL`
> once to confirm, and then you can decide whether the next session is
> 'fix the 20% 10-Q section_fail rate' or 'add a second ETF holdings
> source for weight data'."
