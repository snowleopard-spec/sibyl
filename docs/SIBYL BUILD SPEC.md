# Sibyl — SEC Filing Signal Research Engine

> **Historical spec.** This document describes the original signal-engine direction
> (Phase 1 EDGAR pipeline → Phase 2 IC / backtest harness → Phase 3 Unicorn export).
> Sibyl pivoted in 2026-06 to a research/comparison tool; see
> [`RESEARCH_TOOL_SPEC.md`](RESEARCH_TOOL_SPEC.md) for the current product spec.
> This file is kept for reference on the Layer-1 EDGAR pipeline modules
> (download / parse / sections / score / diff / lm_dictionary) — those still
> exist and are reused as the research tool's substrate.

**Build specification for Claude Code.** This document is the source of truth for the project’s
architecture and the intended build order. It is written to be detailed enough to implement from
directly. Read the whole thing before writing code; the load-bearing decisions are in
*Architecture Principles*, *Internal Architecture*, and *Data Architecture*.

-----

## 1. What Sibyl is

Sibyl is a **batch signal research engine**: it acquires SEC filings, scores them into quant
signals, and validates whether those signals predict returns. Its end goal is to feed *validated*
signals into the existing **Unicorn Hunt** screener — Sibyl is where signals are **proven**, Unicorn
is where proven signals are **used**.

The boundary matters: Sibyl is offline, compute-heavy at build time, and its user is *you,
iterating*. Unicorn Hunt is a live-ish FastAPI/React app whose job is to surface and serve. They
have opposite lifecycles, so they stay separate systems with a narrow contract between them
(§ Export Contract). Sibyl emits standardized point-in-time signal panels; Unicorn consumes them as
factors. Neither needs to know the other’s internals.

**Initial signal families (Sibyl’s first tenants):**

1. **Loughran-McDonald (L&M) sentiment** — finance-specific dictionary counts (Negative,
   Uncertainty, Litigious, Strong/Weak Modal, Constraining, Positive) over filing text. The
   finance dictionary is used deliberately instead of generic NLP sentiment, which mislabels
   ordinary business vocabulary (“liability”, “tax”, “cost”) as negative.
1. **Lazy Prices similarity** — year-over-year textual similarity (cosine on tf-idf vectors) of a
   firm’s filings. Large changes in filing language — especially in Risk Factors and MD&A —
   predict underperformance.

The high-value derived signal is **change**, not level: ΔUncertainty / ΔLitigious year-over-year,
paired with the similarity magnitude. Level sentiment is weak; the delta is strong.

The compute is intentionally cheap — dictionary lookups and sparse vector math, no LLM/GPU
inference. The whole historical corpus scores in minutes of CPU once acquired. The real cost and
the real difficulty is **acquisition and parsing**, not the NLP.

**Crucially, the evaluation engine is signal-agnostic.** L&M and Lazy Prices are just the first
signals through it; accruals, PEAD/SUE, net issuance, and anything else later run through the *same*
honest IC/backtest pipes. Designing the engine decoupled from the specific signals from day one is
what turns Sibyl from a one-signal scorer into a general-purpose signal research engine for almost
no extra cost.

### Phased roadmap

- **Phase 1 — EDGAR pipeline (most of this spec):** acquire → parse → score, fully local. Output:
  raw L&M counts + Lazy Prices similarity per filing.
- **Phase 2 — Signal + evaluation layers:** standardize/sector-neutralize raw scores into panels;
  cache Polygon prices; build the reusable IC / IC-decay / turnover / quantile / backtest harness;
  validate that the signals actually carry IC in the small-cap universe.
- **Phase 3 — Integration:** define and version the export contract; Sibyl emits validated signal
  panels; Unicorn Hunt ingests them as factors in the screener. **Only signals that survive Phase 2
  reach Phase 3** — Sibyl is partly the filter deciding what’s worth surfacing at all.

-----

## 2. Architecture principles (load-bearing — do not violate)

1. **Point-in-time discipline is sacred.** Every filing is stamped with its EDGAR
   `acceptanceDateTime` — the moment the information became public. All downstream joins use this,
   **never** `period_of_report`. Getting this right once at acquisition is what keeps the eventual
   backtest free of lookahead.
1. **Raw bytes on the filesystem, metadata + derived numbers in SQLite, joined by accession
   number.** Never store filing text in the database. Never encode metadata in folder names beyond
   the CIK/accession keys.
1. **`raw/` is immutable; `clean/` is regenerable.** The downloader writes `raw/` once and never
   touches it again. The parser writes `clean/`, which can be deleted and rebuilt wholesale
   whenever parsing logic improves (`rm -rf data/clean && sibyl parse`) with zero risk to the
   expensive-to-reacquire raw data.
1. **Everything is resumable and idempotent.** A killed-and-restarted job picks up where it left
   off by checking what already exists. The database *is* the log of what’s done.
1. **Local-first, single machine.** Develop and run everything on the Mac, writing to the internal
   disk. No hardcoded paths regardless — all paths come from config — so the project *can* move, but
   there is no two-machine reconciliation in normal operation. (A droplet is optional, later, only
   for an unattended incremental updater; see § Deployment.)
1. **The evaluation engine is signal-agnostic.** It takes *any* standardized signal panel + a price
   series and returns IC/decay/turnover/backtest. The L&M and Lazy Prices signals are tenants, not
   the engine’s hardcoded subject. This is what lets future signals (accruals, PEAD) reuse it.
1. **Narrow export contract to Unicorn.** Sibyl’s only coupling to Unicorn Hunt is a versioned
   signal-panel output (date × CIK × signal × score, acceptance lineage preserved). Everything
   EDGAR/NLP-specific stays inside Sibyl.

-----

## 3. Internal architecture (three layers)

Sibyl is one codebase with three internal layers. Signals flow *down*; the engine judges them;
validated panels are the *export*. Keep the layers decoupled — the value of the engine is that it
doesn’t know or care which signal it’s evaluating.

