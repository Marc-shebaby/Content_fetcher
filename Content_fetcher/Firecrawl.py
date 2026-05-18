from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

import pandas as pd
from bs4 import BeautifulSoup
from firecrawl import Firecrawl

try:
    from ftfy import fix_text
except ImportError:
    fix_text = None


DEFAULT_SOURCES_FILE = "sources.json"
DEFAULT_OUT = "firecrawl_news_resultss.csv"

DEFAULT_KEYWORDS = [
    "FDA",
    "clinical trial",
    "approval",
    "drug discovery",
    "biotech",
    "pharma",
    "peptide",
    "peptides",
    "AI",
    "machine learning",
    "partnership",
    "collaboration",
    "licensing",
    "acquisition",
]

ARTICLE_FIELDS = [
    "source",
    "source_page",
    "title",
    "url",
    "published",
    "freshness_status",
    "summary",
    "tags",
    "score",
    "scraped_at",
]

TOPIC_RULES = {
    "clinical_trial": [
        "phase 1",
        "phase i",
        "phase 2",
        "phase ii",
        "phase 3",
        "phase iii",
        "clinical trial",
        "trial data",
        "trial results",
        "readout",
    ],
    "regulatory": [
        "fda",
        "ema",
        "approval",
        "approved",
        "clearance",
        "cleared",
        "pdufa",
        "complete response letter",
        "crl",
    ],
    "deals": [
        "partnership",
        "collaboration",
        "licensing",
        "license",
        "acquisition",
        "merger",
        "buyout",
        "deal",
    ],
    "ai_drug_discovery": [
        "ai",
        "artificial intelligence",
        "machine learning",
        "computational",
        "drug discovery",
        "in silico",
    ],
    "peptides_macrocycles": [
        "peptide",
        "peptides",
        "macrocycle",
        "macrocycles",
        "cyclic peptide",
    ],
    "manufacturing": [
        "manufacturing",
        "capacity",
        "facility",
        "plant",
        "supply",
        "cdmo",
    ],
    "financing": [
        "ipo",
        "series a",
        "series b",
        "financing",
        "funding",
        "raises",
        "venture",
    ],
}

TAG_WEIGHTS = {
    "ai_drug_discovery": 3,
    "peptides_macrocycles": 3,
    "clinical_trial": 2,
    "regulatory": 2,
    "deals": 2,
    "manufacturing": 1,
    "financing": 1,
}

NON_ARTICLE_URL_FRAGMENTS = [
    "/api/auth/",
    "/auth/",
    "/login",
    "/logout",
    "/register",
    "/signup",
    "/subscribe",
    "/category/",
    "/tag/",
    "/author/",
    "/page/",
]

NON_ARTICLE_TITLE_PATTERNS = [
    "subscribe",
    "log in",
    "login",
    "sign up",
    "register",
    "privacy policy",
    "terms of use",
]


@dataclass(frozen=True)
class SourceConfig:
    name: str
    url: str
    allowed_domains: list[str]
    article_url_contains: list[str]
    exclude_url_contains: list[str]


def load_sources(path: str) -> list[SourceConfig]:
    source_path = Path(path)

    if not source_path.exists():
        raise FileNotFoundError(
            f"Missing sources file: {source_path}. "
            "Create sources.json or pass --sources path/to/sources.json."
        )

    with source_path.open("r", encoding="utf-8") as f:
        raw_sources = json.load(f)

    if not isinstance(raw_sources, list):
        raise ValueError("sources.json must contain a list of source objects.")

    sources: list[SourceConfig] = []

    for i, item in enumerate(raw_sources, start=1):
        if not isinstance(item, dict):
            raise ValueError(f"Source #{i} must be an object.")

        if not item.get("name") or not item.get("url"):
            raise ValueError(f"Source #{i} must contain at least 'name' and 'url'.")

        sources.append(
            SourceConfig(
                name=str(item["name"]),
                url=ensure_absolute_url(str(item["url"])),
                allowed_domains=[d.lower() for d in item.get("allowed_domains", [])],
                article_url_contains=item.get("article_url_contains", []),
                exclude_url_contains=item.get("exclude_url_contains", []),
            )
        )

    return sources


