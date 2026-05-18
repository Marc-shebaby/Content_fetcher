"""Scientific article, preprint, abstract, and metadata fetcher."""

from __future__ import annotations
from dotenv import load_dotenv
import argparse
import calendar
import logging
import os
import re
import xml.etree.ElementTree as ET
from abc import ABC, abstractmethod
from datetime import date
from pathlib import Path
from typing import Any
from time import sleep
import requests
load_dotenv()
load_dotenv(dotenv_path=Path(__file__).with_name(".env.txt"), override=False)
from fetcher_utils import (
    date_in_range,
    dedupe_records,
    normalize_date,
    normalize_source_name,
    normalize_title,
    parse_json_config,
    query_terms,
    request_with_retries,
    setup_logging,
    strip_html,
    text_matches_query,
    to_pubmed_date,
    write_csv,
    write_jsonl,
)


ESEARCH_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
ESUMMARY_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi"
EFETCH_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"
EPOST_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/epost.fcgi"

DEFAULT_MINDATE = "2026-03-01"
DEFAULT_MAXDATE = "2026-04-30"
DEFAULT_TARGET = 500
DEFAULT_OUT = "pubmed_500_recent_articles_with_abstracts.csv"

PAGE_SIZE_SEARCH = 100
PAGE_SIZE_SUMMARY = 200
PAGE_SIZE_ABSTRACT = 100
REQUEST_SLEEP = 0.12

DEFAULT_SCHOLARLY_QUERY = (
    'peptides OR "drug design" OR "drug discovery" OR optimization OR '
    '"molecular modeling" OR "virtual screening"'
)

DEFAULT_REVIEW_QUERY = (
    'peptides OR peptide OR "drug design" OR "drug discovery" OR optimization OR '
    '"molecular modeling" OR "virtual screening" OR docking OR pharmacology OR '
    'therapeutics OR "medicinal chemistry" OR cheminformatics OR '
    '"structure-activity relationship"'
)
'''
DEFAULT_SOURCES = [
    
    {"name":"frontiers in pharmacology", "query": DEFAULT_SCHOLARLY_QUERY},
]
'''
DEFAULT_SOURCES = [
    {"name": "Journal of Medicinal Chemistry", "query": DEFAULT_SCHOLARLY_QUERY},
    {"name": "nature", "query": DEFAULT_SCHOLARLY_QUERY},
    {"name": "science", "query": DEFAULT_SCHOLARLY_QUERY},
    {"name": "cell", "query": DEFAULT_SCHOLARLY_QUERY},
    {"name": "Bioinformatics", "query": DEFAULT_SCHOLARLY_QUERY},
    {"name": "Journal of Chemical Information and Modeling", "query": DEFAULT_SCHOLARLY_QUERY},
    {"name":"bioRxiv", "query": DEFAULT_SCHOLARLY_QUERY},
]

DEFAULT_REVIEW_SOURCES = [
    {"name": "Frontiers in Pharmacology", "query": DEFAULT_REVIEW_QUERY},
    {"name": "Journal of Medicinal Chemistry", "query": DEFAULT_REVIEW_QUERY},
    {"name": "Nature Reviews Drug Discovery", "query": DEFAULT_REVIEW_QUERY},
    {"name": "Pharmacological Reviews", "query": DEFAULT_REVIEW_QUERY},
    {"name": "Annual Review of Pharmacology and Toxicology", "query": DEFAULT_REVIEW_QUERY},
    {"name": "Trends in Pharmacological Sciences", "query": DEFAULT_REVIEW_QUERY},
    {"name": "Drug Discovery Today", "query": DEFAULT_REVIEW_QUERY},
    {"name": "Expert Opinion on Drug Discovery", "query": DEFAULT_REVIEW_QUERY},
    {"name": "Journal of Computer-Aided Molecular Design", "query": DEFAULT_REVIEW_QUERY},
    {"name": "Journal of Cheminformatics", "query": DEFAULT_REVIEW_QUERY},
]

SCIENTIFIC_FIELDS = [
    "source_name",
    "source_type",
    "api_used",
    "external_id",
    "pmid",
    "pmcid",
    "doi",
    "title",
    "journal",
    "pub_date",
    "authors",
    "author_count",
    "publication_types",
    "abstract",
    "link",
    "is_review",
    "is_preprint",
    "content_type",
]

ALLOWED_PUBLICATION_TYPES = {
    "Journal Article",
    "Comparative Study",
    "Evaluation Study",
    "Validation Study",
}

REVIEW_PUBLICATION_TYPES = {
    "Review",
    "Systematic Review",
    "Meta-Analysis",
}

EXCLUDED_PUBLICATION_TYPES = {
    "Editorial",
    "Comment",
    "Letter",
    "News",
    "Published Erratum",
    "Retraction of Publication",
    "Retracted Publication",
    "Historical Article",
    "Biography",
    "Interview",
    "Congresses",
    "Introductory Journal Article",
}

EXCLUDED_TITLE_KEYWORDS = [
    "viewpoint",
    "perspective",
    "commentary",
    "editorial",
    "opinion",
    "letter",
    "correction",
    "erratum",
    "retraction",
    "announcement",
    "news",
    "highlights",
    "in this issue",
]

DEFAULT_EXCLUDE_TITLE_TERMS = ["repurposing"]