```
   ┌─────────────────────────────────────────────────────────────┐
   │  LAYER 1 — EDGAR PIPELINE  (Phase 1; most of this spec)       │
   │  universe → download → parse → section-isolate → score        │
   │  output: raw L&M counts + Lazy Prices similarity per filing   │
   └─────────────────────────────────────────────────────────────┘
                              │ raw scores
                              ▼
   ┌─────────────────────────────────────────────────────────────┐
   │  LAYER 2 — SIGNAL LAYER  (Phase 2)                            │
   │  standardize (z/rank within date) → sector-neutralize          │
   │  → assemble panel: date × CIK × signal_name × zscore          │
   │  (later: other signal families land here too)                 │
   └─────────────────────────────────────────────────────────────┘
                              │ standardized panels
                              ▼
   ┌─────────────────────────────────────────────────────────────┐
   │  LAYER 3 — EVALUATION ENGINE  (Phase 2; SIGNAL-AGNOSTIC)      │
   │  input: any signal panel + cached Polygon prices              │
   │  → IC, IC-decay, quantile spreads, turnover, cost-aware bt     │
   │  this is the reusable crown jewel; build it generic           │
   └─────────────────────────────────────────────────────────────┘
                              │ signals that survive
                              ▼
   ┌─────────────────────────────────────────────────────────────┐
   │  EXPORT CONTRACT  (Phase 3)  →  Unicorn Hunt ingests           │
   └─────────────────────────────────────────────────────────────┘
```

Packages map onto layers: `download/parse/sections/score` = Layer 1; `signals/` (standardize,
neutralize, panel assembly) = Layer 2; `eval/` (IC, backtest, costs) = Layer 3; `export/` = the
contract. Layer 3 imports nothing from the L&M code — it operates on generic panels only.

-----

## 4. Project layout

```
sibyl/
  pyproject.toml            # or requirements.txt + setup; pin deps
  README.md
  SIBYL_BUILD_SPEC.md       # this file
  .gitignore                # MUST exclude data/ and *.db and .env
  config.example.yaml       # committed template
  config.yaml               # gitignored; real config incl. paths + SEC user-agent
  .env                      # gitignored; secrets: SIBYL_UNICORN_TOKEN, POLYGON_API_KEY

  sibyl/                    # the package
    __init__.py
    cli.py                  # entry point: argparse/click subcommands
    config.py               # loads config.yaml + .env, resolves paths
    db.py                   # SQLite connection, schema creation, helpers
    edgar.py                # EDGAR endpoint helpers + rate limiter + User-Agent

    # --- Layer 1: EDGAR pipeline ---
    universe.py             # Stage 0: load universe, ticker -> CIK mapping
    download.py             # Stage 1: resumable, rate-limited, gzipped downloader
    parse.py                # Stage 2: HTML/XBRL -> clean text
    sections.py             # Stage 3: isolate Item 7 (MD&A) and Item 1A (Risk Factors)
    score.py                # Stage 4: tokenize + L&M counts
    diff.py                 # Stage 5: yoy similarity + sentiment deltas
    lm_dictionary.py        # loads + caches the L&M master dictionary

    # --- Layer 2: signal layer (Phase 2) ---
    signals.py              # standardize (z/rank within date), sector-neutralize, assemble panels

    # --- Price data (Phase 2) ---
    prices.py               # pull + cache Polygon prices for the universe (own cache, gzip+SQLite)

    # --- Layer 3: evaluation engine (Phase 2, SIGNAL-AGNOSTIC) ---
    eval/
      __init__.py
      ic.py                 # information coefficient + IC decay across horizons
      quantiles.py          # decile/quantile spreads, monotonicity
      turnover.py           # period-to-period signal churn
      backtest.py           # cost-aware long-short / long-tilt simulation
      costs.py              # spread/slippage/impact model scaled to small-cap illiquidity

    # --- Export (Phase 3) ---
    export.py               # emit versioned signal panels for Unicorn Hunt ingestion

  data/                     # gitignored entirely
    raw/<CIK>/<accession>/primary.html.gz + metadata.json
    clean/<CIK>/<accession>/full.txt, mdna.txt, risk_factors.txt, sections.json
    prices/                 # cached Polygon price history (gzip), keyed by ticker/CIK
    sibyl.db                # SQLite index (filings, scores, signals, prices meta)
    lm_master_dictionary.csv
    company_tickers.json    # cached SEC ticker->CIK map
    universe.json           # the input universe (latest snapshot from Unicorn Hunt)
    universe_snapshots/     # timestamped universe snapshots (survivorship-bias defense)
    exports/                # versioned signal panels for Unicorn (Phase 3)
    logs/                   # run logs

  tests/
```

`data/` must be in `.gitignore`. Never commit filings or the DB to git.

-----

## 5. Data architecture

### 5.1 Filesystem

Sharded by CIK so no single directory holds tens of thousands of entries. One folder per
**accession number** (one filing = one atom). A 10-K and its three 10-Qs are four separate folders,
*not* one — fiscal-year grouping is a query concern handled in SQL, never in the folder tree.

```
data/raw/<CIK>/<accession>/
    primary.html.gz     # gzipped raw primary document (gzip ~85-90% on filing HTML)
    metadata.json       # self-describing local copy of the filing's metadata

data/clean/<CIK>/<accession>/
    full.txt            # cleaned running text, whole document
    mdna.txt            # isolated Item 7 (empty/absent if isolation failed)
    risk_factors.txt    # isolated Item 1A (empty/absent if isolation failed)
    sections.json       # which sections were found, char offsets, status
```

Use the CIK and accession exactly as the keys. Accession format from EDGAR is
`0000320193-23-000106`; keep the dashed form as the folder name and DB key, strip dashes only when
building the document URL.

**Compression — per-file gzip, via the standard library.** Each filing is gzipped individually
(not one bulk archive), which preserves random access — the parser opens one filing, decompresses,
moves on, without touching a giant blob. No external tool; `gzip.open` is a drop-in for `open`:

```python
import gzip
# write (in the downloader)
with gzip.open(path / "primary.html.gz", "wt", encoding="utf-8") as f:
    f.write(raw_html)
# read (in the parser)
with gzip.open(path / "primary.html.gz", "rt", encoding="utf-8") as f:
    raw_html = f.read()
```

Use the default compression level (6); higher levels cost disproportionate CPU for marginal gain.
**Costs of compression:** lossless, so no correctness/integrity cost. CPU at write time is hidden
behind the rate-limited network wait; at read time decompression adds single-digit ms per file,
negligible against the HTML parse that follows. The only real friction is that `.gz` files aren’t
directly greppable/browsable — use `zcat file.gz | less`, `zgrep`, or just read in Python when
debugging a specific filing by hand. Net: ~85-90% disk reduction, which is what makes the corpus
laptop-sized and any backup/move fast. Compress without hesitation.