def ensure_absolute_url(url: str) -> str:
    url = url.strip()

    if not url:
        return url

    parsed = urlparse(url)

    if parsed.scheme in {"http", "https"}:
        return url

    return "https://" + url.lstrip("/")


def normalize_url(url: str) -> str:
    url = ensure_absolute_url(url)
    parsed = urlparse(url)
    normalized = parsed._replace(fragment="").geturl()
    return normalized.rstrip("/")


def ordered_unique(values: list[str]) -> list[str]:
    seen: set[str] = set()
    unique_values: list[str] = []

    for value in values:
        if value in seen:
            continue

        seen.add(value)
        unique_values.append(value)

    return unique_values


def clean_text(value: str | None) -> str:
    if not value:
        return ""

    soup = BeautifulSoup(value, "html.parser")
    text = soup.get_text(separator=" ")
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def get_document_field(doc: Any, field: str, default: Any = "") -> Any:
    if isinstance(doc, dict):
        return doc.get(field, default)

    return getattr(doc, field, default)


def get_metadata(doc: Any) -> dict[str, Any]:
    metadata = get_document_field(doc, "metadata", {}) or {}

    if isinstance(metadata, dict):
        return metadata

    if hasattr(metadata, "model_dump"):
        return metadata.model_dump()

    if hasattr(metadata, "__dict__"):
        return vars(metadata)

    return {}


def source_url_from_doc(doc: Any, fallback_url: str) -> str:
    metadata = get_metadata(doc)

    value = (
        metadata.get("sourceURL")
        or metadata.get("source_url")
        or metadata.get("url")
        or get_document_field(doc, "url", "")
        or fallback_url
    )

    return normalize_url(str(value))


def domain_allowed(url: str, allowed_domains: list[str]) -> bool:
    if not allowed_domains:
        return True

    parsed = urlparse(ensure_absolute_url(url))
    netloc = parsed.netloc.lower()

    return any(netloc == domain or netloc.endswith("." + domain) for domain in allowed_domains)


def looks_like_article(url: str, source: SourceConfig) -> bool:
    normalized = normalize_url(url)
    parsed = urlparse(normalized)
    path = parsed.path.lower()

    if not domain_allowed(normalized, source.allowed_domains):
        return False

    if is_source_listing_page(normalized, source):
        return False

    if any(fragment in path for fragment in NON_ARTICLE_URL_FRAGMENTS):
        return False

    if any(fragment and fragment in normalized for fragment in source.exclude_url_contains):
        return False

    if not source.article_url_contains:
        return True

    return any(fragment and fragment in normalized for fragment in source.article_url_contains)


def is_source_listing_page(url: str, source: SourceConfig) -> bool:
    parsed = urlparse(normalize_url(url))
    source_parsed = urlparse(normalize_url(source.url))

    if parsed.netloc.lower() != source_parsed.netloc.lower():
        return False

    normalized_path = parsed.path.rstrip("/").lower()
    source_path = source_parsed.path.rstrip("/").lower()

    if normalized_path != source_path:
        return False

    query = parsed.query.lower()

    return not query or re.fullmatch(r"page=\d+", query) is not None


def extract_links_from_markdown(markdown: str, base_url: str) -> list[str]:
    links: list[str] = []

    # Markdown links: [label](url)
    for match in re.finditer(r"\[[^\]]+\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)", markdown or ""):
        href = match.group(1).strip()
        if href:
            links.append(normalize_url(urljoin(base_url, href)))

    # Plain absolute URLs
    for match in re.finditer(r"https?://[^\s)>\"']+", markdown or ""):
        href = match.group(0).strip()
        if href:
            links.append(normalize_url(href))

    return ordered_unique(links)


def extract_links_from_html(html: str, base_url: str) -> list[str]:
    links: list[str] = []
    soup = BeautifulSoup(html or "", "html.parser")

    for tag in soup.find_all("a", href=True):
        href = str(tag["href"]).strip()

        if not href:
            continue

        if href.startswith(("mailto:", "tel:", "javascript:")):
            continue

        links.append(normalize_url(urljoin(base_url, href)))

    return ordered_unique(links)


