# ReceptorAI Fetcher App

Streamlit app for collecting ReceptorAI-related literature and media signals from three workflows:

- Scientific articles from PubMed/NCBI-backed journal searches
- Review articles from PubMed/NCBI-backed review searches
- Media source articles scraped with Firecrawl

The main app entrypoint is `app.py`.

## Features

- Three app modes: `Scientific articles`, `Reviews`, and `Media sources`
- Editable source/query tables for scientific and review fetches
- Optional preprint inclusion for scientific and review modes
- CSV preview and download inside the Streamlit app
- Optional raw JSONL output for scientific and review records
- Firecrawl media scraping with source-level counts:
  - extracted article-like links
  - queued article URLs after source limits
  - articles kept after filtering

## Folder Structure

- `app.py` - Streamlit UI for all three fetcher modes
- `scientific_fetcher.py` - scientific and review article fetcher
- `Firecrawl.py` - media source scraper using the Firecrawl API
- `fetcher_utils.py` - shared fetcher utilities
- `sources.json` - Firecrawl media source configuration
- `scientific_sources.example.json` - example scientific source config
- `news_fetcher.py`, `Review_fetcher.py`, `pubmed_fetcher.py` - legacy/older scripts kept in the folder

## Setup

Install Python dependencies:

```bash
pip install streamlit pandas requests beautifulsoup4 ftfy python-dotenv firecrawl
```

Optional packages may be needed depending on your local environment and older scripts, but the active app workflow uses the packages above.

## Running the App

```bash
streamlit run app.py
```

Then choose one of:

- `Scientific articles`
- `Reviews`
- `Media sources`

## API Keys

### Scientific Articles and Reviews

These modes use PubMed/NCBI E-utilities.

Recommended:

- `NCBI_EMAIL`
- `NCBI_API_KEY` optional, but useful for higher NCBI rate limits

You can enter these in the app. You can also set them as environment variables before launching Streamlit.

### Media Sources

Media mode uses Firecrawl.

Required:

- `FIRECRAWL_API_KEY`

You can enter this in the app's `Firecrawl API key` field, or set it as an environment variable.

Do not commit API keys. If `.env.txt` contains credentials, keep it local/private.

## Scientific Articles Mode

This mode runs `scientific_fetcher.py` with `--exclude-reviews`.

It retrieves scientific journal articles from the configured sources and filters by:

- publication date range
- query terms
- abstract requirement
- preprint inclusion setting
- excluded title terms
- publication type filters

Default output:

```text
scientific_articles.csv
```

## Reviews Mode

This mode also runs `scientific_fetcher.py`, but with `--include-reviews`.

It retrieves review-style records and applies review-specific PubMed filters. If `Include preprints` is enabled, the app adds `bioRxiv` to the generated review source config and passes `--allow-preprints`.

Default output:

```text
pubmed_recent_reviews.csv
```

## Media Sources Mode

This mode runs `Firecrawl.py`.

It uses `sources.json` to find source/listing pages, discover article-like links, scrape article pages, summarize article text, filter by keywords, and write a CSV.

Default output:

```text
firecrawl_news_results.csv
```

The app displays Firecrawl stdout in an expander. That output includes per-source counts:

```text
Articles retrieved by media source:
Source    Extracted    Queued    Kept after filtering
```

## `sources.json` Format

Media mode expects a JSON list of source objects.

Required fields:

- `name`
- `url`

Optional fields:

- `allowed_domains`
- `article_url_contains`
- `exclude_url_contains`

Example:

```json
[
  {
    "name": "Fierce Biotech",
    "url": "https://www.fiercebiotech.com/biotech",
    "allowed_domains": ["fiercebiotech.com"],
    "article_url_contains": ["/biotech/"],
    "exclude_url_contains": ["/sponsored/"]
  }
]
```

## Command-Line Usage

You can also run the fetchers directly.

Scientific articles:

```bash
python scientific_fetcher.py --mindate 2026-03-01 --maxdate 2026-04-30 --target 500 --out scientific_articles.csv --exclude-reviews --require-abstract
```

Reviews:

```bash
python scientific_fetcher.py --mindate 2026-03-10 --maxdate 2026-04-30 --target 200 --out pubmed_recent_reviews.csv --include-reviews --require-abstract
```

Media sources:

```bash
set FIRECRAWL_API_KEY=your_key_here
python Firecrawl.py --sources sources.json --out firecrawl_news_results.csv --keywords "FDA, clinical trial, approval, peptide, peptides" --max-links-per-source 20
```

On macOS/Linux:

```bash
export FIRECRAWL_API_KEY=your_key_here
python Firecrawl.py --sources sources.json --out firecrawl_news_results.csv --keywords "FDA, clinical trial, approval, peptide, peptides" --max-links-per-source 20
```

## Output Files

Scientific and review outputs include fields such as:

- source name
- source type
- API used
- PMID/PMCID/DOI
- title
- journal
- publication date
- authors
- publication types
- abstract
- link
- review/preprint flags

Media outputs include:

- source
- source page
- title
- URL
- published date
- freshness status
- summary
- tags
- score
- scrape timestamp

## Troubleshooting

### NCBI warning about email

NCBI recommends sending an email address with E-utilities requests. Add `NCBI_EMAIL` in the app or environment.

### Firecrawl key missing

If media mode fails with:

```text
Missing FIRECRAWL_API_KEY
```

enter a Firecrawl API key in the app or set `FIRECRAWL_API_KEY` before running `Firecrawl.py`.

### No media articles kept

Check:

- `sources.json` source URLs are reachable
- `allowed_domains` matches the article domains
- `article_url_contains` is not too restrictive
- keywords are not too narrow
- `No article scrape` is unchecked for normal runs

### Empty or weak summaries

Media summaries are generated from scraped article body text. If `No article scrape` is enabled, article pages are not scraped, so summaries are usually empty or weak.

## Development Notes

- The active Streamlit app uses `app.py`.
- Scientific and review app modes both run `scientific_fetcher.py`.
- Media mode runs `Firecrawl.py`.
- `news_fetcher.py`, `Review_fetcher.py`, and `pubmed_fetcher.py` are legacy/older scripts and are not active top-level app modes.
- Keep API keys out of git history and avoid committing generated CSV outputs unless they are intentional examples.