'''SCIENTIFIC_SOURCE_API_MAP = {
   
    "biorxiv": "pubmed",
    
}'''
SCIENTIFIC_SOURCE_API_MAP = {
    "cell": "pubmed",
    "science:": "pubmed",
    "cancer cell": "pubmed",
    "cell chemical biology": "pubmed",
    "nature": "pubmed",
    "nature biotechnology": "pubmed",
    "nature structural & molecular biology": "pubmed",
    "nature cancer": "pubmed",
    "nature reviews": "pubmed",
    "drug discovery today": "pubmed",
    "frontiers in pharmacology": "pubmed",
    "journal of medicinal chemistry": "pubmed",
    "journal of chemical information and modeling": "pubmed",
    "bioinformatics": "pubmed",
    "journal of computer-aided molecular design": "pubmed",
    "journal of cheminformatics": "pubmed",
    "science": "pubmed",
    "pubmed": "pubmed",
    "biorxiv": "pubmed",
    "ncbi": "pubmed",
    "europe pmc": "europe_pmc",
    "europepmc": "europe_pmc",
    "crossref": "crossref",
    "openalex": "openalex",
    "sciencedirect": "elsevier",
    "elsevier": "elsevier",
    "wiley": "wiley",
    "springer nature": "springer_nature",
    "acs": "acs",
    "aaas": "aaas",
    "frontiers in pharmacology":'pubmed',
    "Wiley": "pubmed",
}
SOURCE_ALIASES = {
    "j med chem": "journal of medicinal chemistry",
    "jmedchem": "journal of medicinal chemistry",
    "nature reviews drug discovery": "nature reviews",
    "aaas science": "science",
    "bio rxiv": "biorxiv",
    "springer": "springer nature",
    "science direct": "sciencedirect",
}

REVIEW_SOURCE_ALIASES = {
    "frontiers pharmacology": "frontiers in pharmacology",
    "front pharmacol": "frontiers in pharmacology",
    "j med chem": "journal of medicinal chemistry",
    "jmedchem": "journal of medicinal chemistry",
    "nat rev drug discov": "nature reviews drug discovery",
    "nature reviews": "nature reviews drug discovery",
    "nat rev chem": "nature reviews chemistry",
    "pharmacol rev": "pharmacological reviews",
    "annu rev pharmacol toxicol": "annual review of pharmacology and toxicology",
    "br j pharmacol": "british journal of pharmacology",
    "trends pharmacol sci": "trends in pharmacological sciences",
    "drug discov today": "drug discovery today",
    "expert opin drug discov": "expert opinion on drug discovery",
    "eur j med chem": "european journal of medicinal chemistry",
    "acs med chem lett": "acs medicinal chemistry letters",
    "bioorg med chem": "bioorganic & medicinal chemistry",
    "bioorg med chem lett": "bioorganic & medicinal chemistry letters",
    "jcim": "journal of chemical information and modeling",
    "j chem inf model": "journal of chemical information and modeling",
    "j comput aided mol des": "journal of computer-aided molecular design",
    "j cheminform": "journal of cheminformatics",
}

REVIEW_SOURCE_API_MAP = {
    "frontiers in pharmacology": "pubmed",
    "journal of medicinal chemistry": "pubmed",
    "nature reviews drug discovery": "pubmed",
    "nature reviews chemistry": "pubmed",
    "pharmacological reviews": "pubmed",
    "annual review of pharmacology and toxicology": "pubmed",
    "british journal of pharmacology": "pubmed",
    "trends in pharmacological sciences": "pubmed",
    "drug discovery today": "pubmed",
    "expert opinion on drug discovery": "pubmed",
    "european journal of medicinal chemistry": "pubmed",
    "acs medicinal chemistry letters": "pubmed",
    "bioorganic & medicinal chemistry": "pubmed",
    "bioorganic & medicinal chemistry letters": "pubmed",
    "journal of chemical information and modeling": "pubmed",
    "journal of computer-aided molecular design": "pubmed",
    "journal of cheminformatics": "pubmed",
    "biorxiv": "pubmed",
}

PUBMED_JOURNAL_TERMS = {
    "frontiers in pharmacology": ['"Frontiers in Pharmacology"[Journal]', '"Front Pharmacol"[Journal]'],
    "journal of medicinal chemistry": ['"Journal of Medicinal Chemistry"[Journal]', '"J Med Chem"[Journal]'],
    "nature reviews drug discovery": ['"Nature Reviews Drug Discovery"[Journal]', '"Nat Rev Drug Discov"[Journal]'],
    "nature reviews chemistry": ['"Nature Reviews Chemistry"[Journal]', '"Nat Rev Chem"[Journal]'],
    "pharmacological reviews": ['"Pharmacological Reviews"[Journal]', '"Pharmacol Rev"[Journal]'],
    "annual review of pharmacology and toxicology": [
        '"Annual Review of Pharmacology and Toxicology"[Journal]',
        '"Annu Rev Pharmacol Toxicol"[Journal]',
    ],
    "british journal of pharmacology": ['"British Journal of Pharmacology"[Journal]', '"Br J Pharmacol"[Journal]'],
    "trends in pharmacological sciences": [
        '"Trends in Pharmacological Sciences"[Journal]',
        '"Trends Pharmacol Sci"[Journal]',
    ],
    "drug discovery today": ['"Drug Discovery Today"[Journal]', '"Drug Discov Today"[Journal]'],
    "expert opinion on drug discovery": [
        '"Expert Opinion on Drug Discovery"[Journal]',
        '"Expert Opin Drug Discov"[Journal]',
    ],
    "european journal of medicinal chemistry": [
        '"European Journal of Medicinal Chemistry"[Journal]',
        '"Eur J Med Chem"[Journal]',
    ],
    "acs medicinal chemistry letters": ['"ACS Medicinal Chemistry Letters"[Journal]', '"ACS Med Chem Lett"[Journal]'],
    "bioorganic & medicinal chemistry": ['"Bioorganic & Medicinal Chemistry"[Journal]', '"Bioorg Med Chem"[Journal]'],
    "bioorganic & medicinal chemistry letters": [
        '"Bioorganic & Medicinal Chemistry Letters"[Journal]',
        '"Bioorg Med Chem Lett"[Journal]',
    ],
    "journal of chemical information and modeling": [
        '"Journal of Chemical Information and Modeling"[Journal]',
        '"J Chem Inf Model"[Journal]',
    ],
    "journal of computer-aided molecular design": [
        '"Journal of Computer-Aided Molecular Design"[Journal]',
        '"J Comput Aided Mol Des"[Journal]',
    ],
    "journal of cheminformatics": ['"Journal of Cheminformatics"[Journal]', '"J Cheminform"[Journal]'],
}