def scrape_page(
    app: Firecrawl,
    url: str,
    *,
    max_retries: int = 2,
    sleep_seconds: float = 1.5,
) -> dict[str, Any]:
    normalized_url = normalize_url(url)
    last_error: Exception | None = None

    for attempt in range(max_retries + 1):
        try:
            doc = app.scrape(
                normalized_url,
                formats=["markdown", "html"],
            )

            markdown = get_document_field(doc, "markdown", "") or ""
            html = get_document_field(doc, "html", "") or ""
            metadata = get_metadata(doc)

            return {
                "url": source_url_from_doc(doc, normalized_url),
                "markdown": markdown,
                "html": html,
                "metadata": metadata,
            }

        except Exception as exc:
            last_error = exc

            if attempt < max_retries:
                time.sleep(sleep_seconds * (attempt + 1))

    raise RuntimeError(f"Failed to scrape {normalized_url}: {last_error}") from last_error


def discover_article_urls(
    app: Firecrawl,
    source: SourceConfig,
    *,
    max_links_per_source: int,
) -> tuple[list[str], int]:
    print(f"Scraping source page: {source.name} - {source.url}")

    page = scrape_page(app, source.url)

    markdown_links = extract_links_from_markdown(page["markdown"], source.url)
    html_links = extract_links_from_html(page["html"], source.url)

    all_links = ordered_unique(markdown_links + html_links)
    article_links = [url for url in all_links if looks_like_article(url, source)]

    print(f"  found {len(article_links)} article-like links")

    return article_links[:max_links_per_source], len(article_links)


def extract_title(article: dict[str, Any]) -> str:
    metadata = article.get("metadata", {}) or {}

    for key in ["title", "ogTitle", "og:title", "twitter:title"]:
        value = metadata.get(key)
        if value:
            return clean_text(str(value))

    markdown = article.get("markdown", "") or ""
    first_heading = re.search(r"^#\s+(.+)$", markdown, flags=re.MULTILINE)

    if first_heading:
        return clean_text(first_heading.group(1))

    text = clean_text(markdown or article.get("html", ""))
    return text[:160]


def extract_published(article: dict[str, Any]) -> str:
    metadata = article.get("metadata", {}) or {}

    candidate_keys = [
        "publishedTime",
        "article:published_time",
        "datePublished",
        "published",
        "date",
        "modifiedTime",
        "article:modified_time",
        "og:updated_time",
    ]

    for key in candidate_keys:
        value = metadata.get(key)
        if value:
            return str(value)

    return ""


def parse_date_value(value: str | None) -> datetime | None:
    if not value:
        return None

    text = clean_text(str(value)).strip()

    if not text:
        return None

    iso_text = text.replace("Z", "+00:00")

    try:
        parsed = datetime.fromisoformat(iso_text)
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except ValueError:
        pass

    date_formats = [
        "%B %d, %Y",
        "%b %d, %Y",
        "%d %B %Y",
        "%d %b %Y",
        "%Y %B %d",
        "%Y %b %d",
        "%Y-%m-%d",
        "%m/%d/%Y",
        "%d/%m/%Y",
    ]

    for date_format in date_formats:
        try:
            return datetime.strptime(text, date_format).replace(tzinfo=timezone.utc)
        except ValueError:
            continue

    return None


def extract_visible_date_text(text: str) -> str:
    cleaned = clean_text(text)

    date_patterns = [
        r"\b[A-Z][a-z]{2,8}\s+\d{1,2},\s+\d{4}\b",
        r"\b\d{1,2}\s+[A-Z][a-z]{2,8}\s+\d{4}\b",
        r"\b\d{4}\s+[A-Z][a-z]{2,8}\s+\d{1,2}\b",
        r"\b\d{4}-\d{2}-\d{2}(?:T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})?)?\b",
        r"\b\d{1,2}/\d{1,2}/\d{4}\b",
    ]

    for pattern in date_patterns:
        match = re.search(pattern, cleaned)
        if match:
            return match.group(0)

    return ""


def extract_publication_datetime(article: dict[str, Any], body: str) -> tuple[str, datetime | None]:
    published = extract_published(article)
    parsed = parse_date_value(published)

    if parsed:
        return published, parsed

    visible_date = extract_visible_date_text(body)
    parsed = parse_date_value(visible_date)

    if parsed:
        return visible_date, parsed

    return published, None