### 5.2 SQLite schema

SQLite, not Postgres: single file, no server, travels with the project folder, vastly over-provisioned for
~50k rows. Full `CREATE TABLE` statements:

```sql
-- Point-in-time universe membership (survivorship-bias defense). Upserted each `sibyl universe`.
CREATE TABLE IF NOT EXISTS universe_membership (
    cik          INTEGER,                   -- NULL until ticker resolves to a CIK
    ticker       TEXT NOT NULL,
    as_of_date   TEXT NOT NULL,             -- the snapshot date (from response generated_at / run date)
    sector       TEXT,                      -- as reported by Unicorn; feeds sector-neutralization
    market_cap   REAL,                      -- optional, for diagnostics
    name         TEXT,
    exchange     TEXT,
    in_universe  INTEGER NOT NULL DEFAULT 1, -- 1 = member on as_of_date
    PRIMARY KEY (ticker, as_of_date)
);
CREATE INDEX IF NOT EXISTS idx_membership_cik  ON universe_membership(cik);
CREATE INDEX IF NOT EXISTS idx_membership_date ON universe_membership(as_of_date);

-- The point-in-time spine. One row per filing.
CREATE TABLE IF NOT EXISTS filings (
    accession         TEXT PRIMARY KEY,      -- dashed form, e.g. 0000320193-23-000106
    cik               INTEGER NOT NULL,
    ticker            TEXT,                   -- as-of at download; tickers drift, CIK is canonical
    form_type         TEXT NOT NULL,          -- '10-K' / '10-Q' (decide on amendments, see §13)
    period_of_report  TEXT,                   -- ISO date, fiscal period covered (NOT for PIT joins)
    acceptance_dt     TEXT NOT NULL,          -- ISO datetime; THE point-in-time field
    filing_date       TEXT,                   -- ISO date
    primary_doc       TEXT,                   -- filename of the primary document
    raw_path          TEXT NOT NULL,          -- relative path into data/raw/
    parse_status      TEXT,                   -- NULL until parsed; 'ok'/'section_fail'/'parse_fail'
    downloaded_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_filings_cik   ON filings(cik);
CREATE INDEX IF NOT EXISTS idx_filings_form  ON filings(form_type);
CREATE INDEX IF NOT EXISTS idx_filings_accept ON filings(acceptance_dt);

-- One row per filing x section x weighting scheme.
CREATE TABLE IF NOT EXISTS filing_scores (
    accession     TEXT NOT NULL,
    section       TEXT NOT NULL,             -- 'full' / 'mdna' / 'risk_factors'
    weighting     TEXT NOT NULL,             -- 'proportional' / 'tfidf'
    total_words   INTEGER,
    neg           REAL,                       -- counts or proportions per L&M category
    pos           REAL,
    unc           REAL,                       -- uncertainty
    lit           REAL,                       -- litigious
    strong_modal  REAL,
    weak_modal    REAL,
    constraining  REAL,
    scored_at     TEXT,
    PRIMARY KEY (accession, section, weighting),
    FOREIGN KEY (accession) REFERENCES filings(accession)
);

-- Per-firm, per-period derived signals (recomputed at rebalance). Schema may evolve.
CREATE TABLE IF NOT EXISTS filing_signals (
    cik             INTEGER NOT NULL,
    accession       TEXT NOT NULL,            -- the current filing in the pair
    prior_accession TEXT,                     -- the same-period prior-year filing
    section         TEXT NOT NULL,
    similarity_yoy  REAL,                     -- cosine similarity vs prior-year same-period filing
    d_unc           REAL,                     -- delta uncertainty proportion
    d_lit           REAL,                     -- delta litigious proportion
    d_neg           REAL,
    computed_at     TEXT,
    PRIMARY KEY (accession, section),
    FOREIGN KEY (accession) REFERENCES filings(accession)
);
```

`filings` and `filing_scores` are append-only / slow-changing. `filing_signals` is recomputed.
`universe_membership` is upserted (accumulates over time — never overwritten — to preserve
point-in-time membership). `parse_status` lets section-isolation failures be treated as data
(queryable, with explicit whole-document fallback) rather than silently dropped firms.

### 5.3 Write ordering (crash safety)

Always **write the file to disk first, then insert the DB row.** A crash then leaves at worst an
orphaned file (harmless, ignored or overwritten on retry) rather than a DB row pointing at a file
that doesn’t exist.

-----

## 6. Configuration

`config.yaml` (gitignored; commit `config.example.yaml` as a template). Everything path- or
environment-specific lives here so the project is location-agnostic:

```yaml
paths:
  data_root: ./data          # overridden per-machine; never hardcoded in code
sec:
  user_agent: "Sibyl research your.name@email.com"   # REQUIRED by SEC, see §7
  rate_limit_per_sec: 8      # stay safely under SEC's 10/s
unicorn:
  base_url: "https://<unicorn-host>"   # Unicorn Hunt host serving /api/universe
  universe_path: "/api/universe"
  expected_contract_version: "1.0"     # warn on mismatch
universe:
  file: ./data/universe.json           # working copy = latest snapshot
  snapshots_dir: ./data/universe_snapshots
  form_types: ["10-K", "10-Q"]
  include_amendments: false  # see §13
  history_start: "2016-01-01"
download:
  gzip: true
```

Secrets go in `.env` (gitignored), loaded via `python-dotenv`:

```
SIBYL_UNICORN_TOKEN=...     # bearer token for the Unicorn /api/universe endpoint
POLYGON_API_KEY=...         # for the Stage 7 price cache
```

-----

## 7. EDGAR access rules (get these right or you get blocked)

- **User-Agent header is mandatory** on every request, in the form
  `"Sample Name contact@email.com"`. SEC blocks requests without a proper UA.
- **Rate limit: 10 requests/second per IP.** Implement a token-bucket / simple sleep limiter; run
  at ~8/s for margin. This is the binding constraint on bulk download wall-clock time.