MONTHS = {
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "may": 5,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
}

PLACEHOLDER_ENV_VALUES = {
    "NCBI_EMAIL": {"your_email@example.com", ""},
    "NCBI_API_KEY": {"your_ncbi_api_key_here", ""},
}

for env_name, placeholder_values in PLACEHOLDER_ENV_VALUES.items():
    if os.getenv(env_name, "").strip() in placeholder_values:
        os.environ.pop(env_name, None)


def empty_scientific_record(
    *,
    source_name: str,
    source_type: str,
    api_used: str,
    content_type: str,
    raw_metadata: Any = None,
) -> dict[str, Any]:
    """Create a normalized scientific record shell."""
    return {
        "source_name": source_name,
        "source_type": source_type,
        "api_used": api_used,
        "external_id": "",
        "pmid": "",
        "pmcid": "",
        "doi": "",
        "title": "",
        "journal": source_name,
        "pub_date": "",
        "authors": "",
        "author_count": None,
        "publication_types": "",
        "abstract": "",
        "link": "",
        "is_review": False,
        "is_preprint": False,
        "content_type": content_type,
        "raw_metadata": raw_metadata or {},
    }


class BaseScientificFetcher(ABC):
    """Common scientific source interface."""

    source_type = "scientific"
    api_used = "unknown"

    def __init__(self, logger: logging.Logger | None = None) -> None:
        self.logger = logger or logging.getLogger(self.__class__.__name__)
        self.session = requests.Session()

    @abstractmethod
    def search(
        self,
        source_name: str,
        query: str,
        mindate: str,
        maxdate: str,
        limit: int,
        include_reviews: bool = False,
    ) -> list[dict[str, Any]]:
        """Return normalized scientific records."""

    def close(self) -> None:
        self.session.close()


