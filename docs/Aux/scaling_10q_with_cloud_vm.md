# Scaling 10-Q acquisition + parsing via a one-off cloud VM

This is the planned approach for when we expand Sibyl from 10-K only
to **10-K + 10-Q** corpus scope. Not for execution now — written down
while the design is fresh so future-us doesn't have to re-derive it.

---

## Why we need this

Adding 10-Qs multiplies the workload by ~5×:

| | 10-K only (today) | + 10-Q |
| --- | --- | --- |
| Filings | 9,144 | ~47,000 |
| Raw disk | 2.4 GB | ~8 GB |
| Clean disk | 3.1 GB | ~10 GB |
| Total footprint | ~6 GB | ~20 GB |
| Stage 1 download | 1.5 hr | ~5 hr |
| Stage 2 parse | 50 min (serial) | ~3 hr (serial) |
| Stage 3 sections | 1-1.5 hr (6 workers) | ~5 hr (6 workers) |

Local-on-Mac is **feasible** — ~8-12 hours of background work,
overnight runs. But it ties up the laptop for a long stretch and
hits thermal throttling under sustained 6-core load.

A **rented cloud VM with 32+ cores** finishes the same job in ~30 min
of compute. The cost is dominated by data transfer, not compute.

---

## Why this works without code changes

Sibyl was designed to be portable from the start. Three properties:

1. **All paths come from `config.yaml`** — no hardcoded
   `/Users/wessch` anywhere in the code.
2. **`raw/` is immutable** — you transfer it once; you never have to
   sync it back.
3. **Resumable + atomic writes** — a re-run on a different machine
   continues from where it left off.

So "scale to a cloud VM" is genuinely just: rsync up → run → rsync
down. No production code change required.

---

## The recommended approach: DigitalOcean one-off droplet

Same vendor as Unicorn — same dashboard, same SSH keys, same billing.
A separate droplet alongside `unicorn-hunt` that exists only for the
duration of the job.

### Step-by-step

**1. Create the droplet** (DigitalOcean dashboard, ~2 min)
- Type: **CPU-optimized**, 32 vCPUs (e.g. `c-32` or `c2-32`)
- OS: Ubuntu 24.04
- Region: choose one geographically close (e.g. SGP1 for Singapore)
- Add your existing SSH key

**2. Bootstrap** (~10 min, one-time per droplet)

```bash
ssh root@<droplet-ip>

apt update && apt install -y python3.13 python3.13-venv git rsync
git clone https://github.com/<your-account>/sibyl /opt/sibyl
cd /opt/sibyl
python3.13 -m venv .venv
.venv/bin/pip install -e .
mkdir -p data
```

If the repo is private, use a deploy key or `gh auth`.

**3. Copy the config + secrets** (~30 sec)

```bash
# From Mac:
scp config.yaml root@<droplet-ip>:/opt/sibyl/
scp .env root@<droplet-ip>:/opt/sibyl/
```

Edit `config.yaml` on the droplet to point `paths.data_root` at a
local path (e.g. `/opt/sibyl/data`).

**4. Transfer raw data** (~15 min on Singapore fibre)

```bash
# From Mac — push raw filings to the droplet
rsync -avz --progress data/raw/ root@<droplet-ip>:/opt/sibyl/data/raw/
rsync -avz data/sibyl.db root@<droplet-ip>:/opt/sibyl/data/
rsync -avz data/lm_master_dictionary.csv root@<droplet-ip>:/opt/sibyl/data/
```

For 10-Q work specifically, also include 10-K raw if not already
present (Sibyl uses both for yoy comparisons later).

**5. Update config for 10-Q + run the stages** (~1-2 hours total)

```bash
ssh root@<droplet-ip>
cd /opt/sibyl

# Edit config.yaml on the droplet to add 10-Q:
#   form_types: ["10-K", "10-Q"]

caffeinate_equivalent=""    # Linux doesn't need caffeinate
.venv/bin/sibyl download    # ~5 hr (SEC rate-limited)
.venv/bin/sibyl parse       # ~3 hr serial, less with multiprocessing
.venv/bin/sibyl sections    # ~30 min on 32 cores
```