- Endpoints:
  - Ticker → CIK map: `https://www.sec.gov/files/company_tickers.json` (fetch once, cache).
  - Submissions per company: `https://data.sec.gov/submissions/CIK##########.json` (CIK
    **zero-padded to 10 digits**). Contains `filings.recent` with parallel arrays
    (`accessionNumber`, `form`, `filingDate`, `reportDate`, `acceptanceDateTime`,
    `primaryDocument`, …). For firms with long histories, additional older filings are referenced
    under `filings.files[]` (each a separate JSON to fetch).
  - Primary document:
    `https://www.sec.gov/Archives/edgar/data/<CIK>/<accession-no-dashes>/<primaryDocument>`.

-----

## 8. Pipeline stages & CLI

CLI subcommands map onto stages. Each stage reads from the cache the previous one wrote and is
independently re-runnable.

```
# Layer 1 — EDGAR pipeline (Phase 1)
sibyl universe   # Stage 0: build/refresh universe.json + ticker->CIK, cache company_tickers.json
sibyl download   # Stage 1: resumable, rate-limited, gzipped acquisition + filings table
sibyl parse      # Stage 2+3: raw -> clean text + section isolation; sets parse_status
sibyl score      # Stage 4: tokenize + L&M counts -> filing_scores
sibyl diff       # Stage 5: yoy similarity + sentiment deltas -> filing_signals
sibyl status     # convenience: counts by stage, parse failures, disk usage

# Layer 2 + price cache + Layer 3 (Phase 2)
sibyl prices     # pull + cache Polygon price history for the universe
sibyl panel      # standardize + sector-neutralize raw scores -> signal panel
sibyl eval       # run IC / decay / quantiles / turnover / cost-aware backtest on a panel

# Export (Phase 3)
sibyl export     # emit a versioned validated signal panel for Unicorn Hunt
```

### Stage 0 — Universe acquisition (from Unicorn Hunt endpoint)

Sibyl fetches the universe from a read-only authenticated **Unicorn Hunt endpoint**,
`GET /api/universe` (built in a separate session; see `UNICORN_UNIVERSE_ENDPOINT_SPEC.md`). This is
the narrow contract — Sibyl never touches Unicorn’s DB or internals, just makes one HTTPS GET.

`sibyl universe` does:

1. **GET** `https://<unicorn-host>/api/universe` with `Authorization: Bearer <token>` (host + token
   from Sibyl’s `.env`).
1. **Validate** the response `contract_version` (expect “1.0”); warn loudly on mismatch.
1. **Snapshot** the response verbatim to `data/universe_snapshots/universe_<YYYY-MM-DD>.json` and
   set `data/universe.json` to the latest (the working file).
1. **Record membership** in the `universe_membership` table (see schema) — this is the
   survivorship-bias defense, below.
1. **Resolve** each `ticker` → SEC CIK via the cached `company_tickers.json`. Log + report tickers
   that fail to map (delistings, ticker drift, foreign listings) rather than silently dropping them.

Expected response envelope (see endpoint spec for full contract):

```json
{
  "contract_version": "1.0",
  "generated_at": "2026-06-13T08:30:00Z",
  "count": 1234,
  "universe": [{"ticker": "ABCD", "name": "...", "sector": "Industrials",
                "market_cap": 1450000000, "exchange": "NASDAQ"}]
}
```

`ticker` is the only required per-row field; **`sector` is the one to care about** — it feeds
sector-neutralization in the Stage 6 signal layer. Sibyl resolves CIK itself; Unicorn does not
supply it.

**Survivorship bias — the part that matters for backtest honesty.** The universe is a *moving
target*: firms cross in/out of the $500M–$2.5B band, delist, get acquired. If Sibyl only ever uses
*today’s* members, the Stage 8 backtest is contaminated — the firms that dropped out (often losers)
are missing, inflating historical performance. Defenses, built in from the first run:

- **Accumulate, don’t overwrite.** Every `sibyl universe` run appends a timestamped snapshot and
  upserts the `universe_membership` table, building a point-in-time record of who was a member
  *when*. The backtest selects the right names at each historical rebalance from this table.
- **Download the union.** For filing acquisition (Stage 1), use the union of everyone who has *ever*
  been a member, not just current members.
- Past membership you never captured can’t be reconstructed — so the value is in starting
  accumulation now. v1 may accept current-only membership and *flag the bias*, but the table costs
  almost nothing and should exist from run one so history starts building immediately.
- If Unicorn turns out to retain historical membership (optional `/api/universe/history` endpoint),
  backfill from it; otherwise rely on Sibyl’s own forward-accumulating snapshots.

### Stage 1 — Download

- For each CIK: fetch submissions JSON (+ any `filings.files` for full history), filter to
  configured `form_types` within `history_start`.
- For each target filing: **skip if** its accession already exists in `filings` with a valid
  `raw_path` on disk (resumability). Otherwise download the primary document, gzip-write to
  `data/raw/<CIK>/<accession>/primary.html.gz`, write `metadata.json`, **then** insert the
  `filings` row.
- Capture `acceptanceDateTime` precisely. Respect the rate limiter on every HTTP call.
- Log progress to `data/logs/` (append completed accessions) so the on-disk log protects against
  the script itself dying mid-run (resumable: re-running skips what’s already cached).

### Stage 2 — Parse / clean

- **Python handles this well** — it’s the natural ecosystem and the work is I/O-bound, so Python’s
  raw speed is irrelevant (the heavy numeric bits later are vectorized C). Stack:
  - HTML stripping: **BeautifulSoup (`bs4`) with the `lxml` backend** (fast), or `selectolax` for
    more speed. Purpose-built SEC libraries (`sec-parser`, `edgartools`) handle filing structure
    for you — worth evaluating, but weigh the added dependency/opacity against rolling your own
    where you control everything. Recommendation: start with bs4+lxml for full control.
- Read gzipped raw, strip HTML and inline-XBRL tag soup, drop tables (mostly numbers, dilute word
  counts), normalize whitespace/unicode → `full.txt`.
- Validate by eye on a handful of known filings before trusting at scale.

### Stage 3 — Section isolation (the genuinely hard part)