class PubMedFetcher(BaseScientificFetcher):
    """PubMed/NCBI E-utilities fetcher."""

    source_type = "pubmed"
    api_used = "NCBI ESearch/EPost/ESummary/EFetch"

    def __init__(self, logger: logging.Logger | None = None) -> None:
        super().__init__(logger)
        self.api_key = os.getenv("NCBI_API_KEY", "")
        self.email = os.getenv("NCBI_EMAIL", "")
        if not self.email:
            self.logger.warning("NCBI_EMAIL is not set; NCBI recommends including one.")
        
    def _ncbi_params(self) -> dict[str, str]:
        params: dict[str, str] = {}
        if self.email:
            params["email"] = self.email
        if self.api_key:
            params["api_key"] = self.api_key
        return params

    def build_search_term(self, journal_name: str, query: str, include_reviews: bool) -> str:
        """Build a PubMed journal query."""
        query = query.strip() or DEFAULT_SCHOLARLY_QUERY
        excluded_types = [
            "editorial[Publication Type]",
            "comment[Publication Type]",
            "letter[Publication Type]",
            "news[Publication Type]",
            "published erratum[Publication Type]",
            "retraction of publication[Publication Type]",
        ]
        if not include_reviews:
            excluded_types.extend(
                [
                    "review[Publication Type]",
                    "systematic review[Publication Type]",
                    "meta-analysis[Publication Type]",
                ]
            )
        return (
            f'"{journal_name}"[Journal] '
            f"AND ({query}) "
            f"NOT ({' OR '.join(excluded_types)})"
        )

    def search(
        self,
        source_name: str,
        query: str,
        mindate: str,
        maxdate: str,
        limit: int,
        include_reviews: bool = False,
    ) -> list[dict[str, Any]]:
        pmids = self.search_pmids(source_name, query, mindate, maxdate, limit, include_reviews)
        if not pmids:
            return []
        summaries = self.fetch_summaries(source_name, pmids)
        details = self.fetch_article_details(pmids)
        is_biorxiv_source = canonical_source_key(source_name) == "biorxiv"
        for row in summaries:
            detail = details.get(row["pmid"], {})
            publication_types = detail.get("publication_types", [])
            authors = detail.get("authors") or row.get("authors", "")
            author_count = detail.get("author_count") or row.get("author_count")
            is_review = any(pt in REVIEW_PUBLICATION_TYPES for pt in publication_types)
            row.update(
                {
                    "abstract": detail.get("abstract", ""),
                    "publication_types": "; ".join(publication_types),
                    "pmcid": detail.get("pmcid", row.get("pmcid", "")),
                    "doi": row.get("doi") or detail.get("doi", ""),
                    "authors": authors,
                    "author_count": author_count,
                    "is_review": is_review,
                    "is_preprint": is_biorxiv_source,
                    "content_type": "preprint"
                    if is_biorxiv_source
                    else "review"
                    if is_review
                    else "journal_article",
                }
            )
        return summaries

    def search_pmids(
        self,
        journal_name: str,
        query: str,
        mindate: str,
        maxdate: str,
        max_to_fetch: int,
        include_reviews: bool,
    ) -> list[str]:
        """Search one PubMed journal and return PMIDs."""
        all_pmids: list[str] = []
        base_params = {
            "db": "pubmed",
            "term": self.build_search_term(journal_name, query, include_reviews),
            "datetype": "pdat",
            "mindate": to_pubmed_date(mindate),
            "maxdate": to_pubmed_date(maxdate),
            "sort": "pub+date",
            "retmode": "json",
            **self._ncbi_params(),
        }
        response = request_with_retries(
            self.session,
            "GET",
            ESEARCH_URL,
            params={**base_params, "retmax": 0},
            logger=self.logger,
        )
        count = int(response.json().get("esearchresult", {}).get("count", 0))
        limit = min(count, 10000, max_to_fetch)
        self.logger.info("%s: %s PubMed results; collecting up to %s", journal_name, count, limit)

        for retstart in range(0, limit, PAGE_SIZE_SEARCH):
            retmax = min(PAGE_SIZE_SEARCH, limit - retstart)
            response = request_with_retries(
                self.session,
                "GET",
                ESEARCH_URL,
                params={**base_params, "retstart": retstart, "retmax": retmax},
                logger=self.logger,
            )
            pmids = response.json().get("esearchresult", {}).get("idlist", [])
            if not pmids:
                break
            all_pmids.extend(pmids)
            sleep(REQUEST_SLEEP)
        return all_pmids

    def epost_pmids(self, pmids: list[str]) -> tuple[str, str]:
        """Upload PMIDs to NCBI history server."""
        response = request_with_retries(
            self.session,
            "POST",
            EPOST_URL,
            params={"db": "pubmed", **self._ncbi_params()},
            data={"id": ",".join(pmids)},
            logger=self.logger,
        )
        root = ET.fromstring(response.content)
        webenv = root.findtext(".//WebEnv")
        query_key = root.findtext(".//QueryKey")
        if not webenv or not query_key:
            raise RuntimeError("Failed to parse WebEnv/QueryKey from EPost response.")
        return webenv, query_key

    def fetch_summaries(self, source_name: str, pmids: list[str]) -> list[dict[str, Any]]:
        """Fetch PubMed summaries."""
        rows: list[dict[str, Any]] = []
        webenv, query_key = self.epost_pmids(pmids)
        for retstart in range(0, len(pmids), PAGE_SIZE_SUMMARY):
            response = request_with_retries(
                self.session,
                "GET",
                ESUMMARY_URL,
                params={
                    "db": "pubmed",
                    "query_key": query_key,
                    "WebEnv": webenv,
                    "retstart": retstart,
                    "retmax": PAGE_SIZE_SUMMARY,
                    "retmode": "json",
                    **self._ncbi_params(),
                },
                logger=self.logger,
            )
            summaries = response.json().get("result", {})
            for uid in summaries.get("uids", []):
                record = summaries.get(uid, {})
                authors = [
                    author.get("name", "")
                    for author in record.get("authors", [])
                    if author.get("name")
                ]
                row = empty_scientific_record(
                    source_name=source_name,
                    source_type=self.source_type,
                    api_used=self.api_used,
                    content_type="journal_article",
                    raw_metadata=record,
                )
                row.update(
                    {
                        "external_id": uid,
                        "pmid": uid,
                        "doi": doi_from_summary(record),
                        "title": record.get("title", ""),
                        "journal": record.get("fulljournalname", source_name),
                        "pub_date": record.get("pubdate", ""),
                        "authors": ", ".join(authors),
                        "author_count": len(authors),
                        "link": f"https://pubmed.ncbi.nlm.nih.gov/{uid}/",
                    }
                )
                rows.append(row)
            self.logger.info("Fetched PubMed summaries: %s/%s", len(rows), len(pmids))
            sleep(REQUEST_SLEEP)
        return rows

    def fetch_article_details(self, pmids: list[str]) -> dict[str, dict[str, Any]]:
        """Fetch abstracts, publication types, authors, DOI, and PMCID from PubMed XML."""
        details: dict[str, dict[str, Any]] = {}
        for start in range(0, len(pmids), PAGE_SIZE_ABSTRACT):
            batch = pmids[start : start + PAGE_SIZE_ABSTRACT]
            response = request_with_retries(
                self.session,
                "GET",
                EFETCH_URL,
                params={
                    "db": "pubmed",
                    "id": ",".join(batch),
                    "retmode": "xml",
                    **self._ncbi_params(),
                },
                logger=self.logger,
            )
            root = ET.fromstring(response.content)
            for article in root.findall(".//PubmedArticle"):
                pmid = (article.findtext(".//PMID") or "").strip()
                if not pmid:
                    continue
                abstract_parts = []
                for abstract_text in article.findall(".//Abstract/AbstractText"):
                    label = abstract_text.attrib.get("Label")
                    text = "".join(abstract_text.itertext()).strip()
                    if text:
                        abstract_parts.append(f"{label}: {text}" if label else text)
                publication_types = [
                    item.text.strip()
                    for item in article.findall(".//PublicationTypeList/PublicationType")
                    if item.text
                ]
                authors = parse_pubmed_xml_authors(article)
                details[pmid] = {
                    "abstract": "\n".join(abstract_parts).strip(),
                    "publication_types": publication_types,
                    "authors": ", ".join(authors),
                    "author_count": len(authors),
                    "doi": article_id(article, "doi"),
                    "pmcid": article_id(article, "pmc"),
                }
            self.logger.info(
                "Fetched PubMed article details: %s/%s",
                min(start + PAGE_SIZE_ABSTRACT, len(pmids)),
                len(pmids),
            )
            sleep(REQUEST_SLEEP)
        return details


class PubMedReviewFetcher(PubMedFetcher):
    """PubMed fetcher that searches review publication types only."""

    def build_search_term(self, journal_name: str, query: str, include_reviews: bool) -> str:
        query = query.strip() or DEFAULT_REVIEW_QUERY
        key = canonical_review_source_key(journal_name)
        journal_terms = PUBMED_JOURNAL_TERMS.get(key, [f'"{journal_name}"[Journal]'])
        review_types = [
            "review[Publication Type]",
            "systematic review[Publication Type]",
            "meta-analysis[Publication Type]",
        ]
        excluded_types = [
            "editorial[Publication Type]",
            "comment[Publication Type]",
            "letter[Publication Type]",
            "news[Publication Type]",
            "published erratum[Publication Type]",
            "retraction of publication[Publication Type]",
        ]
        return (
            f"({' OR '.join(journal_terms)}) "
            f"AND ({query}) "
            f"AND ({' OR '.join(review_types)}) "
            f"NOT ({' OR '.join(excluded_types)})"
        )


