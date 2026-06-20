# Droplet Backfill Runbook

*Use the droplet for the long I/O-bound download phase; do everything CPU-bound on the Mac.*

The initial S&P 500 backfill is ~503 names × ~25 filings (10-K + 5y of 10-Q) ≈ 12,500 filings. At SEC's 8 req/s rate limit that's ~3-4h of HTTP-bound work, plus parsing/sections/scoring/diffing afterwards. This runbook offloads only the download to the droplet (unattended in `tmux`) and runs the rest on the Mac (faster, multi-core).

| Phase | Where | Wall clock | Why |
|---|---|---|---|
| Download (HTTP) | Droplet | ~3-4h | Rate-limited; runs unattended in tmux |
| rsync data back | Mac | ~10-20 min | ~6 GB over home broadband |
| Parse + sections + score + diff + aggregate | Mac | ~30-60 min | CPU-bound; Mac's M-series multi-core is much faster than a 1-CPU droplet |

---

## 0. Prerequisites

- SSH access to `wessch@161.35.122.12` (existing Unicorn droplet)
- Local Sibyl repo on the `research-tool` branch, tests passing
- ≥10 GB free on the droplet (`df -h ~`) — the backfill needs ~6 GB
- Python 3.11+ on the droplet (`python3 --version`)
- `tmux` on the droplet (`which tmux`)

---

## 1. One-time droplet setup (~5 min)

```bash
ssh wessch@161.35.122.12

# Pre-flight: ensure no resource issues
df -h ~                                  # >10 GB free
free -h                                  # plenty of RAM (download barely uses any)
systemctl status unicornhunt 2>/dev/null | head -3   # ensure Unicorn still healthy

git clone https://github.com/snowleopard-spec/sibyl.git
cd sibyl
git checkout research-tool

python3 -m venv .venv
.venv/bin/pip install -e .               # pulls everything from pyproject.toml

cp config.example.yaml config.yaml
# Now edit config.yaml: set sec.user_agent to your email. SEC requires a real contact.
# e.g.: user_agent: "Sibyl research wes.hunt1@outlook.com"
nano config.yaml
```

**Verify the install works**:
```bash
.venv/bin/sibyl --help                   # 9 subcommands listed
.venv/bin/pytest -q                      # 151 passed (optional)
```

---

## 2. Smoke-test the deploy on a small subset (~10 min)

Before committing to a 4-hour pull, run the existing 20-name smoke test on the droplet. This validates the full pipeline end-to-end and catches any environment surprises early.

```bash
.venv/bin/python scripts/smoke_sp500.py
```

Expected outcome: completes in ~7 min, ~215 filings downloaded, ~200 parsed, chart written to `data/queried/AAPL/chart_AAPL_*.png`. If it crashes — fix that first (probably a missing dep or a config issue) before proceeding to the real run.

---

## 3. Run the real backfill (download only, in tmux, ~3-4h)

```bash
tmux new -s sibyl-download
.venv/bin/sibyl sp500 refresh --no-download    # ~5s — pulls Wikipedia membership (503 names)
.venv/bin/sibyl download --stack sp500         # ~3-4h — pulls all 10-K + 10-Q in cfg's window
```

Detach with `Ctrl-b d`. Reattach later with `tmux a -t sibyl-download`.

**Check progress mid-run from another SSH session:**
```bash
# Filings downloaded so far
find ~/sibyl/data/sp500/raw -name primary.html.gz | wc -l

# Most recent log line
tail -1 ~/sibyl/data/logs/sibyl_*.log

# Disk usage
du -sh ~/sibyl/data/
```

When it finishes, you'll see (in tmux):
```
CIKs processed: 503
New filings:    ~12500
Skipped:        0
Failed:         <some small number>
```

A handful of failures (1-2%) is normal — usually older filings with HTTP errors or SEC quirks. They're skipped, not retried; the rest of the pipeline handles missing filings gracefully.

---

## 4. Rsync the data back to your Mac (~10-20 min)

From the Mac:
```bash
cd ~/Projects/Projects/sibyl

# DB (small but essential — has membership + filings rows)
rsync -avzh --progress wessch@161.35.122.12:~/sibyl/data/sibyl.db ./data/

# SEC ticker cache (small — saves a refresh on Mac later)
rsync -avzh --progress wessch@161.35.122.12:~/sibyl/data/company_tickers.json ./data/

# The actual filings — biggest chunk, ~5-6 GB
rsync -avzh --progress wessch@161.35.122.12:~/sibyl/data/sp500/ ./data/sp500/
```

Why this works without folder-structure surgery: `filings.raw_path` is stored **relative** to `data_root` (e.g. `sp500/raw/320193/<accession>/primary.html.gz`), so the DB rows resolve correctly on both machines.