- **Use `edgartools` (pinned `==5.36.0`) as the structural extractor.** A spike
  (see `/tmp/sibyl_spike_report.md` history) measured 85% clean extraction
  out-of-the-box on a hard-case sample; the library does the regex-vs-TOC-vs-
  forward-reference disambiguation for us and works on the original SEC HTML,
  not on our `full.txt`. We subclass `edgar.Filing` with `LocalFiling` (in
  `sibyl/sections.py`) so the library reads from our local
  `data/raw/<CIK>/<accession>/primary.html.gz` rather than fetching from SEC.
  No network calls at this stage. The earlier plan to roll our own regex
  stays as the documented fallback if `edgartools` ever ages out or drifts.
- **Pin and version-lock.** Bumping `edgartools` changes extracted boundaries
  silently — fatal for the yoy similarity in Stage 5. Sibyl tracks a separate
  `section_extractor_version` in each filing's `sections.json`; bumping it
  triggers a full re-extract.
- **Parallelize via `ProcessPoolExecutor`** (in `sibyl/sections.py`). The
  extractor is pure CPU and embarrassingly parallel — workers per filing,
  parent reads/writes the DB serially. See `docs/parallel_processing.md`.
- Expect a meaningful fraction to fail clean extraction. On failure, **fall back to scoring
  `full.txt`** and set `parse_status='section_fail'` — flag it, don’t drop the firm.
  Per-section statuses: `ok` / `missing` / `incorp_ref` / `over_extracted`.
- Write `sections.json` with offsets + per-section status.
- Note: 10-Q has a different/shorter structure than 10-K; handle both. (Risk Factors in 10-Q are
  often “material changes” only.) 10-Q is deferred from v1 — see §13.

### Stage 4 — Tokenize + score

- **Tokenize** = split clean text into word units for dictionary lookup: lowercase, split on word
  boundaries, strip punctuation/numbers. Match L&M’s tokenization conventions so word forms line up
  with the dictionary.
- Count each token against the L&M master dictionary categories. Produce, per section
  (`full`/`mdna`/`risk_factors`):
  - **proportional**: category count / total words (robust baseline).
  - **tfidf**: L&M’s weighted variant, downweighting near-ubiquitous words (needs a corpus-wide
    document-frequency table built in one pass first).
- Store both weightings side by side so you can later see which carries IC. Sentiment signal is
  dominated by Negative/Uncertainty, not Positive (positive language is managed and noisy) —
  don’t weight Positive like Negative.

### Stage 5 — Differencing (the edge)

- For each filing, find the **same-period prior-year filing of the same form type** (10-K↔10-K,
  Q2↔Q2 — never cross form types) via SQL on `filings`.
- Compute cosine similarity (tf-idf vectors) = Lazy Prices magnitude; and ΔUncertainty / ΔLitigious
  / ΔNegative = direction. Write to `filing_signals`.
- The yoy **alignment** is, with section isolation, the hardest correctness problem — jittery
  section extraction makes deltas mostly parsing noise. Validate alignment explicitly.

### Stage 6 — Signal layer (Phase 2, Layer 2)

Raw scores are meaningless in isolation (a 3% negative-word density means nothing until you know
it’s the 90th percentile this quarter). The signal layer converts raw scores into comparable
panels:

- **Standardize within each rebalance date** — z-score or cross-sectional rank of every firm’s
  metric, so signals are comparable across firms and across time.
- **Sector-neutralize** — subtract the sector mean (or rank within sector). Filing language differs
  systematically by industry: a biotech’s risk factors always read scarier than a utility’s, and
  you don’t want the signal to be a sector bet. Sector mapping comes from the universe metadata /
  Polygon.
- **Assemble the panel** — output `date × CIK × signal_name × zscore` with acceptance lineage
  preserved. This is the universal currency that Layer 3 evaluates and Phase 3 exports.

### Stage 7 — Price cache (Phase 2)

The backtester needs prices, so Sibyl caches its own. Pull Polygon history for the universe and
store it with the same disk discipline (gzip + a `prices` meta table in SQLite). Self-contained:
Sibyl owns its research price cache; it does not share a live feed with Unicorn. Key by CIK/ticker
with the same as-of caution (handle ticker changes via the universe mapping).

### Stage 8 — Evaluation engine (Phase 2, Layer 3 — the reusable crown jewel)

**Build this signal-agnostic.** Input: any standardized signal panel + the cached price series.
Output: the verdict on whether the signal predicts returns. Components:

- **Information Coefficient (IC)** — rank correlation of signal at *t* with forward returns over the
  horizon. The single most important number; a *stable* monthly IC of 0.03–0.05 is a genuinely good
  equity signal.
- **IC decay** — IC at multiple horizons (5d / 20d / 60d / 120d) to see how fast the edge arrives
  and dies; this dictates holding period and rebalance frequency. (Annual-filing signals are
  slower; PEAD-like drift ~60d.)
- **Quantile/decile spreads** — sort into deciles, track forward returns; require *monotonicity*
  (decile 10 > 9 > … > 1), not just a good top-minus-bottom — monotonicity separates real signal
  from a couple of lucky outliers.
- **Turnover** — period-to-period churn. The silent killer in this universe: a 0.04 IC with 200%
  monthly turnover dies to costs in illiquid small-caps.
- **Cost-aware backtest** — long-short or long-tilt sim with a spread/slippage/impact model scaled
  to your size in illiquid names. Costs are not optional here.

**The point-in-time join (do carefully):** align each filing’s signal to returns starting *after*
`acceptance_dt` plus a realistic lag (can’t trade an after-hours filing instantly — assume next-day
open). Any sloppiness manufactures the lookahead that makes a backtest look brilliant and trade like
noise. This is the whole reason the acceptance-timestamp spine exists.

**Overfitting discipline:** out-of-sample testing; and if you ever combine signals with ML
(gradient-boosted trees), use **purged k-fold CV with embargo periods** (López de Prado) to kill the
leakage from overlapping return windows. Start with a simple equal-weight or IC-weighted *linear*
composite of standardized signals — genuinely hard to beat net of costs — before reaching for trees.

### Stage 9 — Export contract (Phase 3)

The narrow, versioned interface to Unicorn Hunt. Only signals that survived Stage 8 are exported.
Keep it explicit and dumb so both sides build against it independently.

**Proposed contract (pin this down early, even if thin):** Sibyl writes, per rebalance, a panel
keyed by `(date, cik, signal_name, zscore)` plus lineage columns, to `data/exports/` as
parquet/CSV *and/or* a dedicated table Unicorn reads on a schedule. Include a `contract_version`
field. Suggested columns:

