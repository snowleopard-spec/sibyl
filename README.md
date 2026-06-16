# Sibyl

SEC filing signal research engine. See `SIBYL BUILD SPEC.md` for the full
architecture and `SIBYL_HANDOFF.md` for the Unicorn Hunt `/api/universe`
contract.

This README covers the scaffolding + Stage 0 (`sibyl universe`) milestone.
Later stages (download, parse, score, signal layer, evaluation engine,
export) are stubbed.

## Setup

```bash
# from the repo root
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
```

## Configure

1. Copy the example config and edit the Unicorn host + SEC User-Agent:

    ```bash
    cp config.example.yaml config.yaml
    ```

    - `unicorn.base_url`: the Unicorn Hunt droplet URL (HTTPS).
    - `sec.user_agent`: required by SEC on every request (see
      `SIBYL BUILD SPEC.md` §7), in the form `"Name contact@email.com"`.

2. Generate the Unicorn API token once and install it in two places
   (`SIBYL_HANDOFF.md` §3):

    ```bash
    python -c "import secrets; print(secrets.token_urlsafe(32))"
    ```

    - Set `SIBYL_API_TOKEN=<value>` on the Unicorn droplet's env and
      restart its FastAPI app.
    - Create `.env` in this repo with `SIBYL_UNICORN_TOKEN=<same-value>`.

## L&M dictionary (Stage 4 input, sanity-checked now)

Download `Loughran-McDonald_MasterDictionary_1993-2025.csv` from
<https://sraf.nd.edu/loughranmcdonald-master-dictionary/> and save it to
`data/lm_master_dictionary.csv`. Then:

```bash
python -m sibyl.lm_dictionary
```

This prints the row count and per-category word counts — a five-minute
de-risk that confirms the file is well-formed before any later stage
needs it.

## Run Stage 0

```bash
sibyl universe
```

What it does:
1. GET `<base_url>/api/universe` with bearer auth.
2. Validate `contract_version`; warn loudly on mismatch.
3. Snapshot the response verbatim to `data/universe_snapshots/`.
4. Upsert into `universe_membership` (survivorship-bias defense).
5. Cache SEC's `company_tickers.json` and resolve ticker → CIK.
6. Log unresolved tickers (delistings, foreign listings, etc).

Re-running on the same day is idempotent.

## CLI

| Command | Status |
| --- | --- |
| `sibyl universe` | implemented |
| `sibyl status` | implemented (counts + disk usage) |
| `sibyl download` | stub |
| `sibyl parse` | stub |
| `sibyl score` | stub |
| `sibyl diff` | stub |
| `sibyl prices` | stub |
| `sibyl panel` | stub |
| `sibyl eval` | stub |
| `sibyl export` | stub |

## Tests

```bash
pytest
```

No tests hit the network; the universe test uses a recorded JSON fixture.