**Sanity check after rsync:**
```bash
.venv/bin/sibyl status                   # filings count should match droplet's
.venv/bin/sibyl sp500 status             # 503 members
find data/sp500/raw -name primary.html.gz | wc -l   # matches DB count
```

---

## 5. Run the remaining stages on the Mac (~30-60 min)

Easiest: one command does it all (download is a no-op since files are cached):
```bash
.venv/bin/sibyl sp500 refresh
```

Or run the stages individually if you want progress visibility per stage:
```bash
.venv/bin/sibyl parse --stack sp500       # ~5-10 min
.venv/bin/sibyl sections --stack sp500    # ~10-20 min (parallel via ProcessPoolExecutor)
.venv/bin/sibyl score --stack sp500       # ~5 min
.venv/bin/sibyl diff --stack sp500        # ~5 min
.venv/bin/sibyl sp500 refresh --no-download   # rebuilds sp500_aggregates
```

**Verify end-to-end works:**
```bash
.venv/bin/sibyl research AAPL
# Should print stack=sp500, sector=Information Technology, filings=~20,
# and write data/queried/AAPL/chart_AAPL_<stamp>.png
open data/queried/AAPL/chart_AAPL_*.png   # eyeball the chart
```

---

## 6. After the backfill — droplet cleanup

The droplet's data is now redundant with your Mac. Options:

**Option A — leave it as a backup** (recommended if disk is fine):
```bash
ssh wessch@161.35.122.12
du -sh ~/sibyl/data/                     # confirm it's still ~6 GB
df -h ~                                  # confirm there's headroom
```
No action needed. If you ever lose the Mac copy, rsync it back.

**Option B — free the disk**:
```bash
ssh wessch@161.35.122.12
rm -rf ~/sibyl/data/sp500/raw            # 5-6 GB freed
# Keep data/sibyl.db, ~/sibyl source — tiny
```

**Option C — full tear-down**:
```bash
ssh wessch@161.35.122.12
rm -rf ~/sibyl                            # gone
```

The download work isn't lost in any case — the Mac copy is canonical going forward. Future incremental refreshes (`sibyl sp500 refresh`) on the Mac are fast since most filings are already cached.

---

## 7. Troubleshooting

### tmux session died

Re-attach with `tmux a -t sibyl-download`. If the session is gone but the download was partway through, just re-run — `download_all` is resumable. It checks `is_filing_complete(conn, raw_root, cik, accession)` for each filing and skips ones already on disk.

```bash
tmux new -s sibyl-download
.venv/bin/sibyl download --stack sp500   # picks up where it left off
```

### Out of disk on droplet

```bash
df -h ~
# If <500 MB free:
du -sh ~/sibyl/data/sp500/raw/* | sort -h | tail   # find the biggest CIKs
```

If you genuinely need more, resize the droplet via the DigitalOcean dashboard (adds a volume; no data loss).

### SEC HTTP errors

A small number is expected (network blips, SEC's own 503s under load). They show up in the log as `WARNING sibyl.download:Filing download failed...`. Re-run `sibyl download --stack sp500` — failures aren't retried inline but the next run picks them up.

If MANY filings fail (e.g. >5%), it might be:
- `cfg.sec.rate_limit_per_sec` set too high (must be ≤10 per SEC's published limit; default 8 is fine)
- `cfg.sec.user_agent` missing or doesn't include an email — SEC requires a real contact, otherwise they'll throttle or block

### Mac sibyl can't find the rsync'd filings

Symptom: `sibyl parse --stack sp500` says "Parsed: 0" despite the DB having rows.

Likely cause: the rsync didn't preserve the path. Check:
```bash
ls data/sp500/raw/320193/ | head -3       # should show accession folders
```
If missing, re-run the rsync (the `-a` flag preserves structure).

### Unicorn affected by the download

Sibyl's download is HTTP-bound (waiting on SEC) and barely uses CPU/RAM, so it shouldn't interfere with `unicornhunt.service`. If you observe latency in Unicorn during the backfill, pause sibyl (`tmux a -t sibyl-download` then Ctrl-c) and re-run later off-peak.

---

## 8. Summary checklist

- [ ] SSH + clone + venv + `pip install -e .` on droplet
- [ ] Edit `config.yaml` (sec.user_agent)
- [ ] Smoke test: `scripts/smoke_sp500.py`
- [ ] `tmux new -s sibyl-download`
- [ ] `sibyl sp500 refresh --no-download` + `sibyl download --stack sp500`
- [ ] Wait ~3-4h
- [ ] rsync `sibyl.db` + `company_tickers.json` + `data/sp500/` back to Mac
- [ ] `sibyl sp500 refresh` on Mac (parses + scores + diffs + aggregates)
- [ ] `sibyl research AAPL` — eyeball the chart
- [ ] Decide droplet cleanup (leave / free disk / full tear-down)
