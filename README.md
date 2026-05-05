# uscraper

Scrapes Lithuanian grocery promos into PostgreSQL, then normalizes with OpenAI.

## Setup (once)

```powershell
pip install -r requirements.txt
playwright install chromium
copy .env.example .env
```

Edit `.env`. You need: `PGPASSWORD`, `OPENAI_API_KEY`.

## Run

```powershell
python main.py
```

That scrapes every store in `configs/`, writes to `raw_offers`, then normalizes everything pending. Run it once a week.

## Test

```powershell
pytest
```

## Debug a single store

When a store starts returning 0 or fewer products than expected (usually because the site changed its markup):

```powershell
python debug_store.py rimi
```

Writes `debug_rimi.json` with one entry per card found by the container selector, including extraction result and `_skipped_reason` + `_card_html_excerpt` for invalid ones. Open the JSON, find what changed, fix `configs/<store>.json`, re-run.

## Schedule weekly (Windows)

```powershell
schtasks /Create /TN "uscraper-weekly" /SC WEEKLY /D MON /ST 06:00 /TR "python C:\Users\PC\Desktop\bakis\uscraper\main.py"
```

## Add a new store

1. Drop a `configs/<name>.json` next to the existing ones.
2. Run `python main.py`.

Config fields: `store`, `url`, `type` (`static` or `dynamic`), `container` (CSS selector for one product card), `selectors` (per-field CSS). See `configs/maxima.json` for a full example.

## Layout

```
main.py           one entrypoint
run.py            scrape pipeline
db.py             postgres helpers
engine.py         per-config scraping
fetcher.py        static (requests) + dynamic (playwright)
transforms.py     text cleanup helpers
configs/          one JSON per store
normalization/    preprocess + AI normalize
scripts/          process_raw_offers_normalized.py (called by main.py)
tests/            pytest unit tests
```

## Env vars

| Var | Required | Default |
|---|---|---|
| `PGHOST` | no | `localhost` |
| `PGPORT` | no | `5432` |
| `PGDATABASE` | no | `discount_tracker` |
| `PGUSER` | no | `postgres` |
| `PGPASSWORD` | yes | — |
| `OPENAI_API_KEY` | yes | — |
| `NORMALIZE_BATCH_SIZE` | no | `50` |
| `PROCESSING_AGE_MINUTES` | no | `60` |