def current_and_previous_months(now: datetime) -> set[tuple[int, int]]:
    current = (now.year, now.month)

    if now.month == 1:
        previous = (now.year - 1, 12)
    else:
        previous = (now.year, now.month - 1)

    return {current, previous}


def classify_freshness(published_at: datetime | None, now: datetime) -> str:
    if published_at is None:
        return "undated"

    article_month = (published_at.year, published_at.month)

    if article_month in current_and_previous_months(now):
        return "recent"

    return "old"


def is_obvious_non_article(title: str, url: str) -> bool:
    title_lower = title.lower().strip()
    parsed = urlparse(normalize_url(url))
    path = parsed.path.lower()

    if any(fragment in path for fragment in NON_ARTICLE_URL_FRAGMENTS):
        return True

    return any(pattern == title_lower or pattern in title_lower for pattern in NON_ARTICLE_TITLE_PATTERNS)


def fix_encoding_artifacts(text: str) -> str:
    replacements = {
        "â€™": "’",
        "â€˜": "‘",
        "â€œ": "“",
        "â€": "”",
        "â€“": "–",
        "â€”": "—",
        "Â": "",
    }

    for bad, good in replacements.items():
        text = text.replace(bad, good)

    return text


def clean_summary_text(value: Any) -> Any:
    """Clean one summary value using the same mojibake fixes as Clean_summary.py."""
    if pd.isna(value):
        return value

    text = str(value)

    if fix_text is not None:
        text = fix_text(text)

    text = fix_encoding_artifacts(text)
    text = text.replace("\u00ad", "")
    text = text.replace("\xa0", " ")
    text = re.sub(r"\s+", " ", text).strip()

    return text


def clean_summary_column_in_csv(csv_path: str | Path, column: str = "summary") -> None:
    """Clean the summary column in a generated CSV file in place."""
    path = Path(csv_path)
    df = pd.read_csv(path)

    if column not in df.columns:
        raise ValueError(f"Column '{column}' not found. Available columns: {list(df.columns)}")

    df[column] = df[column].apply(clean_summary_text)
    df.to_csv(path, index=False, encoding="utf-8", quoting=csv.QUOTE_MINIMAL)

    print(f"Cleaned '{column}' column in {path}")


def make_summary(markdown_or_html: str, max_chars: int = 1200) -> str:
    if not markdown_or_html:
        return ""

    text = markdown_or_html

    # Remove Markdown images completely: ![](url) or ![alt](url)
    text = re.sub(r"!\[[^\]]*\]\([^)]+\)", " ", text)

    # Convert Markdown links to just their visible text: [text](url) -> text
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)

    # Remove bare URLs
    text = re.sub(r"https?://\S+", " ", text)

    # Remove Markdown heading/bold/list formatting but keep the words
    text = re.sub(r"(^|\s)#{1,6}\s*", " ", text)
    text = text.replace("**", "").replace("__", "")
    text = re.sub(r"\\-", "-", text)

    # Convert any remaining HTML to text and normalize whitespace
    text = clean_text(text)
    text = clean_summary_text(text)

    # Remove obvious low-value fragments
    boilerplate = [
        "skip to main content",
        "advertisement",
        "privacy policy",
        "terms of use",
        "cookie",
        "cookies",
        "accept all",
        "manage preferences",
        "sign up",
        "subscribe",
        "newsletter",
        "linkedin",
        "facebook",
        "twitter",
        "share this article",
        "all rights reserved",
    ]

    sentences = re.split(r"(?<=[.!?])\s+", text)
    kept = []

    for sentence in sentences:
        lower = sentence.lower()

        if any(bad in lower for bad in boilerplate):
            continue

        if len(sentence.strip()) < 25:
            continue

        kept.append(sentence.strip())

        if len(" ".join(kept)) >= max_chars:
            break

    summary = " ".join(kept) if kept else text

    if len(summary) <= max_chars:
        return summary

    return summary[:max_chars].rsplit(" ", 1)[0] + "..."


def parse_keywords(value: str) -> list[str]:
    if not value.strip():
        return []

    return [item.strip() for item in value.split(",") if item.strip()]