class EuropePMCFetcher(BaseScientificFetcher):
    """Europe PMC metadata fetcher."""

    source_type = "metadata"
    api_used = "Europe PMC REST API"
    endpoint = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"

    def search(
        self,
        source_name: str,
        query: str,
        mindate: str,
        maxdate: str,
        limit: int,
        include_reviews: bool = False,
    ) -> list[dict[str, Any]]:
        full_query = f"({query}) FIRST_PDATE:[{mindate} TO {maxdate}]"
        response = request_with_retries(
            self.session,
            "GET",
            self.endpoint,
            params={"query": full_query, "format": "json", "pageSize": min(limit, 1000)},
            logger=self.logger,
        )
        rows = []
        for item in response.json().get("resultList", {}).get("result", []):
            row = empty_scientific_record(
                source_name=source_name,
                source_type=self.source_type,
                api_used=self.api_used,
                content_type=item.get("pubType", "metadata"),
                raw_metadata=item,
            )
            pub_types = item.get("pubType", "")
            row.update(
                {
                    "external_id": item.get("id", ""),
                    "pmid": item.get("pmid", ""),
                    "pmcid": item.get("pmcid", ""),
                    "doi": item.get("doi", ""),
                    "title": item.get("title", ""),
                    "journal": item.get("journalTitle", source_name),
                    "pub_date": item.get("firstPublicationDate", item.get("pubYear", "")),
                    "authors": item.get("authorString", ""),
                    "publication_types": pub_types,
                    "abstract": item.get("abstractText", ""),
                    "link": item.get("fullTextUrlList", {}).get("fullTextUrl", [{}])[0].get("url", "")
                    if item.get("fullTextUrlList")
                    else "",
                    "is_review": "review" in pub_types.lower(),
                    "is_preprint": item.get("source", "").lower() == "ppr",
                }
            )
            rows.append(row)
        return rows


class CrossrefFetcher(BaseScientificFetcher):
    """Crossref works metadata fetcher."""

    source_type = "metadata"
    api_used = "Crossref Works API"
    endpoint = "https://api.crossref.org/works"

    def search(
        self,
        source_name: str,
        query: str,
        mindate: str,
        maxdate: str,
        limit: int,
        include_reviews: bool = False,
    ) -> list[dict[str, Any]]:
        response = request_with_retries(
            self.session,
            "GET",
            self.endpoint,
            params={
                "query": query,
                "filter": f"from-pub-date:{mindate},until-pub-date:{maxdate}",
                "rows": min(limit, 1000),
                "sort": "published",
                "order": "desc",
            },
            logger=self.logger,
        )
        rows = []
        for item in response.json().get("message", {}).get("items", []):
            pub_date = crossref_date(item)
            authors = crossref_authors(item)
            row = empty_scientific_record(
                source_name=source_name,
                source_type=self.source_type,
                api_used=self.api_used,
                content_type=item.get("type", "metadata"),
                raw_metadata=item,
            )
            row.update(
                {
                    "external_id": item.get("DOI", item.get("URL", "")),
                    "doi": item.get("DOI", ""),
                    "title": " ".join(item.get("title", [])),
                    "journal": " ".join(item.get("container-title", [])) or source_name,
                    "pub_date": pub_date,
                    "authors": authors,
                    "author_count": len([a for a in authors.split(", ") if a]),
                    "publication_types": item.get("type", ""),
                    "abstract": strip_html(item.get("abstract", "")),
                    "link": item.get("URL", ""),
                    "is_review": item.get("type") == "journal-article"
                    and "review" in " ".join(item.get("subtype", [])).lower(),
                }
            )
            rows.append(row)
        return rows


class OpenAlexFetcher(BaseScientificFetcher):
    """OpenAlex works metadata fetcher."""

    source_type = "metadata"
    api_used = "OpenAlex Works API"
    endpoint = "https://api.openalex.org/works"

    def search(
        self,
        source_name: str,
        query: str,
        mindate: str,
        maxdate: str,
        limit: int,
        include_reviews: bool = False,
    ) -> list[dict[str, Any]]:
        response = request_with_retries(
            self.session,
            "GET",
            self.endpoint,
            params={
                "search": query,
                "filter": f"from_publication_date:{mindate},to_publication_date:{maxdate}",
                "per-page": min(limit, 200),
                "sort": "publication_date:desc",
            },
            logger=self.logger,
        )
        rows = []
        for item in response.json().get("results", []):
            authors = ", ".join(
                authorship.get("author", {}).get("display_name", "")
                for authorship in item.get("authorships", [])
                if authorship.get("author", {}).get("display_name")
            )
            row = empty_scientific_record(
                source_name=source_name,
                source_type=self.source_type,
                api_used=self.api_used,
                content_type=item.get("type", "metadata"),
                raw_metadata=item,
            )
            doi = (item.get("doi") or "").replace("https://doi.org/", "")
            row.update(
                {
                    "external_id": item.get("id", ""),
                    "doi": doi,
                    "title": item.get("display_name", ""),
                    "journal": item.get("primary_location", {})
                    .get("source", {})
                    .get("display_name", source_name),
                    "pub_date": item.get("publication_date", ""),
                    "authors": authors,
                    "author_count": len(item.get("authorships", [])),
                    "publication_types": item.get("type", ""),
                    "link": item.get("primary_location", {}).get("landing_page_url", ""),
                    "is_review": item.get("type") == "review",
                }
            )
            rows.append(row)
        return rows


class CredentialedPublisherFetcher(BaseScientificFetcher):
    """Base placeholder for publisher APIs that require credentials/licensing."""

    source_type = "publisher"
    api_used = "publisher API"
    env_var = ""

    def search(
        self,
        source_name: str,
        query: str,
        mindate: str,
        maxdate: str,
        limit: int,
        include_reviews: bool = False,
    ) -> list[dict[str, Any]]:
        if self.env_var and not os.getenv(self.env_var):
            self.logger.warning("%s is not set; skipping %s.", self.env_var, source_name)
            return []
        self.logger.warning(
            "%s integration for %s requires endpoint and license details; returning no records.",
            self.api_used,
            source_name,
        )
        # TODO: Implement the vendor-specific request after confirming entitlement,
        # endpoint, query syntax, and allowed metadata/full-text fields.
        return []