```
contract_version   TEXT      -- bump on any schema change
as_of_date         DATE      -- rebalance date
cik                INTEGER
ticker             TEXT      -- convenience
signal_name        TEXT      -- e.g. 'lm_d_unc_rf', 'lazyprices_sim_10k'
zscore             REAL      -- standardized, sector-neutralized
source_accession   TEXT      -- lineage: which filing produced it
acceptance_dt      TEXT      -- lineage: when it became public
```

Unicorn ingests this as additional factors in the screener, evaluated by its own Sharpe/IR
framework, with zero knowledge of EDGAR/L&M/tf-idf. **ACTION ITEM (Wes):** decide file-drop vs
shared table, and whether Unicorn reads Sibyl’s `data/exports/` directly or Sibyl pushes to a
Unicorn-owned location.

-----

## 9. Parsing validation — the gate before trusting any signal

**The whole project’s validity rests here.** A bad parser doesn’t error out — it emits a
plausible-looking similarity series that is actually measuring formatting noise. Validation is not
a final phase; it is interleaved into the build and *gates* the move from levels (Stage 4) to deltas
(Stage 5).

**Reframe what “good” means: consistency, not fidelity.** For a year-over-year cosine signal, the
killer is *differential* parsing error — extracting Item 1A with slightly different boundaries this
year vs last manufactures artificial “change” unrelated to the company. Year-over-year *consistency*
for the same firm matters more than absolute extraction perfection. The validation strategy is
therefore a hunt for extraction that is *unstable across time for the same firm*. (A Stanford
replication of Lazy Prices makes exactly this point: comparing macroscopically dissimilar sections
year to year misrepresents the degree of change.)

### 9.1 The validation plan (build in this order)

1. **Labelled validation set (do first).** Hand-pick 20–30 filings spanning the hard cases: a clean
   modern HTML 10-K; an old pre-2001 plain-text filing; one with an oddly formatted Item 1A header;
   a post-acquisition filing (text bloat); a 10-Q (different structure); one using “incorporated by
   reference” where Risk Factors point elsewhere. Manually note where Item 1A and Item 7 truly begin
   and end. This is ground truth — tedious but the only way to know the parser *works* vs *runs*.
1. **Validate section isolation against it.** Compare extracted boundaries to hand labels: found at
   all? boundaries right? This surfaces the classic traps — the forward-reference (“see Item 1A”),
   the table-of-contents false positive (TOC lists “Item 1A” before the real section), header format
   variants (“ITEM 1A.” vs “Item 1A —” vs bold-no-period). Iterate the anchor/regex logic until the
   set passes.
1. **Instrument the parser to flag its own failures** (highest-leverage move). Emit diagnostics per
   filing; quarantine outliers via `parse_status` rather than silently scoring them:
- *Section-length sanity*: a Risk Factors section of 200 or 80,000 words is almost certainly a
  failure (truncated, or ran past the boundary into the next item). Flag extremes.
- *Year-over-year length jumps*: Item 1A of 5,000 words one year and 500 the next is far more
  likely a parse break than a real disclosure change. **The single most useful red flag** — it
  directly targets differential error.
- *Section-found rate*: track the fraction of filings yielding clean Item 1A/Item 7. A sudden
  drop for a subset (e.g. all pre-2005) means a format the parser doesn’t handle.
1. **The decisive test — does the *signal* behave?**
- *Similarity distribution sanity*: most firm-years should be highly similar to their prior year
  (filings are sticky), with a thin tail of genuine large changes. A distribution that’s all over
  the place means parsing is injecting noise.
- *Eyeball the extremes (the gate)*: read the ~20 filings flagged as most-changed YoY. If they’re
  substantively changed (new risk factors, restructuring, going-concern language), the signal is
  real. If they’re formatting artifacts (reformatted table, renumbered section), the “change” is
  noise — fix the parser. **Do not believe any IC number until this tail check passes.**
1. **Cross-check against a reference.** Spot-check your per-filing word counts for a sample against
   Notre Dame SRAF’s pre-computed 10-X word counts (the ~16 GB file). Wild divergence from a
   respected reference parser means a cleaning bug.

### 9.2 What the Lazy Prices paper actually did (use as the template)

Cohen, Malloy & Nguyen, “Lazy Prices” (Journal of Finance 75(3), 2020, pp. 1371–1415; NBER WP
25084; SSRN 1658471). The make-or-break details worth copying:

- **Same-type, period-aligned comparison.** They compare each filing to the *same firm’s* prior
  comparable filing — 10-K to prior-year 10-K, 10-Q to the same fiscal quarter prior year — over the
  complete history of regular annual/quarterly filings (their sample spanned 1994–2014, covering
  10-K, 10-K405, 10-KSB, and 10-Q). This is exactly the Stage 5 alignment rule: never cross form
  types, always same-period.
- **Four similarity measures, not one.** Cosine similarity, Jaccard similarity, minimum edit
  distance, and a “simple” side-by-side measure. Cosine and Jaccard are the cheap robust pair to
  start with; computing more than one is a cross-check against any single measure’s artifacts.
- **The result is economically large and inattention-driven.** A portfolio shorting “changers” and
  buying “non-changers” earned up to ~188 bps/month of alpha (>22%/yr), and — tellingly — there was
  *no* announcement effect, implying investors simply don’t read the diffs. The edge is the tedium
  of comparison, which is why it survives and why it concentrates in low-coverage small caps.
- **Length context:** 10-K length grew roughly six-fold since 1995, so raw documents are long and
  boilerplate-heavy — which is *why* section isolation and preprocessing discipline matter so much;
  the signal lives in targeted changes, not document bulk.
- Follow-up work emphasises the predictive content concentrates in **Risk Factors and MD&A**, and
  that the *direction* of sentiment in the changed text adds information beyond change magnitude —
  i.e. pair the cosine signal with an L&M sentiment delta (exactly Sibyl’s Stage 5 design).

**Note on figures:** the ~188 bps/month and sample-window numbers are from the published paper as
retrieved; treat as directional and re-verify against the JF/NBER PDF before citing in the lecture
or design docs.

### 9.3 Where this sits in the build

