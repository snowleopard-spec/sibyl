# Plan — LLM audit of Stage 3 extractions

Self-contained brief for the next session. The point: build a script
that uses Claude to spot-check Sibyl's Stage 3 section extractions
across a random sample, reporting `% clean / partial / wrong` plus a
list of failures to eyeball.

---

## Context

- **Sibyl is a SEC-filing signal research engine.** Phase 1 (EDGAR
  pipeline) is done — see `docs/HANDOVER.md` for the overall state.
- **Stage 3 isolates two sections** from each 10-K:
  - **Item 1A → `data/clean/<CIK>/<accession>/risk_factors.txt`**
    (the Risk Factors section — heart of the Lazy Prices signal)
  - **Item 7 → `data/clean/<CIK>/<accession>/mdna.txt`**
    (Management's Discussion and Analysis — heart of the L&M sentiment
    signal)
- Both sections are extracted using `edgartools==5.36.0` via a
  `LocalFiling` override (no network). 98.5% of filings have
  `parse_status='ok'` per the corpus pass.
- **Layer 1+2 validation already passed** (auto stats + flags
  surfaced a `285`-row suspicious cohort, year-coverage 0.97-1.00).
- **Layer 3 is pending.** Two paths exist:
  - **Hand-labelled set** at `data/clean/validation_labels.csv` —
    20 hard-case filings, blank label columns ready (per spec §9).
    Reserve this as belt-and-suspenders for later if Stage 5 yoy
    similarity looks suspicious.
  - **LLM audit** — this plan. Audits N=100 random `ok` filings
    for "does the extraction look right at a glance?" Catches
    common-case failures cheaply.

The two checks measure different things:
- Hand-labelled set: **precision** on hard cases — boundaries within
  ~200 chars of truth
- LLM audit: **coverage** on typical cases — does the extraction look
  like Item 1A / Item 7 at all

The LLM audit is the higher-value next step.

---

## Goal

Build `scripts/llm_audit.py` that:

1. Samples N filings (default 100) with `parse_status='ok'` from
   `data/sibyl.db`.
2. For each, calls the Claude API (Anthropic SDK) with structured
   context and asks Claude to judge the extraction quality of
   `risk_factors.txt` and `mdna.txt`.
3. Aggregates results into a summary report + a CSV of per-filing
   verdicts.
4. Surfaces the failures (`partial` / `wrong`) for manual eyeballing.

Estimated cost at 100 filings, Sonnet 4.6, with prompt caching: **~$1-3
total**. Wall time: **~5 min serial**, **~30 sec async**.

---

## Design

### Sampling

```python
def sample_filings(conn, n=100, seed=42, only_ok=True) -> list[tuple[int, str]]:
    """Random sample of (cik, accession) where parse_status='ok' AND
    both sections.json status fields are 'ok'."""
```

Stratification is optional. A flat random over all `ok` filings is fine
for the first pass; if year-coverage matters later, stratify by
filing-year.

### Per-filing prompt structure

For each sampled filing, build a prompt with these pieces:

1. **Static instructions** (cached) — what we're auditing and how to
   respond.
2. **Per-filing variable content** (not cached) — the extracted +
   reference text excerpts.

#### Instructions (cached prefix)

```
You are auditing automated extractions of SEC 10-K sections from a
financial research pipeline. The extractor outputs two text files per
filing:

- risk_factors.txt — should contain Item 1A (Risk Factors)
- mdna.txt        — should contain Item 7 (Management's Discussion and
                    Analysis of Financial Condition and Results of
                    Operations)

For each section, judge whether the extracted text correctly represents
the corresponding Item of the 10-K. Return one of three verdicts:

- "clean":   starts at the right place, ends at the right place, and
             contents are coherent prose from that Item.
- "partial": correct section but with notable boundary issues —
             leading TOC entries, premature cutoff, signatures bleeding
             in at the end, etc.
- "wrong":   the extraction is not Item 1A / Item 7 at all, or is
             empty/junk.

Output strict JSON of the form:
{
  "risk_factors": {"verdict": "clean|partial|wrong", "reason": "<one sentence>"},
  "mdna":         {"verdict": "clean|partial|wrong", "reason": "<one sentence>"}
}

You will see:
- COVER PAGE: first ~2k chars of the filing's full text
- FULL_NEAR_ITEM_1B: ~1.5k chars around "Item 1B" in the full filing
  (this is approximately where Item 1A should end)
- FULL_NEAR_ITEM_8:  ~1.5k chars around "Item 8" in the full filing
  (this is approximately where Item 7 should end)
- RISK_FACTORS_HEAD: first 2k chars of the extracted Risk Factors
- RISK_FACTORS_TAIL: last 1k chars of the extracted Risk Factors
- MDNA_HEAD:         first 2k chars of the extracted MD&A
- MDNA_TAIL:         last 1k chars of the extracted MD&A

Compare. The HEAD of each extraction should look like the start of
that Item, not a TOC line or a cover-page line. The TAIL should look
like content from late in that Item, not from a subsequent Item or the
signatures.
```