class ElsevierFetcher(CredentialedPublisherFetcher):
    api_used = "Elsevier ScienceDirect API"
    env_var = "ELSEVIER_API_KEY"


class SpringerNatureFetcher(CredentialedPublisherFetcher):
    api_used = "Springer Nature API"
    env_var = "SPRINGER_NATURE_API_KEY"


class WileyFetcher(CredentialedPublisherFetcher):
    api_used = "Wiley API"
    env_var = "WILEY_API_KEY"


class ACSFetcher(CredentialedPublisherFetcher):
    api_used = "ACS API"
    env_var = "ACS_API_KEY"


class AAASFetcher(CredentialedPublisherFetcher):
    api_used = "AAAS API"
    env_var = "AAAS_API_KEY"


FETCHER_REGISTRY: dict[str, type[BaseScientificFetcher]] = {
    "pubmed": PubMedFetcher,
    "europe_pmc": EuropePMCFetcher,
    "crossref": CrossrefFetcher,
    "openalex": OpenAlexFetcher,
    "elsevier": ElsevierFetcher,
    "springer_nature": SpringerNatureFetcher,
    "wiley": WileyFetcher,
    "acs": ACSFetcher,
    "aaas": AAASFetcher,
}


def get_scientific_fetcher_for_source(
    source_name: str,
    logger: logging.Logger | None = None,
) -> BaseScientificFetcher:
    """Resolve source name/alias to a scientific fetcher."""
    key = canonical_source_key(source_name)
    api_name = SCIENTIFIC_SOURCE_API_MAP.get(key)
    if not api_name:
        supported = ", ".join(sorted(SCIENTIFIC_SOURCE_API_MAP))
        raise ValueError(
            f"Unsupported scientific source '{source_name}'. Add an alias, map it to an API, "
            f"and register a fetcher. Supported keys include: {supported}"
        )
    return FETCHER_REGISTRY[api_name](logger=logger)


def get_review_fetcher_for_source(
    source_name: str,
    logger: logging.Logger | None = None,
) -> BaseScientificFetcher:
    """Resolve source name/alias to a review-only fetcher."""
    key = canonical_review_source_key(source_name)
    api_name = REVIEW_SOURCE_API_MAP.get(key)
    if not api_name:
        supported = ", ".join(sorted(REVIEW_SOURCE_API_MAP))
        raise ValueError(
            f"Unsupported review source '{source_name}'. Supported review sources are: {supported}"
        )
    if api_name == "pubmed":
        return PubMedReviewFetcher(logger=logger)
    return FETCHER_REGISTRY[api_name](logger=logger)


def canonical_source_key(source_name: str) -> str:
    """Normalize a scientific source name and resolve aliases."""
    key = normalize_source_name(source_name)
    return SOURCE_ALIASES.get(key, key)


def canonical_review_source_key(source_name: str) -> str:
    """Normalize a review source name and resolve review-specific aliases."""
    key = normalize_source_name(source_name)
    return REVIEW_SOURCE_ALIASES.get(key, canonical_source_key(source_name))


def doi_from_summary(record: dict[str, Any]) -> str:
    """Extract DOI from PubMed ESummary metadata."""
    for article_id_data in record.get("articleids", []):
        if article_id_data.get("idtype") == "doi" and article_id_data.get("value"):
            return article_id_data["value"].strip()
    elocation = record.get("elocationid", "")
    match = re.search(r"10\.\S+", elocation)
    return match.group(0).strip(" .;") if match else ""


def article_id(article: ET.Element, id_type: str) -> str:
    """Extract PubMed XML ArticleId by IdType."""
    for item in article.findall(".//ArticleIdList/ArticleId"):
        if item.attrib.get("IdType") == id_type and item.text:
            return item.text.strip()
    return ""


def parse_pubmed_xml_authors(article: ET.Element) -> list[str]:
    """Parse author names from PubMed XML."""
    authors = []
    for author in article.findall(".//AuthorList/Author"):
        collective = author.findtext("CollectiveName")
        if collective:
            authors.append(collective.strip())
            continue
        last = author.findtext("LastName") or ""
        fore = author.findtext("ForeName") or ""
        name = " ".join(part for part in [fore.strip(), last.strip()] if part)
        if name:
            authors.append(name)
    return authors


def crossref_date(item: dict[str, Any]) -> str:
    """Extract best available Crossref publication date."""
    for key in ("published-print", "published-online", "published", "created"):
        parts = item.get(key, {}).get("date-parts", [[]])[0]
        if parts:
            try:
                year = int(parts[0])
                month = int(parts[1]) if len(parts) > 1 else 1
                day = int(parts[2]) if len(parts) > 2 else 1
                return f"{year:04d}-{month:02d}-{day:02d}"
            except (TypeError, ValueError):
                pass
    return ""


def crossref_authors(item: dict[str, Any]) -> str:
    """Format Crossref authors."""
    names = []
    for author in item.get("author", []):
        given = author.get("given", "")
        family = author.get("family", "")
        name = " ".join(part for part in [given, family] if part).strip()
        if name:
            names.append(name)
    return ", ".join(names)


def looks_like_non_research_article(record: dict[str, Any]) -> bool:
    """Detect PubMed non-research items from title/type/abstract text."""
    combined = " ".join(
        [
            str(record.get("title", "")),
            str(record.get("publication_types", "")),
            str(record.get("abstract", "")),
        ]
    ).lower()
    return any(keyword in combined for keyword in EXCLUDED_TITLE_KEYWORDS)


def parse_exclude_title_terms(value: str) -> list[str]:
    """Parse comma-separated title terms that should exclude records."""
    return [term.strip().lower() for term in value.split(",") if term.strip()]