Add the labelled set as the *first* task of parsing work; make the parser emit diagnostics from day
one; **gate the move to Stage 5 on the tail-eyeball test passing.** The failure mode is detectable
with cheap manual checks if the instrumentation exists — the only truly dangerous path is trusting a
parser that runs without error and never reading what it extracted.

-----

## 10. L&M dictionary & NLP code — what to download vs build

**De-risk this FIRST — before the universe export or the downloader.** It’s a five-minute check
that removes an external dependency, but it is *not* a real roadblock candidate: the dictionary is a
single static CSV (a few MB), not an API or service that can rate-limit or go down. Download once,
cache, done.

**Verified source (checked Jun 2026):** Notre Dame Software Repository for Accounting and Finance
(SRAF), `https://sraf.nd.edu/loughranmcdonald-master-dictionary/`. Current file as of the March 2026
update is `Loughran-McDonald_MasterDictionary_1993-2025.csv` (also an `.xlsx`). Cache it at
`data/lm_master_dictionary.csv`.

**File shape:** one row per word; columns flag category membership (Negative, Positive,
Uncertainty, Litigious, Strong_Modal, Weak_Modal, Constraining) plus frequency/metadata. A word can
belong to multiple categories. The category columns hold the year the word was assigned (non-zero =
member). Open it once and confirm the columns match this before building the scorer.

SRAF also publishes a helper module `MOD_Load_MasterDictionary_v2024.py` and a reference
`Generic_Parser.py` — useful to read for how they tokenize, but see build-vs-download below.

**LICENSE CAVEAT — relevant to you specifically.** The dictionary is free for academic /
non-commercial research; commercial use requires a license (contact [loughranmcdonald@gmail.com](mailto:loughranmcdonald@gmail.com)).
Given you work at a bank, decide honestly whether a personal research project sits on the
non-commercial side. For a private screener you run yourself it almost certainly does, but flag it
rather than ignore it. (Added to §13.)

**Download vs build — the split:**

- **Download the *data*** (the word lists / master dictionary CSV). Never reimplement the lexicon.
- **Build the *scoring code*** (tokenize + count). It’s ~50 lines: tokenize, look each word up in
  the category sets, increment counters, divide by total words. Write it yourself rather than using
  a wrapper (`pysentiment2` et al.) for three reasons: you control tokenization so word forms match
  the dictionary exactly; you control point-in-time text extraction; you avoid a black box whose
  tokenization choices are exactly where silent signal-quality bugs hide. Reading SRAF’s
  `MOD_Load_MasterDictionary` / `Generic_Parser` for reference is fine; depending on them is not.
- **Download/use the *library* for tf-idf + cosine** (Stage 5): **scikit-learn**
  (`TfidfVectorizer`, `cosine_similarity`). Standard, battle-tested, sparse — do NOT reimplement
  this; no edge or risk lives here.

So: build the dictionary-counting, use scikit-learn for tf-idf, download only the dictionary data.

Categories: Negative, Positive, Uncertainty, Litigious, Strong Modal, Weak Modal, Constraining.
Signal is dominated by Negative/Uncertainty; Positive is managed and noisy — don’t weight it like
Negative.

*(Aside, optional: SRAF also offers a pre-computed `10X_DocumentDictionaries` file with word counts
for the entire 10-X archive — but it’s ~16 GB and uses their parsing, not your point-in-time
controlled extraction. Not the main path; possibly useful later for cross-validation of your own
counts.)*

-----

## 11. Deployment — local-first

Everything runs on the Mac, writing to the internal disk. No droplet, no tmux, no rsync in normal
operation.

**Footprint (with gzip, internal disk):** full universe + ~10y ≈ **5–10 GB** compressed raw; clean
text + DB ≈ **1–3 GB**; 10-K-only v1 ≈ **1–2 GB**. The ~100 GB figure was the discarded
uncompressed worst case, not what you store.

**Running the bulk download locally:** the wall-clock is bounded by EDGAR’s rate limit (~8/s), *not*
disk or bandwidth — roughly 2–3 hours for full universe + history, or ~40 min for a 10-K-only v1.
Two things make an unattended local run safe:

- **`caffeinate`** — run `caffeinate -i sibyl download` to prevent idle sleep for the duration, so
  the lid/sleep doesn’t interrupt it.
- **Resumability** — even if interrupted, re-running `sibyl download` skips everything already
  cached (the DB is the log). A sleep or wifi drop is not fatal.

Running from a home IP in Singapore is fine — EDGAR’s limit is per-IP and location-agnostic.

**Storage caveats:**

- **Never run the project out of iCloud Drive (or any synced/network folder).** Not a speed issue —
  a correctness one: (1) “Optimize Mac Storage” evicts cold files to placeholders, breaking the
  cache; (2) **SQLite must not live on a synced folder** — file locking misbehaves and sync can
  corrupt the DB mid-write. iCloud is fine only as a place to stash a compressed *backup tarball*
  (archive, not working directory).
- If internal space gets tight later, the clean fix is an **external USB-C SSD**: point
  `paths.data_root` at `/Volumes/YourSSD/sibyl-data` — one config line, nothing else changes (this
  is what the config-driven paths are for). Fast enough for one-file-at-a-time I/O.

**One-off cloud VM for big bulk jobs:** the config-driven paths and resumable design make Sibyl
portable to a rented machine without any code change — just `rsync` `data/raw/`, `sibyl.db`, and
the L&M dictionary across, run the stage, `rsync` `data/clean/` back. For the **10-Q expansion
specifically** (~5× the corpus), a single 32-core DigitalOcean droplet rented for ~2 hours
finishes the work for ~$10-15. Detailed runbook in `docs/scaling_10q_with_cloud_vm.md`. This does
not change the local-first principle; it just operationalises the portability that was already
designed in.

**Optional droplet, later (not now):** the only thing that benefits from an always-on box is an
unattended *incremental* updater (`sibyl download` on a cron after earnings season). Because paths
are config-driven, deploying that later is trivial and doesn’t change the local-first development
story. If you do, that’s the one scenario where the two-machine `.db`-source-of-truth discipline
returns — handle it then, not now.

-----

## 12. Build order / milestones

**Phase 1 — EDGAR pipeline (local):**

1. **De-risk externals (~30 min)** — (a) download + eyeball the L&M dictionary CSV (§10), confirm
   columns; (b) check internal disk free space; confirm headroom for chosen scope.
