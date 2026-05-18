"""Shared utilities for scientific and news fetchers."""

from __future__ import annotations

import csv
import html
import json
import logging
import re
import xml.etree.ElementTree as ET
from datetime import date, datetime
from email.utils import parsedate_to_datetime
from time import sleep
from typing import Any, Callable, Iterable

import requests
from requests.exceptions import ChunkedEncodingError, ConnectionError, Timeout


RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}


def setup_logging(level: str = "INFO") -> None:
    """Configure process logging."""
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def request_with_retries(
    session: requests.Session,
    method: str,
    url: str,
    *,
    logger: logging.Logger | None = None,
    timeout: int = 30,
    attempts: int = 6,
    backoff_seconds: float = 0.8,
    **kwargs: Any,
) -> requests.Response:
    """Make an HTTP request with exponential backoff for transient failures."""
    for attempt in range(attempts):
        try:
            response = session.request(method, url, timeout=timeout, **kwargs)
            if response.status_code in RETRYABLE_STATUS_CODES:
                delay = backoff_seconds * (2**attempt)
                if logger:
                    logger.warning(
                        "Retryable HTTP %s from %s; retrying in %.1fs",
                        response.status_code,
                        url,
                        delay,
                    )
                sleep(delay)
                continue
            response.raise_for_status()
            return response
        except (ChunkedEncodingError, ConnectionError, Timeout) as exc:
            delay = backoff_seconds * (2**attempt)
            if logger:
                logger.warning("Network error from %s: %s; retrying in %.1fs", url, exc, delay)
            sleep(delay)

    raise RuntimeError(f"Request failed repeatedly: {url}")


def normalize_source_name(value: str) -> str:
    """Normalize source names for case-insensitive routing."""
    return re.sub(r"\s+", " ", value.strip()).lower()


def normalize_title(value: str) -> str:
    """Normalize titles for deduplication."""
    return re.sub(r"\W+", " ", (value or "").lower()).strip()


def strip_html(value: str) -> str:
    """Strip basic HTML/XML markup and decode entities."""
    no_tags = re.sub(r"<[^>]+>", " ", value or "")
    return re.sub(r"\s+", " ", html.unescape(no_tags)).strip()


def query_terms(query: str) -> list[str]:
    """Extract coarse query terms for simple local filtering."""
    stopwords = {"and", "or", "not", "the", "a", "an", "of", "for", "to", "in"}
    return [
        term.lower()
        for term in re.findall(r"[A-Za-z0-9-]+", query or "")
        if len(term) > 2 and term.lower() not in stopwords
    ]


def text_matches_query(text: str, query: str) -> bool:
    """Return true when text contains at least one query term."""
    terms = query_terms(query)
    if not terms:
        return True
    lower_text = (text or "").lower()
    return any(term in lower_text for term in terms)


def to_pubmed_date(value: str) -> str:
    """Convert YYYY-MM-DD into PubMed's YYYY/MM/DD format."""
    return value.replace("-", "/")


def compact_gdelt_date(value: str, *, end_of_day: bool = False) -> str:
    """Convert YYYY-MM-DD into GDELT's YYYYMMDDHHMMSS format."""
    digits = re.sub(r"\D", "", value or "")[:8]
    suffix = "235959" if end_of_day else "000000"
    return digits + suffix


def normalize_date(value: str) -> str:
    """Normalize common date strings to YYYY-MM-DD when possible."""
    if not value:
        return ""

    value = value.strip()
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y%m%d", "%Y-%m", "%Y"):
        try:
            parsed = datetime.strptime(value[: len(fmt)], fmt)
            if fmt == "%Y":
                return f"{parsed.year:04d}"
            if fmt == "%Y-%m":
                return f"{parsed.year:04d}-{parsed.month:02d}"
            return parsed.date().isoformat()
        except ValueError:
            pass

    try:
        return parsedate_to_datetime(value).date().isoformat()
    except Exception:
        return value


def date_in_range(value: str, mindate: str, maxdate: str) -> bool:
    """Best-effort date range filtering."""
    normalized = normalize_date(value)
    if not normalized or len(normalized) < 10:
        return True
    try:
        current = date.fromisoformat(normalized[:10])
        start = date.fromisoformat(mindate[:10])
        end = date.fromisoformat(maxdate[:10])
    except ValueError:
        return True
    return start <= current <= end


def parse_json_config(path: str | None, default_sources: list[dict[str, str]]) -> list[dict[str, str]]:
    """Load source config or return defaults."""
    if not path:
        return default_sources

    with open(path, "r", encoding="utf-8") as handle:
        data = json.load(handle)

    if not isinstance(data, list):
        raise ValueError("Config must be a JSON list of source objects.")

    sources: list[dict[str, str]] = []
    for item in data:
        if not isinstance(item, dict) or not item.get("name"):
            raise ValueError("Each config entry must be an object with at least a 'name'.")
        sources.append({"name": str(item["name"]), "query": str(item.get("query", ""))})
    return sources


def write_csv(rows: Iterable[dict[str, Any]], out_file: str, fields: list[str]) -> None:
    """Write selected fields to CSV."""
    with open(out_file, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in fields})


def write_jsonl(rows: Iterable[dict[str, Any]], out_file: str) -> None:
    """Write rows to JSONL, including raw metadata when present."""
    with open(out_file, "w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")


def dedupe_records(
    records: Iterable[dict[str, Any]],
    key_functions: list[Callable[[dict[str, Any]], tuple[str, str] | None]],
) -> list[dict[str, Any]]:
    """Deduplicate records by the first available stable key."""
    seen: set[tuple[str, str]] = set()
    output: list[dict[str, Any]] = []

    for record in records:
        key = None
        for key_function in key_functions:
            key = key_function(record)
            if key:
                break
        if not key or key in seen:
            continue
        seen.add(key)
        output.append(record)
    return output


def get_xml_text(element: ET.Element, tag: str) -> str:
    """Read a child element's text with namespace-tolerant fallback."""
    found = element.find(tag)
    if found is not None and found.text:
        return found.text.strip()
    found = element.find(f".//{tag}")
    if found is not None and found.text:
        return found.text.strip()
    return ""


def element_to_dict(element: ET.Element) -> dict[str, Any]:
    """Convert a small XML element tree into plain metadata."""
    return {
        "tag": element.tag,
        "text": (element.text or "").strip(),
        "attrib": dict(element.attrib),
        "children": [element_to_dict(child) for child in list(element)],
    }