def matches_keywords(
    title: str,
    summary: str,
    body: str,
    keywords: list[str],
    *,
    search_full_body: bool = True,
) -> bool:
    if not keywords:
        return True

    if search_full_body:
        text = f"{title} {summary} {body}".lower()
    else:
        text = f"{title} {summary}".lower()

    return any(keyword.lower() in text for keyword in keywords)


def assign_tags(title: str, summary: str, body: str) -> list[str]:
    text = f"{title} {summary} {body}".lower()
    tags: list[str] = []

    for tag, keywords in TOPIC_RULES.items():
        if any(keyword.lower() in text for keyword in keywords):
            tags.append(tag)

    return tags


def score_tags(tags: list[str]) -> int:
    return min(10, sum(TAG_WEIGHTS.get(tag, 0) for tag in tags))


def content_hash(url: str) -> str:
    return hashlib.sha256(normalize_url(url).encode("utf-8")).hexdigest()


def dedupe_articles(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    deduped: list[dict[str, Any]] = []

    for row in rows:
        key = content_hash(str(row["url"]))

        if key in seen:
            continue

        seen.add(key)
        deduped.append(row)

    return deduped


def initial_source_counts(sources: list[SourceConfig]) -> dict[str, dict[str, int]]:
    return {
        source.name: {
            "extracted": 0,
            "queued": 0,
            "kept_after_filtering": 0,
        }
        for source in sources
    }


def update_kept_counts(
    source_counts: dict[str, dict[str, int]],
    rows: list[dict[str, Any]],
) -> None:
    for counts in source_counts.values():
        counts["kept_after_filtering"] = 0

    for row in rows:
        source_name = str(row.get("source", "")).strip() or "Unknown"
        source_counts.setdefault(
            source_name,
            {"extracted": 0, "queued": 0, "kept_after_filtering": 0},
        )
        source_counts[source_name]["kept_after_filtering"] += 1


def print_source_counts(source_counts: dict[str, dict[str, int]]) -> None:
    print("\nArticles retrieved by media source:")
    print("Source\tExtracted\tQueued\tKept after filtering")
    for source_name, counts in source_counts.items():
        print(
            f"{source_name}\t"
            f"{counts['extracted']}\t"
            f"{counts['queued']}\t"
            f"{counts['kept_after_filtering']}"
        )


def build_url_to_source_map(
    discovered: list[tuple[str, SourceConfig]],
) -> dict[str, SourceConfig]:
    return {normalize_url(url): source for url, source in discovered}


def find_source_for_url(
    url: str,
    url_to_source: dict[str, SourceConfig],
    all_sources: list[SourceConfig],
) -> SourceConfig | None:
    normalized = normalize_url(url)

    if normalized in url_to_source:
        return url_to_source[normalized]

    matching_sources = [
        source for source in all_sources if domain_allowed(normalized, source.allowed_domains)
    ]

    if len(matching_sources) == 1:
        return matching_sources[0]

    for source in matching_sources:
        if looks_like_article(normalized, source):
            return source

    return None


def scrape_articles(
    app: Firecrawl,
    urls: list[str],
    url_to_source: dict[str, SourceConfig],
    all_sources: list[SourceConfig],
    keywords: list[str],
    *,
    scrape_article_pages: bool,
    search_full_body: bool,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []

    if not urls:
        return rows

    now = datetime.now(timezone.utc)

    print(f"Processing {len(urls)} article URLs...")

    for i, url in enumerate(urls, start=1):
        normalized_url = normalize_url(url)

        try:
            if scrape_article_pages:
                print(f"  [{i}/{len(urls)}] scraping article: {normalized_url}")
                article = scrape_page(app, normalized_url)
            else:
                article = {
                    "url": normalized_url,
                    "markdown": "",
                    "html": "",
                    "metadata": {},
                }

        except Exception as exc:
            print(f"  failed article scrape: {normalized_url} - {exc}")
            continue

        final_url = normalize_url(article.get("url") or normalized_url)
        source = find_source_for_url(final_url, url_to_source, all_sources)

        if source and is_source_listing_page(final_url, source):
            print(f"  skipping source listing page: {final_url}")
            continue

        title = extract_title(article)

        if is_obvious_non_article(title, final_url):
            print(f"  skipping non-article page: {final_url}")
            continue

        body = article.get("markdown") or article.get("html") or ""
        summary = make_summary(body)
        published, published_at = extract_publication_datetime(article, body)
        freshness_status = classify_freshness(published_at, now)

        if freshness_status == "old":
            print(f"  skipping old article: {final_url} ({published})")
            continue

        tags = assign_tags(title, summary, body)
        score = score_tags(tags)

        if not matches_keywords(
            title,
            summary,
            body,
            keywords,
            search_full_body=search_full_body,
        ):
            continue

        rows.append(
            {
                "source": source.name if source else "",
                "source_page": source.url if source else "",
                "title": title,
                "url": final_url,
                "published": published,
                "freshness_status": freshness_status,
                "summary": summary,
                "tags": ", ".join(tags),
                "score": score,
                "scraped_at": datetime.now(timezone.utc).isoformat(),
            }
        )

    return rows


def save_results(rows: list[dict[str, Any]], out_path: str) -> None:
    path = Path(out_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    df = pd.DataFrame(rows, columns=ARTICLE_FIELDS)
    df.to_csv(path, index=False, quoting=csv.QUOTE_MINIMAL)
    clean_summary_column_in_csv(path)

    print(f"\nSaved {len(df)} articles to {path}")


def main() -> None:


    parser = argparse.ArgumentParser(
        description="Scrape pharma/biotech news pages with Firecrawl, no RSS and no LLM."
    )

    parser.add_argument("--sources", default=DEFAULT_SOURCES_FILE)
    parser.add_argument("--out", default=DEFAULT_OUT)

    parser.add_argument(
        "--keywords",
        default=", ".join(DEFAULT_KEYWORDS),
        help="Comma-separated keywords. Use an empty string to keep all articles.",
    )

    parser.add_argument(
        "--max-links-per-source",
        type=int,
        default=20,
        help="Maximum article links to keep from each source page.",
    )

    parser.add_argument(
        "--days",
        type=int,
        default=None,
        help=argparse.SUPPRESS,
    )

    parser.add_argument(
        "--no-article-scrape",
        action="store_true",
        help="Only discover article links; do not scrape article pages.",
    )

    parser.add_argument(
        "--title-summary-only",
        action="store_true",
        help="Only match keywords against title and summary, not the full body.",
    )

    args = parser.parse_args()

    if args.days is not None:
        print(
            "Warning: --days is ignored. Dated articles are filtered to the current "
            "month and previous month."
        )

    firecrawl_api_key = os.getenv("FIRECRAWL_API_KEY", "").strip()
    if not firecrawl_api_key:
        raise SystemExit(
            "Missing FIRECRAWL_API_KEY. Provide it in the app's Firecrawl API key field "
            "or set the FIRECRAWL_API_KEY environment variable."
        )

    app = Firecrawl(api_key=firecrawl_api_key)
    sources = load_sources(args.sources)
    keywords = parse_keywords(args.keywords)

    discovered: list[tuple[str, SourceConfig]] = []
    source_counts = initial_source_counts(sources)

    for source in sources:
        try:
            urls, extracted_count = discover_article_urls(
                app,
                source,
                max_links_per_source=args.max_links_per_source,
            )
            source_counts[source.name]["extracted"] = extracted_count
            source_counts[source.name]["queued"] = len(urls)

        except Exception as exc:
            print(f"  failed source page: {source.name} - {exc}")
            continue

        for url in urls:
            discovered.append((normalize_url(url), source))

    url_to_source = build_url_to_source_map(discovered)
    all_urls = sorted(url_to_source.keys())

    print(f"\nTotal unique article URLs discovered: {len(all_urls)}")

    rows = scrape_articles(
        app,
        all_urls,
        url_to_source,
        sources,
        keywords,
        scrape_article_pages=not args.no_article_scrape,
        search_full_body=not args.title_summary_only,
    )

    rows = dedupe_articles(rows)
    update_kept_counts(source_counts, rows)
    print_source_counts(source_counts)
    save_results(rows, args.out)


if __name__ == "__main__":
    main()
