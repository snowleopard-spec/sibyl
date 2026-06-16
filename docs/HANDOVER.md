# Sibyl — Handover Note

*Last updated: 2026-06-16 (Stage 3 corpus pass complete, Layer 3 labelling pending).*

This document is enough context for someone (or a fresh Claude Code
window) to pick up Sibyl exactly where the prior session left off.

---

## Where you are in one paragraph

Phase 1 of Sibyl is mostly built. Stages 0, 1, 2, 3 are **done**.
Stage 3's corpus pass finished at **98.5% both-ok** across 9,143
filings (Layer 1 + 2 validation gates passed). The remaining gate
before Stage 4 is **Layer 3 — hand-labelling 20 hard-case filings**.
A candidate CSV is already generated at
`data/clean/validation_labels.csv`; instructions in
`docs/validation_labelling_guide.md`. Once labelled and validated,
plan Stage 4 (L&M scoring).

---

## Stage progress

| Stage | Status | Notes |
| --- | --- | --- |
| **0 — `sibyl universe`** | ✅ done | 1,263 tickers from Unicorn, 1,263 CIKs resolved (8 dotted share classes still unresolved) |
| **1 — `sibyl download`** | ✅ done | 9,144 10-Ks, 2.4 GB raw, 0 failures. v1 scope: 10-K only, 2016+, no amendments |
| **2 — `sibyl parse`** | ✅ done | 9,143 / 9,144 ok (99.99%). 1 expected fail (Windstream shell). Eyeball gate passed. 3.0 GB clean |
| **3 — `sibyl sections`** | ✅ done; Layer 3 pending | `edgartools` (pinned 5.36.0) via `LocalFiling` override + `ProcessPoolExecutor`. Corpus pass: 9,008 both-ok / 135 section_fail / 285 suspicious. Year-by-year coverage 0.97–1.00. 4.6 GB clean. See `docs/parallel_processing.md`. |
| 4 — `sibyl score` | ⏸ stub. L&M dictionary already downloaded |
| 5 — `sibyl diff` | ⏸ stub |
| 6 — `sibyl panel` | ⏸ stub (Phase 2) |
| 7 — `sibyl prices` | ⏸ stub (Phase 2) |
| 8 — `sibyl eval` | ⏸ stub (Phase 2) |
| 9 — `sibyl export` | ⏸ stub (Phase 3) |

51 tests pass. Test suite covers Stages 0-3 (Stage 3 now includes
multiprocessing workers-parity + picklable checks).

---

## The single action to take when resuming

Do the **Layer 3 hand-labelling** of `data/clean/validation_labels.csv`
per `docs/validation_labelling_guide.md`. ~30-60 min of manual work.
Then:

```bash
.venv/bin/sibyl sections --validate
```

**Gate criteria** (must pass before Stage 4):
- Section recall ≥ 90%
- Start-accuracy ≥ 85% (within 200 chars of true start)

If gate passes → plan Stage 4 (L&M scoring per section). If it
fails → look at the failing rows; the fix is usually a bump to
`EXTRACTOR_VERSION` in `sibyl/sections.py` followed by
`sibyl sections --force`.

Diagnostic commands worth running anytime:

```bash
.venv/bin/sibyl sections --stats              # Layer 2 diagnostics
.venv/bin/sibyl sections --sample 5            # random ok extractions
.venv/bin/sibyl sections --sample 5 --suspicious  # flagged ones
```

---

## Project facts