1. **Scaffolding** — package layout (incl. empty Layer 2/3 packages), config loader, `.gitignore`,
   `db.py` with the schema, CLI skeleton with no-op subcommands.
1. **Stage 0** — *prereq:* Unicorn `/api/universe` endpoint built + deployed, host+token in `.env`.
   Then `sibyl universe`: fetch → snapshot → record membership → resolve ticker→CIK. Verify mapping
   coverage; confirm membership accumulation is happening from run one.
1. **Stage 1 downloader** — resumable, rate-limited, gzipped, on-disk log, write-file-then-DB-row.
   **Get this and the point-in-time spine bulletproof on a small set of CIKs first.** Then run the
   full bulk pull locally under `caffeinate`.
1. **Stages 2–4** — parse → (section isolation) → tokenize → L&M **levels**. Eyeball levels against
   known filings (a firm that had a bad year should light up Negative/Litigious) before trusting.
1. **Parsing-validation gate (§9)** — build the labelled set *first*, instrument the parser with
   the diagnostic flags, and **do not proceed to Stage 5 until the tail-eyeball test passes.** This
   is the project’s load-bearing checkpoint.
1. **Stage 5** — yoy similarity + deltas. Only after levels look sane and the gate passes, since
   deltas amplify any parsing/alignment inconsistency.

**Phase 2 — signal + evaluation layers:**
8. **Stage 7 (prices)** — pull + cache Polygon history for the universe.
9. **Stage 6 (signal layer)** — standardize + sector-neutralize into panels.
10. **Stage 8 (evaluation engine)** — build it **signal-agnostic**: IC → decay → quantiles →
turnover → cost-aware backtest, with the careful point-in-time join. Validate that L&M deltas and
Lazy Prices actually carry IC in the universe. This harness is the reusable crown jewel — every
future signal (accruals, PEAD) runs through it.

**Phase 3 — integration:**
11. **Stage 9 (export contract)** — pin the panel schema + version; emit validated panels; wire
Unicorn Hunt to ingest them as factors. Only Phase-2-surviving signals get here.

Validate at each stage before moving on. The hard parts are §8-Stage 3 (section isolation),
§8-Stage 5 (yoy alignment), and §8-Stage 8 (the honest point-in-time join + cost model) — not the
dictionary counting.

-----

## 13. Open decisions / things flagged for Wes

- [x] **Unicorn universe endpoint** — deployed at `https://api.unicornpunk.org/api/universe`
  (Caddy → uvicorn on the existing droplet). Bearer token (`SIBYL_API_TOKEN` server-side /
  `SIBYL_UNICORN_TOKEN` Sibyl-side) generated and installed in both `.env` files. Unicorn does NOT
  retain historical membership — Sibyl accumulates `universe_membership` snapshots from run one.
- [ ] **Export contract delivery** (Phase 3) — file-drop in `data/exports/` that Unicorn reads, vs
  a shared table vs Sibyl pushing to a Unicorn-owned location. See Stage 9.
- [x] **v1 scope** — **decided: 10-K only, history from 2016**. Levers are config-only
  (`form_types`, `history_start` in `config.yaml`). 10-Q expansion is a later pass — runbook in
  `docs/scaling_10q_with_cloud_vm.md`; threshold recalibration + form-aware yoy pairing required
  on Sibyl side before flipping the switch.
- [x] **Amendments** — **decided: excluded** from v1. The Stage 1 filter drops anything ending in
  `/A`. Add later as a separate pass if/when yoy pairing rules are settled.
- [x] **Section isolation library vs roll-your-own regex** — **decided: `edgartools==5.36.0`**
  after a spike measured 85% clean-extraction on a hard-case sample. The fallback regex approach
  stays documented in §8 Stage 3 should the library ever age out.
- [ ] **Tables in parse** — recommend dropping for word-count signals; revisit if a numeric signal
  is added later. (Stage 2 currently drops them.)
- [ ] **Section scope for scoring** — score `full`, `mdna`, `risk_factors` all; decide later which
  drives IC. (Risk Factors + MD&A are where the information is; full document includes
  boilerplate that dilutes.)
- [ ] **L&M dictionary license** — free for non-commercial/academic; commercial needs a license.
  Decide whether your personal research project is non-commercial (almost certainly yes for a
  private screener, but flag given the bank context). See §10. (Dictionary downloaded; loads cleanly.)
- [ ] **tf-idf vs proportional** — build both, compare empirically; don’t assume.
- [ ] **Holding horizon / rebalance frequency** — set after seeing IC-decay in Stage 8, not before.
- [ ] **Sibyl as research-flagging tool vs systematic trader** — recommend the former (alpha-flag
  layer feeding Unicorn), far more achievable solo; decide before Phase 3 shapes the export.

-----

## 14. Gotchas (things easy to get wrong)

- **No User-Agent / over rate limit → SEC blocks you.** Set UA on every call, throttle to ≤8/s.
- **`period_of_report` is not the point-in-time field.** Only `acceptance_dt` is. Any join on
  period-end manufactures lookahead.
- **Restated vs as-filed.** Score the as-filed text you downloaded; never substitute a backfilled
  series. (You’re storing the actual filed document, so you’re safe by construction — keep it that
  way.)
- **CIK is the identity, not ticker.** Tickers get reused/reassigned; CIK never does. Store ticker
  as a convenience column, key everything on CIK/accession.
- **Write file before DB row** (crash safety, §5.3).
- **Section-isolation jitter** silently corrupts yoy deltas. Treat `parse_status` as first-class
  data and validate alignment.
- **`data/` must be gitignored** — never commit GBs of filings or the DB.
- **Never put the SQLite DB on iCloud/synced folders** — corruption risk, not just slowness (§11).
- **The Stage 8 point-in-time join is where lookahead sneaks back in** — align to `acceptance_dt` +
  next-day lag, never to period-end, and never to the filing’s own date without the trading lag.
- **Keep Layer 3 signal-agnostic** — if `eval/` ever imports from the L&M code, the engine has
  stopped being reusable. Panels in, verdicts out.
- **Survivorship bias** — only ever using *today’s* universe silently inflates backtest results.
  Accumulate `universe_membership` from the first run; download the union of all-time members; never
  overwrite snapshots.