def title_has_excluded_term(record: dict[str, Any], exclude_title_terms: list[str]) -> bool:
    """Return true when a record title contains a user-provided excluded term."""
    title = str(record.get("title", "")).lower()
    return any(term in title for term in exclude_title_terms)


def keep_scientific_record(
    record: dict[str, Any],
    *,
    include_reviews: bool,
    require_abstract: bool,
    allow_preprints: bool,
    exclude_title_terms: list[str],
) -> bool:
    """Apply source-aware scientific filtering."""
    if title_has_excluded_term(record, exclude_title_terms):
        return False

    if record.get("is_preprint") and not allow_preprints:
        return False

    if require_abstract and not str(record.get("abstract", "")).strip():
        return False

    publication_types = [
        item.strip()
        for item in str(record.get("publication_types", "")).split(";")
        if item.strip()
    ]

    if record.get("api_used", "").startswith("NCBI"):
        if not str(record.get("abstract", "")).strip():
            return False
        if any(pub_type in EXCLUDED_PUBLICATION_TYPES for pub_type in publication_types):
            return False
        if record.get("is_review") and not include_reviews:
            return False
        if not record.get("is_preprint") and not include_reviews and not any(
            pub_type in ALLOWED_PUBLICATION_TYPES for pub_type in publication_types
        ):
            return False
        if (
            not record.get("is_preprint")
            and int(record.get("author_count") or 0) <= 2
            and not record.get("is_review")
        ):
            return False
        if looks_like_non_research_article(record):
            return False
        return True

    if record.get("is_review") and not include_reviews:
        return False

    if not str(record.get("title", "")).strip():
        return False
    return True


def looks_like_non_review_article(record: dict[str, Any]) -> bool:
    """Detect PubMed items that are not substantive review articles."""
    combined = " ".join(
        [
            str(record.get("title", "")),
            str(record.get("publication_types", "")),
            str(record.get("abstract", "")),
        ]
    ).lower()
    return any(keyword in combined for keyword in EXCLUDED_TITLE_KEYWORDS)


def review_date_in_range(value: str, mindate: str, maxdate: str) -> bool:
    """Strict review date filter that rejects partial dates outside the window."""
    bounds = review_date_bounds(value)
    if not bounds:
        return False

    start = date.fromisoformat(mindate[:10])
    end = date.fromisoformat(maxdate[:10])
    record_start, record_end = bounds
    return record_start <= end and record_end >= start


def review_date_bounds(value: str) -> tuple[date, date] | None:
    """Return best-effort date bounds for PubMed date strings."""
    text = str(value or "").strip()
    if not text:
        return None

    normalized = normalize_date(text)
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", normalized):
        parsed = date.fromisoformat(normalized)
        return parsed, parsed
    if re.fullmatch(r"\d{4}-\d{2}", normalized):
        year, month = [int(part) for part in normalized.split("-")]
        return month_bounds(year, month)
    if re.fullmatch(r"\d{4}", normalized):
        year = int(normalized)
        return date(year, 1, 1), date(year, 12, 31)

    pubmed_match = re.search(
        r"\b(?P<year>\d{4})(?:\s+(?P<month>[A-Za-z]{3,9})(?:\s+(?P<day>\d{1,2}))?)?",
        text,
    )
    if not pubmed_match:
        return None

    year = int(pubmed_match.group("year"))
    month_text = (pubmed_match.group("month") or "").lower()[:3]
    if not month_text:
        return date(year, 1, 1), date(year, 12, 31)

    month = MONTHS.get(month_text)
    if not month:
        return None

    day_text = pubmed_match.group("day")
    if day_text:
        try:
            parsed = date(year, month, int(day_text))
            return parsed, parsed
        except ValueError:
            return None

    return month_bounds(year, month)


def month_bounds(year: int, month: int) -> tuple[date, date]:
    """Return first and last day for a year-month pair."""
    return date(year, month, 1), date(year, month, calendar.monthrange(year, month)[1])


def keep_review_record(
    record: dict[str, Any],
    *,
    require_abstract: bool,
    allow_preprints: bool,
    exclude_title_terms: list[str],
) -> bool:
    """Apply review-only filtering on top of the shared scientific filters."""
    if title_has_excluded_term(record, exclude_title_terms):
        return False

    if record.get("is_preprint"):
        if not allow_preprints:
            return False
        if require_abstract and not str(record.get("abstract", "")).strip():
            return False
        if looks_like_non_review_article(record):
            return False
        return bool(str(record.get("title", "")).strip())

    if require_abstract and not str(record.get("abstract", "")).strip():
        return False

    publication_types = [
        item.strip()
        for item in str(record.get("publication_types", "")).split(";")
        if item.strip()
    ]
    has_review_type = any(pub_type in REVIEW_PUBLICATION_TYPES for pub_type in publication_types)
    if not has_review_type and not record.get("is_review"):
        return False

    if any(pub_type in EXCLUDED_PUBLICATION_TYPES for pub_type in publication_types):
        return False

    if looks_like_non_review_article(record):
        return False

    return keep_scientific_record(
        record,
        include_reviews=True,
        require_abstract=require_abstract,
        allow_preprints=allow_preprints,
        exclude_title_terms=exclude_title_terms,
    )