- **What Sibyl is:** offline batch signal research engine. Acquires SEC
  10-Ks → cleans → isolates Item 1A & Item 7 → scores with L&M sentiment
  → tests if signals predict returns. Validated signals later feed into
  **Unicorn Hunt** (the user's live small-cap screener).
- **User:** works at a bank, based in Singapore. Sibyl is personal
  research. EDGAR/Polygon/Unicorn integrations run from a Mac on a
  home IP.
- **Project root:** `/Users/wessch/Projects/Projects/sibyl/`
- **Spec (source of truth):** `docs/SIBYL BUILD SPEC.md`
- **Unicorn contract spec:** `SIBYL_HANDOFF.md` (project root if present; otherwise see git history)
- **Labelling workflow:** `docs/validation_labelling_guide.md`
- **Plan files (Claude harness):**
  `/Users/wessch/.claude/plans/hidden-yawning-quilt.md` — gets
  overwritten each stage. Current contents = revised Stage 3 plan.
- **Memory (Claude harness):**
  `/Users/wessch/.claude/projects/-Users-wessch-Projects-Projects-sibyl/memory/`
  — MEMORY.md index + four memory files (project, user, boundary).

---

## CLI surface

```
sibyl universe           # Stage 0: fetch universe from Unicorn
sibyl download           # Stage 1: bulk pull 10-Ks (resumable)
sibyl parse              # Stage 2: HTML → full.txt
  --stats                # Layer 2 diagnostics
  --sample N [--suspicious]
sibyl sections           # Stage 3: extract Item 1A + Item 7
  --cik N                # smoke testing
  --limit N
  --force                # re-extract at current EXTRACTOR_VERSION
  --stats
  --sample N [--suspicious]
  --pick-validation-set N
  --validate
sibyl status             # DB row counts + disk usage
sibyl score / diff / prices / panel / eval / export  # all stubs
```

All commands also accept `--config <path>` (defaults `config.yaml`).

---

## File layout (project root)

```
sibyl/
├── pyproject.toml       # deps: requests, pyyaml, python-dotenv, bs4, lxml, edgartools==5.36.0
├── README.md
├── config.example.yaml  # template
├── config.yaml          # GITIGNORED — real config (Unicorn host, SEC UA)
├── .env                 # GITIGNORED — SIBYL_UNICORN_TOKEN
├── .gitignore
│
├── sibyl/               # the package
│   ├── __init__.py
│   ├── cli.py           # argparse entry; main()
│   ├── config.py        # YAML + .env loader → Config dataclass
│   ├── db.py            # sqlite3 schema (universe_membership, filings, filing_scores, filing_signals)
│   ├── edgar.py         # SEC HTTP: RateLimiter (8/s), sec_get, retry, URL builders
│   ├── universe.py      # Stage 0 implementation
│   ├── download.py      # Stage 1 implementation
│   ├── parse.py         # Stage 2 implementation + cleaning + bs4
│   ├── sections.py      # Stage 3 implementation (edgartools wrapper, LocalFiling, flagging)
│   ├── lm_dictionary.py # L&M loader (used by future Stage 4)
│   ├── score.py / diff.py / signals.py / prices.py / export.py  # stubs
│   └── eval/__init__.py # stub
│
├── docs/                # canonical specs + explainers + this handover
│   ├── HANDOVER.md                    # ← this file
│   ├── SIBYL BUILD SPEC.md            # spec (source of truth)
│   ├── parallel_processing.md         # Stage 3 multiprocessing in plain terms
│   ├── scaling_10q_with_cloud_vm.md   # runbook for the 10-Q expansion on a DO droplet
│   └── validation_labelling_guide.md  # Layer 3 hand-labelling workflow
│
├── tests/               # 51 tests, all offline (mocked SEC for downloader)
│   ├── test_config.py
│   ├── test_db_schema.py
│   ├── test_universe.py
│   ├── test_download.py
│   ├── test_parse.py
│   ├── test_sections.py
│   └── fixtures/
│
└── data/                # GITIGNORED
    ├── raw/<CIK>/<accession>/primary.html.gz + metadata.json  # immutable
    ├── raw/<CIK>/submissions.json + submissions_*.json        # SEC cache
    ├── clean/<CIK>/<accession>/full.txt                       # Stage 2 output
    ├── clean/<CIK>/<accession>/risk_factors.txt + mdna.txt    # Stage 3 output (corpus pass complete; 9143 at v1)
    ├── clean/<CIK>/<accession>/sections.json                  # per-filing metadata
    ├── clean/validation_labels.csv                            # Layer 3 hand-labelling target (20 rows, blank)
    ├── sibyl.db                                               # SQLite
    ├── universe.json                                          # latest snapshot
    ├── universe_snapshots/                                    # accumulating point-in-time membership
    ├── company_tickers.json                                   # SEC ticker→CIK cache
    ├── lm_master_dictionary.csv                               # 86,553 words, downloaded
    └── logs/                                                  # per-run logs
```

---

## Key conventions to preserve

1. **Point-in-time discipline.** `acceptance_dt` is the only PIT field;
   never join on `period_of_report`. Already baked into the schema and
   `filings` row writes.
2. **Write file before DB row.** Crash safety. Already in
   `sibyl/download.py` and `sibyl/parse.py`. Maintain in any new stage.
3. **`raw/` is immutable; `clean/` is regenerable.** `rm -rf data/clean
   && sibyl parse && sibyl sections` rebuilds the entire clean side
   from scratch without re-hitting SEC.
4. **Version bumps re-trigger work.** Two independent versions in
   `sections.json`:
   - `parser_version` (current: "2") — bump in `sibyl/parse.py` to
     invalidate Stage 2.
   - `section_extractor_version` (current: "1") — bump in
     `sibyl/sections.py` to invalidate Stage 3.
   - `sibyl parse --force` or `sibyl sections --force` re-processes.
5. **`edgartools==5.36.0` is pinned** in `pyproject.toml`. Version drift
   would silently change extracted boundaries — fatal for yoy similarity.
   Bumping is an explicit decision + full re-extract.
6. **No network calls in Stage 3.** `LocalFiling` subclass in
   `sibyl/sections.py` overrides `Filing.html()` to read our local
   `data/raw/.../primary.html.gz`. Stage 3 is 100% local.
7. **L&M dictionary** at `data/lm_master_dictionary.csv` (86,553 words,
   already downloaded from Notre Dame SRAF). Confirmed loads cleanly
   via `python -m sibyl.lm_dictionary`.

---

## Open follow-ups / known issues

| Item | Severity | Where |
| --- | --- | --- |
| 8 dotted-share-class tickers unresolved (GEF.B, BH.A, …) — SEC uses dashes, exchanges use dots. Small fix in `edgar.load_ticker_to_cik`. | low | Stage 0 |
| 1 Stage-2 fail is Windstream Parent (CIK 2020795) — a shell holding co with a stripped 10-K. Not a bug; do not fix. | none | Stage 2 |
| Mid-word splits in some Stage 2 `full.txt` (e.g. OPKO Health "SIG NATURES"). Caused by bs4 joining text across HTML element boundaries. Stage 3 (edgartools) bypasses this since it parses structurally, not from `full.txt`. May affect Stage 4 L&M counts on a small minority of filings. **Revisit only if Stage 5 IC is borderline.** | low | Stage 2 |
| Single-process Stage 2 and Stage 3. ~30-60 min and ~10-15 min wall clock. Multiprocessing deferred unless measured too slow. | none |
| 2 expected Stage-3 over-extractions on older formats (Photronics 2016-style — extractor captures ~entire doc body). Handled: `over_extracted` status → falls back to scoring `full.txt` in Stage 4. | medium | Stage 3 |
| TOC bleed in some MD&A extractions (~200 words of page numbers at start in ~10% of filings). Cosmetic; doesn't move L&M counts. | low | Stage 3 |
| Layer 3 labelled validation set (~20 hand-labelled filings) not yet built. **Required before Stage 4 declares ready.** | required | Stage 3 |
| Export contract delivery mechanism (Phase 3) — file drop vs shared table vs Sibyl pushes — undecided. | future | Phase 3 |

---

## Sibyl ↔ Unicorn Hunt boundary

- **Universe ingress (in place):**
  `GET https://api.unicornpunk.org/api/universe` with
  `Authorization: Bearer $SIBYL_UNICORN_TOKEN`. Returns ~1,271
  small-caps, contract v1.0.
- **Unicorn server:** `unicornhunt.service` (systemd) on DigitalOcean
  droplet at `161.35.122.12`. Project lives at
  `/home/smallcap-momentum/`. Caddy terminates HTTPS via
  `api.unicornpunk.org` → uvicorn:8000.
- **Token:** the same value lives in two `.env` files:
  - On the droplet, key `SIBYL_API_TOKEN` (in
    `/home/smallcap-momentum/.env`).
  - Locally, key `SIBYL_UNICORN_TOKEN` (in
    `/Users/wessch/Projects/Projects/sibyl/.env`).
  Restart `systemctl restart unicornhunt` after rotation.
- **Signal panel egress (Phase 3, deferred):** Sibyl will emit
  versioned `(date, cik, signal_name, zscore)` panels with acceptance
  lineage. Delivery mechanism is an open decision (see Phase 3
  follow-ups).

Unicorn Hunt does **not** retain historical universe membership.
Sibyl accumulates `universe_membership` snapshots itself for
survivorship-bias defense — already happening from run one.

---

## Quick "is everything still wired up" smoke test

```bash
cd /Users/wessch/Projects/Projects/sibyl
.venv/bin/pytest -q                          # expect 51 passed
.venv/bin/sibyl status                       # see row counts + disk usage
.venv/bin/sibyl sections --cik 320193        # re-runs Apple; idempotent — 10 skipped on 2nd run
```

If `pytest` fails on `test_apple_2023_regression` with the wrong word
counts, edgartools has shifted under us — investigate before
proceeding.

---

## What I'd say if I were the prior Claude

> "Hand-label the 20 candidates in `data/clean/validation_labels.csv`
> per `docs/validation_labelling_guide.md` (~30-60 min), then
> `sibyl sections --validate`. That gate is what spec §9 calls the
> 'load-bearing checkpoint' — don't skip it. Then plan Stage 4 with
> the same per-section / per-weighting structure already prefigured
> in the DB schema (`filing_scores` table). The L&M dictionary is
> downloaded and verified; scoring is conceptually trivial. The hard
> part is over."
