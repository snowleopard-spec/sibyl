# Sibyl — Handover Note

*Last updated: 2026-06-21 (full S&P 500 backfill complete on Mac; data ready for analysis).*

This document is enough context for someone (or a fresh Claude Code
window) to pick up Sibyl exactly where the prior session left off.

---

## Where you are in one paragraph

Sibyl has **pivoted from a small-cap signal engine to an S&P-comparison
research tool.** Work happens on the `research-tool` branch (currently 16
commits ahead of `main`). All Layer-1 modules — download, parse,
sections, score, diff — were re-used as-is and made stack-aware. New
modules added: `wiki.py` (Wikipedia S&P 500 scrape), `sp500.py` (membership
orchestration), `tickers.py` (ticker→CIK), `queried.py` (queried-stack
manager), `aggregate.py` (rolling sector + S&P means), `chart.py` (6-panel
PNG), `runner.py` (orchestrator). Two top-level workflows live:
`sibyl sp500 refresh` and `sibyl research TICKER`. **Full backfill done
2026-06-21**: 503 S&P 500 names × 5 years of 10-K + 10-Q = 9,837 filings
parsed, scored, diffed, and aggregated on the Mac. Download was offloaded
to the DigitalOcean droplet (29 min); the CPU-bound stages ran locally
(~3.5h, with sections being the long pole). End-to-end `sibyl research
AAPL` verified.

The canonical spec for the current product is **`docs/RESEARCH_TOOL_SPEC.md`**.
The original signal-engine spec (`SIBYL BUILD SPEC.md`) is kept as historical
reference; it still accurately describes the Layer-1 EDGAR pipeline modules
the research tool reuses.

---

## Backfill summary (2026-06-21)

| Stage | Where | Wall clock | Notes |
|---|---|---|---|
| Download | Droplet (tmux: `sibyl-backfill`) | 29 min | 0 failures; rate-limited at 8 req/s |
| rsync to Mac | Mac | 5 min | 2.41 GB at ~6.6 MB/s |
| Parse | Mac (workers=8) | 16 min | 0 failures; 4 suspicious flags |
| Sections | Mac (workers=7) | **3h 5min** | the long pole — see breakdown below |
| Score | Mac | 2.5 min | 51,780 rows, 0 errors |
| Diff | Mac | 3.5 min | 20,325 rows; 1,855 had no prior to compare |
| Aggregate | Mac | <1 min | 1,194 sector-quarter rolling rows |

**Final dataset:**
- `filings` 9,837 — 2,462 × 10-K + 7,375 × 10-Q across 500 of 503 CIKs (3 had no qualifying filings in window)
- `filing_scores` 51,780 — LM sentiment counts by section
- `filing_signals` 20,325 — diff vs prior filing
- `sp500_membership` 503 across 11 sectors
- `sp500_aggregates` 1,194
- Disk: 2.2 GB raw + 2.8 GB clean

**Section extraction quality:**
- 10-K: 2,321 OK / 141 fail (**94.3% OK**)
- 10-Q: 6,309 OK / 1,066 fail (**85.5% OK**)
- Overall: 8,317 OK / 1,140 fail (**88.0% OK**)

---

## The single action to take when resuming

**Diagnose the section_fail concentration in megabanks/megacap industrials.**
The 1,140 failures aren't random — they're concentrated in ~15 CIKs (banks
+ a few utilities/tech), 10 of which have 100% fail rate. Suggested
30-min investigation:

1. Pick 3 fully-failing tickers: **JPM (CIK 19617), MS (895421), IBM (51143)**.
2. Open one filing per ticker:
   `cat data/sp500/clean/19617/<latest-accession>/sections.json | jq .`
   to see the failure mode (which section, what error).
3. Compare the raw filing text (`full.txt`) against `sibyl/sections.py`'s
   regex anchors. Banks tend to use **"Part II, Item 1A"** ordering in
   10-Qs (not "Item 1A"), which may trip the existing anchors.
4. Decide between:
   - **(a) Add regex variants** in `sections.py` for bank-style ordering. Re-run
     `sibyl sections --stack sp500 --force` (would re-process ~9,000 filings
     in 3h, but only fixes the targeted ones).
   - **(b) Lean on edgartools' `TenQ.Item("1A")`** for the fallback path
     when our regex returns nothing — accept whatever edgartools gives.
   - **(c) Accept the loss.** 88% coverage is fine; the chart/diff code
     handles missing sections gracefully. Add a quality flag to the doc.

After that, install the two cron jobs per `RESEARCH_TOOL_SPEC.md §8`
(weekly filings refresh + monthly membership re-check) — currently
nothing keeps the data fresh.

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

| Item | Severity | Notes |
|---|---|---|
| **No cron installed yet** | required | Data won't stay fresh without it. `RESEARCH_TOOL_SPEC.md §8` defines: weekly `sibyl sp500 refresh` for new filings; monthly membership re-check. Install on the existing droplet alongside `unicornhunt.service`. |
| **1,140 section_fails (12% rate), concentrated in megabanks** | medium | Not random — see "Backfill summary" above. Top offenders (100% fail rate, 20/20 filings): BNY, MS, C, EIX, WFC, IBM, INTC, XOM, JPM, AEP. Likely "Part II, Item 1A" vs "Item 1A" anchor mismatch. See "The single action to take when resuming" for the proposed 30-min triage. |
| **3 CIKs with zero qualifying filings** | low | 500 of 503 S&P members got filings; 3 didn't (probably recent IPOs that postdate the 5y window). Run `SELECT cik FROM sp500_membership WHERE cik NOT IN (SELECT DISTINCT cik FROM filings WHERE stack='sp500');` to identify, then decide whether to widen the window. |
| **Wikipedia lag** | low | Constituents page is editor-maintained; S&P index changes can show up hours-to-days late. For a monthly-refresh research tool this doesn't matter, but flag if you ever need realtime accuracy. |
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

## Quick "is everything still wired up" smoke test

```bash
cd /Users/wessch/Projects/Projects/sibyl
.venv/bin/pytest -q                           # expect 151 passed
.venv/bin/sibyl status                        # expect 9837 filings, 51780 scores, 20325 signals
.venv/bin/sibyl sp500 status                  # expect 503 members
.venv/bin/sibyl sp500 refresh --no-download   # ~5s; refreshes membership, rebuilds aggregates
.venv/bin/sibyl research AAPL --no-download   # writes data/queried/AAPL/chart_<stamp>.png
```

If the membership has changed since the last refresh, Wikipedia will
reflect it. The chart uses whatever filings are in the DB.

---

## What I'd say if I were the prior Claude

> "The backfill landed clean — 9,837 filings, zero download failures,
> zero score errors. The interesting next thing is the 1,140 section
> fails: they're NOT random — 25% are concentrated in just 15 mega-cap
> CIKs (mostly banks). That's a 30-minute investigation that probably
> reclaims most of them by adding a couple of regex variants for
> 'Part II, Item 1A' ordering. Don't go down the LLM-audit path for
> this — the pattern is obvious enough that a manual look at three
> JPM/MS/IBM filings will tell you what's needed.
>
> Second priority: install the cron. The data is current as of 2026-06-21
> and will rot without weekly refresh. Until that's installed, every
> `sibyl research` is reading a frozen-in-time corpus.
>
> Don't add features yet. The current tool works end-to-end and the
> next user-visible improvement is more interesting analysis (heat
> maps, sector overlays) — but that's only worth building once you've
> actually been *using* the tool for a week and know what's missing."
