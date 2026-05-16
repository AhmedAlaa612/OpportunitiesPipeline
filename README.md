# Opportunities Ingestion Pipeline

Scrapes → Extracts → Embeds opportunities into Supabase (PostgreSQL) and Qdrant.

## Structure

```
pipeline/
├── run_pipeline.py          # Orchestrator — run this
├── config.py                # Shared env-based configuration
├── extract.py               # Step 2: LLM extraction + translation + DB insert
├── embed.py                 # Step 3: Jina embeddings → Qdrant upsert
├── countries.py             # Country name normalization
│
├── helpers/
│   ├── db.py                # get_last_scraped_date(), DB connection
│   ├── html.py              # html_to_clean_md(), sanitize_filename()
│   ├── markdown.py          # build_enriched_markdown()
│   └── scraper_base.py      # BaseScraper abstract class
│
└── scrapers/
    ├── opportunitiescorners.py   # opportunitiescorners.com scraper
    └── example_new_scraper.py    # template for adding new sources
```

## Usage

```bash
# Full pipeline
python run_pipeline.py

# Individual steps
python run_pipeline.py scrape
python run_pipeline.py extract
python run_pipeline.py embed

# Any combination
python run_pipeline.py scrape embed
```

## Adding a New Scraper

1. Copy `scrapers/example_new_scraper.py` to `scrapers/mysource.py`
2. Set `source_name`, `base_url`, and optionally `exclude_domains`
3. Implement `fetch_opportunity_list()` — returns list of `{title, link, datetime, date_text}`
4. Implement `scrape_opportunity_page()` — returns enriched Markdown string or `None`
5. Register in `run_pipeline.py`:
   ```python
   from scrapers.mysource import MySourceScraper
   SCRAPERS = [
       OpportunitiesCornersScraper(),
       MySourceScraper(),
   ]
   ```

Each scraper tracks its own last-scraped date in the DB (by `source` column),
so adding a new source won't re-scrape existing ones.

## Environment Variables

```
DB_HOST, DB_PORT, DB_NAME, DB_USER, DB_PASSWORD
GROQ_API_KEY
CEREBRAS_API_KEY
JINA_API_KEY
QDRANT_ENDPOINT
QDRANT_API_KEY
```
