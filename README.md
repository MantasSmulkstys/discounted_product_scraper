- Python 3.10+ recommended.
- PostgreSQL database.

This project scrapes discount offers from multiple Lithuanian grocery stores using config-driven selectors, then stores and normalizes the data for downstream analytics.

- `run.py` - main scraper entrypoint.
- `configs/*.json` - per-store scraping config
- `.env.example` - environment variables template.

### Viable commands

```powershell

This is for json output 

python run.py
python run.py --store maxima
python run.py --store iki norfa
python run.py --dry-run
python run.py --output db

This is the full process of scraping and normalizing data into db with AI

python run_full_pipeline.py
python run_full_pipeline.py --no-ai 
```

### Store configs:

Each store is following its JSON config file and the config files follow the same pattern / idea:
```powershell
store - store identifier
url - starting page for discounts that the scraper will get data from
type - static or dynamic (depends if javascript loads products or the products are already in page source HTML)
container - selector for the product card
selectors - what should be extracted from the product card. Example {name: .name-selector, price: .price-selector} etc.
transforms - used for text normalization like trim, collapseWhitespace, stripCurrency, commaToDecimal, firstLine, stripPrefix. Stores format text differently.
loyaltyCard - some products require loyalty card

waitFor
dismiss
scroll
                These are for behavior of the scraper because of site differences.
loadMore
pagination
viewport
```
So when adding a new store, you should ask:
- Is the page static or dynamic?
- What is the product card container that is duplicated throughout the list.
- How is the price displayed? one full text? split into integer and a fraction? stored inside an attribute?
- Does the page need scrolling, pagination or load-more button?
- Are there any banner or popups showing up when opening the page?
- If there are products that are only discounted with a loyalty card?