(Stage 2 should also gain `--workers` for consistency before the 10-Q
job — a small follow-up.)

**6. Transfer results back to Mac** (~15-20 min)

```bash
# From Mac — pull clean text + updated DB back
rsync -avz --progress root@<droplet-ip>:/opt/sibyl/data/clean/ data/clean/
rsync -avz root@<droplet-ip>:/opt/sibyl/data/sibyl.db data/
```

You can skip pulling `raw/` back if you already have the 10-K raw
locally — the 10-Q additions live in the same directory structure.

**7. Destroy the droplet** (DigitalOcean dashboard, ~10 sec)

Just click "Destroy" on the droplet. Done. Billing stops the moment
it's destroyed.

### Cost

- DigitalOcean CPU-optimized 32-core ≈ **$0.95-1.20/hour** (verify on
  the dashboard at the time)
- Total elapsed: ~10 hours (mostly Stage 1 SEC download — rate-limit
  bound, not CPU-bound)
- Total cost: **~$10-15** for the entire 10-Q corpus pass

If you split it: Stage 1 (download) on a smaller cheaper droplet
since it's network-bound, then resize/rebuild for Stage 2+3 — you
can cut cost further. Probably not worth optimizing for a one-off.

### Speeding up future re-runs: snapshots

After step 2 (bootstrap done, code installed, deps installed), take
a **DigitalOcean snapshot** of the droplet (~5 GB image). Next time
you spin up a droplet, "Create from snapshot" → bootstrap is already
done. New droplet is ready in ~60 sec instead of 15 min.

Snapshot storage cost: ~$0.05/GB/month → ~$0.25/month for a 5 GB
image. Cheaper than re-bootstrapping every time.

---

## Alternative: serverless via Modal.com

If we end up re-running the corpus frequently (e.g. after every
edgartools or parser bump), Modal pays off:

```python
import modal

app = modal.App("sibyl-sections")
image = (
    modal.Image.debian_slim()
    .pip_install("edgartools==5.36.0", "beautifulsoup4", "lxml")
    .add_local_dir(".", remote_path="/sibyl")
)

@app.function(image=image, cpu=2)
def extract_one_filing(html_bytes: bytes, cik: int, accession: str) -> dict:
    from sibyl.sections import extract_sections_from_bytes
    return extract_sections_from_bytes(cik, accession, html_bytes)

@app.local_entrypoint()
def main():
    work = list_work_from_local_db()
    results = list(extract_one_filing.map(work))
    write_results_back(results)
```

| | Cloud VM | Modal |
| --- | --- | --- |
| Wall clock for 47k filings | ~30 min (after transfer) | ~5-10 min |
| Setup time | ~15 min (one-time) | ~1-2 hr (one-time) |
| Cost per corpus pass | ~$10-15 | ~$5-15 |
| Code changes | None | ~30-line adapter |
| Easier to reason about | ✅ | (harder; serverless mental model) |

Use the VM for the **first 10-Q pass**. If we end up re-running often,
build the Modal adapter.

---

## Out of scope (left for the actual 10-Q implementation)

The cloud-VM approach is the *transport mechanism*. The actual 10-Q
expansion will also require:

- **Threshold recalibration** in `sibyl/sections.py`. 10-Q risk-factor
  sections are typically "material changes only" — usually <500 words.
  The current `length_low = 5000` and `MIN_OK_WORDS = 1000` thresholds
  would mark almost every 10-Q as `incorp_ref` or worse.
- **Form-aware yoy pairing.** Lazy Prices says: 10-K↔10-K, Q2↔Q2.
  Sibyl's `filing_signals` table is form-agnostic today; needs the
  pairing logic in Stage 5 to filter by `form_type`.
- **Separate labelled validation set** for 10-Qs in Layer 3.
- **`sibyl parse --workers` flag** (same change as Stage 3 got — a
  small follow-up before scaling).
- **Edgartools' `TenQ` class** is the analog of `TenK` for quarterly
  filings. API is the same shape; outputs need separate sanity-check
  thresholds.

When ready, this doc becomes the deployment runbook for the cloud
side; a separate plan covers the Sibyl-side changes.