#### Variable content per filing

```
=== Filing CIK <CIK> accession <ACCESSION> ===

--- COVER PAGE (first 2000 chars of full.txt) ---
<excerpt>

--- FULL_NEAR_ITEM_1B (find "Item 1B" in full.txt; show 750 chars before + 750 chars after) ---
<excerpt>

--- FULL_NEAR_ITEM_8 (find "Item 8" in full.txt; show 750 chars before + 750 chars after) ---
<excerpt>

--- RISK_FACTORS_HEAD (first 2000 chars of risk_factors.txt) ---
<excerpt>

--- RISK_FACTORS_TAIL (last 1000 chars of risk_factors.txt) ---
<excerpt>

--- MDNA_HEAD (first 2000 chars of mdna.txt) ---
<excerpt>

--- MDNA_TAIL (last 1000 chars of mdna.txt) ---
<excerpt>
```

#### Token math

- Static instructions: ~600 tokens (cached after first call → reads at
  10% of base price)
- Per-filing variable: ~3,000 tokens uncached input
- Output: ~150 tokens

Per filing cost at Sonnet 4.6 ($3/MT input, $15/MT output):
- First call (no cache): 3,600 × $3/1M + 150 × $15/1M = $0.013
- Subsequent (with cache hit on instructions):
  600 × $0.30/1M + 3000 × $3/1M + 150 × $15/1M = $0.0093

100 filings ≈ **$0.95**. With Opus 4.7 (5x cost) ≈ $4.75.

### Model choice

**Default: `claude-sonnet-4-6`.** This is a structured judgment task
(reading text, matching against expectations). Sonnet is plenty
capable; Opus is overkill.

Make it a CLI flag (`--model`) for easy override.

### Concurrency

Use `anthropic.AsyncAnthropic` with `asyncio.gather()` and a small
semaphore (e.g. 10 concurrent). 100 filings at ~3 sec each
sequentially is 5 min; at 10 concurrent ~30 sec.

If async feels like overkill for v1, sequential is fine — still
finishes in ~5 min.

### Output

Two files in `data/audits/`:

1. **`audit_<UTC-stamp>.json`** — full results:
   ```json
   {
     "model": "claude-sonnet-4-6",
     "ran_at": "2026-06-19T...",
     "n_sampled": 100,
     "seed": 42,
     "results": [
       {
         "cik": 320193, "accession": "0000320193-23-000106",
         "risk_factors": {"verdict": "clean", "reason": "..."},
         "mdna":         {"verdict": "clean", "reason": "..."}
       }, ...
     ]
   }
   ```

2. **`audit_<UTC-stamp>.csv`** — tabular per-filing verdicts for
   easy spreadsheet review.

Console output: aggregate summary + failure list:

```
Audit of 100 ok filings (model claude-sonnet-4-6, seed 42, cost ~$0.95)

risk_factors:  92 clean / 6 partial / 2 wrong
mdna:          95 clean / 4 partial / 1 wrong
combined ok:   88 / 100 (both sections clean)

Failures to eyeball:
  CIK 320193 / 0000320193-23-000106  risk_factors: partial
    "leads with TOC entry before real section start"
  CIK 1234567 / 0001234567-22-000010  mdna: wrong
    "extracted text appears to be Item 8 (financial statements), not Item 7"
  ...
```

### File structure