def dedupe_scientific_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Deduplicate scientific records by DOI, PMID, PMCID, external ID, then title/date."""

    def by_doi(record: dict[str, Any]) -> tuple[str, str] | None:
        doi = str(record.get("doi", "")).strip().lower()
        return ("doi", doi) if doi else None

    def by_pmid(record: dict[str, Any]) -> tuple[str, str] | None:
        pmid = str(record.get("pmid", "")).strip().lower()
        return ("pmid", pmid) if pmid else None

    def by_pmcid(record: dict[str, Any]) -> tuple[str, str] | None:
        pmcid = str(record.get("pmcid", "")).strip().lower()
        return ("pmcid", pmcid) if pmcid else None

    def by_external_source(record: dict[str, Any]) -> tuple[str, str] | None:
        external_id = str(record.get("external_id", "")).strip().lower()
        source = str(record.get("source_name", "")).strip().lower()
        return ("external_source", f"{source}:{external_id}") if external_id else None

    def by_title_date(record: dict[str, Any]) -> tuple[str, str] | None:
        title = normalize_title(str(record.get("title", "")))
        pub_date = normalize_date(str(record.get("pub_date", "")))
        return ("title_date", f"{title}:{pub_date}") if title else None

    return dedupe_records(
        records, [by_doi, by_pmid, by_pmcid, by_external_source, by_title_date]
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", help="Path to scientific JSON source config.")
    parser.add_argument("--mindate", default=DEFAULT_MINDATE, help="Start date, YYYY-MM-DD.")
    parser.add_argument("--maxdate", default=DEFAULT_MAXDATE, help="End date, YYYY-MM-DD.")
    parser.add_argument("--target", type=int, default=DEFAULT_TARGET, help="Maximum output rows.")
    parser.add_argument("--out", default=DEFAULT_OUT, help="Output CSV path.")
    parser.add_argument("--raw-out", help="Optional JSONL path for raw metadata.")
    parser.add_argument(
        "--include-reviews",
        action="store_true",
        help="Fetch review articles instead of scientific articles.",
    )
    parser.add_argument(
        "--exclude-reviews",
        action="store_true",
        help="Explicitly exclude reviews. This is the default.",
    )
    parser.add_argument(
        "--require-abstract",
        action="store_true",
        help="Require abstracts for every scientific source, not only PubMed.",
    )
    parser.add_argument(
        "--allow-preprints",
        action="store_true",
        help="Allow preprints where the selected source/API exposes preprint metadata.",
    )
    parser.add_argument(
        "--exclude-title-terms",
        default=", ".join(DEFAULT_EXCLUDE_TITLE_TERMS),
        help=(
            "Comma-separated title terms to disregard. Matching is case-insensitive "
            "and substring-based. Default: repurposing."
        ),
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging verbosity.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    setup_logging(args.log_level)
    logger = logging.getLogger("scientific_fetcher")

    review_mode = args.include_reviews and not args.exclude_reviews
    exclude_title_terms = parse_exclude_title_terms(args.exclude_title_terms)
    if exclude_title_terms:
        logger.info("Excluding records with these title terms: %s", ", ".join(exclude_title_terms))
    sources = parse_json_config(args.config, DEFAULT_REVIEW_SOURCES if review_mode else DEFAULT_SOURCES)
    all_records: list[dict[str, Any]] = []
    source_counts: dict[str, dict[str, int]] = {}

    for source in sources:
        source_name = source["name"]
        query = source.get("query", "")
        try:
            fetcher = (
                get_review_fetcher_for_source(source_name, logger=logger)
                if review_mode
                else get_scientific_fetcher_for_source(source_name, logger=logger)
            )
        except ValueError as exc:
            logger.error("%s", exc)
            continue

        logger.info(
            "Processing %s source '%s' with %s (%s)",
            "review" if review_mode else "scientific",
            source_name,
            fetcher.__class__.__name__,
            fetcher.api_used,
        )
        try:
            found = fetcher.search(
                source_name=source_name,
                query=query,
                mindate=args.mindate,
                maxdate=args.maxdate,
                limit=args.target,
                include_reviews=review_mode,
            )
        except Exception as exc:
            logger.exception("Failed processing %s: %s", source_name, exc)
            found = []
        finally:
            fetcher.close()

        kept = [
            record
            for record in found
            if (
                review_date_in_range(str(record.get("pub_date", "")), args.mindate, args.maxdate)
                if review_mode
                else date_in_range(str(record.get("pub_date", "")), args.mindate, args.maxdate)
            )
            and text_matches_query(
                f"{record.get('title', '')} {record.get('abstract', '')}",
                query,
            )
            and (
                keep_review_record(
                    record,
                    require_abstract=args.require_abstract,
                    allow_preprints=args.allow_preprints,
                    exclude_title_terms=exclude_title_terms,
                )
                if review_mode
                else keep_scientific_record(
                    record,
                    include_reviews=False,
                    require_abstract=args.require_abstract,
                    allow_preprints=args.allow_preprints,
                    exclude_title_terms=exclude_title_terms,
                )
            )
        ]
        logger.info("%s: found %s records; kept %s after filtering", source_name, len(found), len(kept))
        source_counts[source_name] = {
            "fetched": len(found),
            "kept_after_filtering": len(kept),
            "final_csv": 0,
        }
        all_records.extend(kept)

    deduped = dedupe_scientific_records(all_records)
    final_rows = deduped[: args.target]
    for row in final_rows:
        source_name = str(row.get("source_name", "Unknown")) or "Unknown"
        source_counts.setdefault(
            source_name,
            {"fetched": 0, "kept_after_filtering": 0, "final_csv": 0},
        )
        source_counts[source_name]["final_csv"] += 1

    write_csv(final_rows, args.out, SCIENTIFIC_FIELDS)
    if args.raw_out:
        write_jsonl(final_rows, args.raw_out)

    logger.info(
        "Saved %s %s records to %s after dedupe (%s before dedupe).",
        len(final_rows),
        "review" if review_mode else "scientific",
        args.out,
        len(all_records),
    )
    if args.raw_out:
        logger.info("Saved raw metadata to %s.", args.raw_out)

    print(f"\n{'Reviews' if review_mode else 'Articles'} retrieved by source:")
    print("Source\tFetched\tKept after filtering\tFinal CSV")
    for source_name, counts in source_counts.items():
        print(
            f"{source_name}\t"
            f"{counts['fetched']}\t"
            f"{counts['kept_after_filtering']}\t"
            f"{counts['final_csv']}"
        )


if __name__ == "__main__":
    main()
