# uscraper

Scrapes Lithuanian grocery promos into PostgreSQL, then normalizes them with OpenAI.

## Setup (once)

Install deps + browser, copy env, fill in `PGPASSWORD` and `OPENAI_API_KEY`.
```powershell
pip install -r requirements.txt; playwright install chromium; copy .env.example .env
```

## Commands

Scrape every store and normalize into the database (the weekly job).
```powershell
python main.py --o db
```

Same, but only one store.
```powershell
python main.py --o db --store iki
```

Scrape and normalize to a JSON file, no database.
```powershell
python main.py --o json
```

Test scrape — scrape only, no AI, no database, dumps `raw_scrape.json`.
```powershell
python main.py --o raw --store iki
```

Debug scrape — same but also lists the cards the engine skipped and why.
```powershell
python main.py --o raw --store iki --debug
```

Normalize already-scraped offers only (no scraping).
```powershell
python -m scripts.process_raw_offers_normalized --use-ai
```

Recluster families with one global AI pass — preview + write `family_vocabulary.json`.
```powershell
python -m scripts.recluster_families
```

Recluster families — actually update the database.
```powershell
python -m scripts.recluster_families --apply
```

Run the tests.
```powershell
pytest
```

Add a new store — drop `configs/<name>.json` (copy `configs/maxima.json`), then run `main.py`.
```powershell
python main.py --o raw --store <name>
```

Schedule the weekly job on Windows.
```powershell
schtasks /Create /TN "uscraper-weekly" /SC WEEKLY /D MON /ST 06:00 /TR "python C:\Users\PC\Desktop\bakis\uscraper\main.py"
```

## Layout

```
main.py           one entrypoint (--o db | json | raw)
run.py            scrape pipeline
db.py             postgres helpers
engine.py         per-config scraping
fetcher.py        static (requests) + dynamic (playwright)
transforms.py     text cleanup helpers
configs/          one JSON per store + categories.json (for --o json)
normalization/    preprocess + AI normalize (+ json_normalize for --o json)
scripts/          process_raw_offers_normalized.py (normalize), recluster_families.py (global family pass), scrape_one_store.py
tests/            pytest unit tests
```

## Env vars

| Var | Required | Default |
|---|---|---|
| `PGPASSWORD` | yes | — |
| `OPENAI_API_KEY` | yes | — |
| `PGHOST` | no | `localhost` |
| `PGPORT` | no | `5432` |
| `PGDATABASE` | no | `discount_tracker` |
| `PGUSER` | no | `postgres` |
| `NORMALIZE_BATCH_SIZE` | no | `50` |
| `PROCESSING_AGE_MINUTES` | no | `60` |
| `RECLUSTER_MODEL` | no | `gpt-4.1-mini` |