```
sibyl/
├── scripts/
│   └── llm_audit.py           # NEW — the audit script
├── pyproject.toml             # add 'anthropic' as an optional dep group
└── data/
    └── audits/                # NEW — output dir; gitignored
        ├── audit_<stamp>.json
        └── audit_<stamp>.csv
```

Add to `.gitignore`:
```
data/audits/
```

The `anthropic` SDK should be an **optional** dependency group:
```toml
[project.optional-dependencies]
audit = ["anthropic>=0.50"]
```

Reason: the audit is a one-off check, not part of the core pipeline.
Keeps `pip install -e .` lean.

Install: `pip install -e ".[audit]"`

### CLI

```
python scripts/llm_audit.py \
    --n 100 \
    --seed 42 \
    --model claude-sonnet-4-6 \
    --concurrency 10
```

Defaults: `--n 100 --seed 42 --model claude-sonnet-4-6 --concurrency 1`
(sequential by default; user adds concurrency explicitly).

API key picked up from environment: `ANTHROPIC_API_KEY`.

### Validation steps (do these in the next session, in order)

1. **Verify SDK installs cleanly**:
   `.venv/bin/pip install -e ".[audit]"` should succeed without
   touching the existing deps.
2. **Smoke test on N=3** Apple filings (CIK 320193, fixed seed):
   manually verify Claude's verdicts look reasonable. Iterate on the
   prompt if needed.
3. **N=20 dry run** to validate cost projection and parsing robustness.
4. **N=100 main run.** Save the report.
5. **Eyeball every flagged failure** in VS Code (open
   `full.txt` + the relevant extracted file alongside).
6. **Decide:**
   - **≥ 90% combined `clean`** → Stage 3 declared trusted; proceed
     to Stage 4 planning.
   - **70-90%** → look at the failure patterns. If they're
     concentrated in one filing type (e.g., all pre-2018), targeted
     fix. If scattered, accept and note as known limitation.
   - **< 70%** → larger problem, revisit Stage 3 design.

---

## Limitations to keep in mind

- **LLM audit ≠ ground-truth boundaries.** Claude judges "does this
  look right" — not "is the start within 200 chars of truth." A
  systematic 100-char drift would slip through this audit but get
  caught by the hand-labelled set.
- **Claude could be fooled by plausible-looking but wrong content.**
  E.g., if `risk_factors.txt` is a clean extraction of Item 7
  mislabelled as Item 1A, Claude might still rate it "clean" because
  the text reads coherent. Mitigation: include the cover-page +
  near-Item-1B context so Claude can cross-check.
- **Model upgrade risk.** A future Claude model might judge differently.
  Re-running the audit after model bump may produce different
  percentages. Mitigate by recording `model` in the output.
- **Sample is random, not exhaustive.** 100 / 9,143 = 1.1% sample. Wide
  confidence intervals on per-year breakdowns. Fine for global "does
  this generally work?" judgment; not fine for finding rare systemic
  failures.

---

## Out of scope

- Hand-labelled `validation_labels.csv` (deferred — useful belt-and-
  suspenders if Stage 5 signal looks suspicious).
- Auditing `parse_status='section_fail'` filings — these are already
  flagged. Audit only the `ok` ones.
- Re-running the audit on a schedule. v1 is one-shot. If parser bumps
  later, re-run manually.
- Per-filing prompt caching at finer granularity (e.g., caching
  per-CIK history). Not worth the complexity for 100 filings.
- Using a different LLM provider for cross-validation. Out of scope;
  would add real complexity for marginal gain.

---

## Resume checklist for the next session

```
[ ] Confirm ANTHROPIC_API_KEY is set (or get one at console.anthropic.com)
[ ] Add 'anthropic' to pyproject.toml optional-deps
[ ] Add 'data/audits/' to .gitignore
[ ] Write scripts/llm_audit.py
[ ] Smoke test on N=3 Apple filings, eyeball prompt+verdicts
[ ] Iterate on prompt if smoke results look off
[ ] N=20 dry run, validate cost projection
[ ] N=100 main run
[ ] Eyeball flagged failures
[ ] Decide: trust Stage 3 → plan Stage 4, OR fix Stage 3 first
[ ] Commit the script (not the audit results — those are gitignored)
```

If anything in this plan needs revising once you start, just edit this
file or supersede with a fresh plan.
