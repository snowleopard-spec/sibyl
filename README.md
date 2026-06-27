# Sibyl — S&P 500 Sentiment Monitor

Sibyl is a self-contained S&P 500 sentiment monitor. For each constituent,
it scores the MD&A and Risk Factors sections of the last four 10-Q filings
with the Loughran-McDonald (LM) negative-words dictionary, ranks
constituents into deciles, and serves the results via a local Flask page.

See `SIBYL_SENTIMENT_SPEC.md` for the full build spec.

## Quickstart

```bash
# 1. Install
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

# 2. Configure
cp config.example.yaml config.yaml
# edit sec.user_agent to your "Name email@host" form

# 3. (Optional) carry an existing corpus across instead of re-pulling
# cp -r /path/to/research-tool/data ./data

# 4. Refresh membership + score any new filings (10-Q only)
sibyl refresh

# 5. Serve
sibyl serve
# Open http://localhost:5000
```

## CLI

| Command | Description |
| --- | --- |
| `sibyl refresh` | Full pipeline: membership → download → parse → sections → score (10-Q only). Writes `data/last_refresh.txt`. |
| `sibyl rank` | Print decile-ranked tickers as a terminal table. |
| `sibyl serve` | Start the local Flask report at `http://localhost:5000`. |
| `sibyl status` | DB row counts + disk usage. |
| `sibyl download` / `parse` / `sections` / `score` | Sub-steps of `refresh`; useful for debugging. |

## Loughran-McDonald dictionary

Download `Loughran-McDonald_MasterDictionary_1993-2025.csv` from
<https://sraf.nd.edu/loughranmcdonald-master-dictionary/> and save it to
`data/lm_master_dictionary.csv`. Then sanity-check it:

```bash
python -m sibyl.lm_dictionary
```

This prints the row count and per-category word counts.

## Tests

```bash
pytest
```

No tests hit the network; fixtures cover the EDGAR + Wikipedia paths.
