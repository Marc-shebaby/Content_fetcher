"""Streamlit UI wrapper for the ReceptorAI fetchers.

Run with:
    streamlit run app.py
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import pandas as pd
import streamlit as st

from scientific_fetcher import (
    DEFAULT_REVIEW_QUERY as REVIEW_DEFAULT_QUERY,
    DEFAULT_REVIEW_SOURCES,
    DEFAULT_SCHOLARLY_QUERY as SCIENTIFIC_DEFAULT_QUERY,
    DEFAULT_SOURCES as SCIENTIFIC_DEFAULT_SOURCES,
)


APP_DIR = Path(__file__).resolve().parent

FETCHER_SCRIPTS = {
    "scientific": APP_DIR / "scientific_fetcher.py",
    "media": APP_DIR / "Firecrawl.py",
}

FETCHER_OPTIONS = ["Scientific articles", "Reviews", "Media sources"]

NEWS_DEFAULT_KEYWORDS = "FDA, clinical trial, approval, peptide, peptides"

LITERATURE_FETCHERS = {
    "Scientific articles": {
        "key": "scientific",
        "script_key": "scientific",
        "description": "Fetch scientific articles from journals and scholarly APIs.",
        "sources": SCIENTIFIC_DEFAULT_SOURCES,
        "query": SCIENTIFIC_DEFAULT_QUERY,
        "mindate": date(2026, 3, 1),
        "maxdate": date(2026, 4, 30),
        "target": 500,
        "out": "scientific_articles.csv",
        "include_reviews": False,
    },
    "Reviews": {
        "key": "review",
        "script_key": "scientific",
        "description": "Fetch review articles from supported PubMed-indexed sources.",
        "sources": DEFAULT_REVIEW_SOURCES,
        "query": REVIEW_DEFAULT_QUERY,
        "mindate": date(2026, 3, 10),
        "maxdate": date(2026, 4, 30),
        "target": 200,
        "out": "pubmed_recent_reviews.csv",
        "include_reviews": True,
    },
}

LITERATURE_API_ENV_FIELDS = {
    "NCBI_EMAIL": "ncbi_email",
    "NCBI_API_KEY": "ncbi_api_key",
}

MEDIA_API_ENV_FIELDS = {
    "FIRECRAWL_API_KEY": "firecrawl_api_key",
}

MEDIA_FETCHER_TIMEOUT_SECONDS = 30 * 60


@dataclass
class StreamedProcessResult:
    """Captured result from a subprocess whose output was streamed live."""

    returncode: int
    stdout: str
    stderr: str
    timed_out: bool = False


def output_path(filename: str) -> Path:
    """Resolve a validated output filename under the app directory."""
    return APP_DIR / filename


def has_path_separator(filename: str) -> bool:
    """Return true if filename attempts to include a path."""
    return any(separator in filename for separator in ("/", "\\")) or Path(filename).name != filename


def write_temp_json(data: list[dict[str, str]], prefix: str) -> Path:
    """Write a temporary JSON config file for a fetcher subprocess."""
    temp_file = tempfile.NamedTemporaryFile(
        mode="w",
        suffix=".json",
        prefix=prefix,
        encoding="utf-8",
        delete=False,
    )
    with temp_file:
        json.dump(data, temp_file, indent=2)
    return Path(temp_file.name)


def create_literature_config_file(sources: list[dict[str, Any]], default_query: str, prefix: str) -> Path:
    """Create a temporary JSON source config for scientific or review fetchers."""
    normalized_sources = []
    for source in sources:
        name = str(source.get("name", "")).strip()
        query = str(source.get("query", "")).strip() or default_query.strip()
        if name:
            normalized_sources.append({"name": name, "query": query})

    return write_temp_json(normalized_sources, prefix)


def set_environment_variables(values: dict[str, str], fields: dict[str, str]) -> dict[str, str]:
    """Return a subprocess environment with user-provided API values set."""
    env = os.environ.copy()
    for env_name, form_key in fields.items():
        value = values.get(form_key, "").strip()
        if value:
            env[env_name] = value
    return env


def run_fetcher_script(
    script_path: Path,
    args: list[str],
    env: dict[str, str],
) -> subprocess.CompletedProcess[str]:
    """Run a fetcher script safely with captured output."""
    command = [sys.executable, str(script_path), *args]
    return subprocess.run(
        command,
        cwd=APP_DIR,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def enqueue_stream(
    stream: Any,
    stream_name: str,
    output_queue: queue.Queue[tuple[str, str]],
) -> None:
    """Read a text stream line by line into a queue for UI updates."""
    try:
        for line in iter(stream.readline, ""):
            output_queue.put((stream_name, line))
    finally:
        stream.close()


def run_fetcher_script_streaming(
    script_path: Path,
    args: list[str],
    env: dict[str, str],
    *,
    stdout_placeholder: st.delta_generator.DeltaGenerator,
    stderr_placeholder: st.delta_generator.DeltaGenerator,
    timeout_seconds: int = MEDIA_FETCHER_TIMEOUT_SECONDS,
) -> StreamedProcessResult:
    """Run a fetcher script and stream stdout/stderr into Streamlit placeholders."""
    command = [sys.executable, "-u", str(script_path), *args]
    process = subprocess.Popen(
        command,
        cwd=APP_DIR,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )

    output_queue: queue.Queue[tuple[str, str]] = queue.Queue()
    stdout_lines: list[str] = []
    stderr_lines: list[str] = []
    threads = [
        threading.Thread(
            target=enqueue_stream,
            args=(process.stdout, "stdout", output_queue),
            daemon=True,
        ),
        threading.Thread(
            target=enqueue_stream,
            args=(process.stderr, "stderr", output_queue),
            daemon=True,
        ),
    ]
    for thread in threads:
        thread.start()

    started_at = time.monotonic()
    timed_out = False

    while True:
        try:
            stream_name, line = output_queue.get(timeout=0.1)
        except queue.Empty:
            stream_name = ""
            line = ""

        if line:
            if stream_name == "stdout":
                stdout_lines.append(line)
                stdout_placeholder.code("".join(stdout_lines[-200:]), language="text")
            else:
                stderr_lines.append(line)
                stderr_placeholder.code("".join(stderr_lines[-200:]), language="text")

        if process.poll() is not None and output_queue.empty():
            break

        if time.monotonic() - started_at > timeout_seconds:
            timed_out = True
            process.kill()
            stderr_lines.append(
                f"\nTimed out after {timeout_seconds // 60} minutes. Firecrawl was stopped.\n"
            )
            stderr_placeholder.code("".join(stderr_lines[-200:]), language="text")
            break

    for thread in threads:
        thread.join(timeout=1)

    return StreamedProcessResult(
        returncode=process.wait(),
        stdout="".join(stdout_lines),
        stderr="".join(stderr_lines),
        timed_out=timed_out,
    )


def load_output_csv(path: Path) -> pd.DataFrame:
    """Load the generated CSV into a DataFrame."""
    return pd.read_csv(path)


def validate_output_files(out_file: str, raw_out_file: str = "") -> list[str]:
    """Validate output file names before running a fetcher."""
    errors = []

    if not out_file.strip().lower().endswith(".csv"):
        errors.append("Output CSV file name must end with .csv.")
    if out_file and has_path_separator(out_file.strip()):
        errors.append("Output CSV file name cannot include folders or path separators.")
    if raw_out_file.strip() and not raw_out_file.strip().lower().endswith(".jsonl"):
        errors.append("Raw JSONL file name must end with .jsonl.")
    if raw_out_file.strip() and has_path_separator(raw_out_file.strip()):
        errors.append("Raw JSONL file name cannot include folders or path separators.")

    return errors


def validate_literature_inputs(
    *,
    sources: list[dict[str, Any]],
    mindate: date,
    maxdate: date,
    target: int,
    out_file: str,
    raw_out_file: str,
) -> list[str]:
    """Validate scientific/review form inputs."""
    errors = validate_output_files(out_file, raw_out_file)
    non_empty_sources = [source for source in sources if str(source.get("name", "")).strip()]

    if not non_empty_sources:
        errors.append("Add at least one journal or source name.")
    if mindate > maxdate:
        errors.append("Minimum publication date must be before or equal to maximum publication date.")
    if target <= 0:
        errors.append("Target number of records must be a positive integer.")

    return errors


def resolve_user_path(path_value: str) -> Path:
    """Resolve an app-relative or absolute user-provided path."""
    path = Path(path_value.strip())
    if not path.is_absolute():
        path = APP_DIR / path
    return path


def validate_media_inputs(
    *,
    sources_path: str,
    max_links_per_source: int,
    out_file: str,
) -> list[str]:
    """Validate Firecrawl media form inputs."""
    errors = validate_output_files(out_file)
    if not sources_path.strip():
        errors.append("Add a Firecrawl sources JSON path.")
    elif not resolve_user_path(sources_path).exists():
        errors.append(f"Sources JSON file does not exist: {sources_path.strip()}")
    if max_links_per_source <= 0:
        errors.append("Max links per source must be a positive integer.")
    return errors


def build_literature_fetcher_args(
    *,
    fetcher_key: str,
    config_path: Path,
    mindate: date,
    maxdate: date,
    target: int,
    out_file: str,
    raw_out_file: str,
    include_reviews: bool,
    require_abstract: bool,
    allow_preprints: bool,
    exclude_title_terms: str,
    log_level: str,
) -> list[str]:
    """Build CLI arguments for scientific_fetcher.py."""
    args = [
        "--config",
        str(config_path),
        "--mindate",
        mindate.isoformat(),
        "--maxdate",
        maxdate.isoformat(),
        "--target",
        str(target),
        "--out",
        str(output_path(out_file)),
        "--log-level",
        log_level,
    ]

    if raw_out_file.strip():
        args.extend(["--raw-out", str(output_path(raw_out_file.strip()))])
    if exclude_title_terms.strip():
        args.extend(["--exclude-title-terms", exclude_title_terms.strip()])

    args.append("--include-reviews" if include_reviews else "--exclude-reviews")
    if require_abstract:
        args.append("--require-abstract")
    if allow_preprints:
        args.append("--allow-preprints")

    return args


def build_media_fetcher_args(
    *,
    sources_path: Path,
    keywords: str,
    max_links_per_source: int,
    out_file: str,
) -> list[str]:
    """Build CLI arguments for Firecrawl.py."""
    args = [
        "--sources",
        str(sources_path),
        "--out",
        str(output_path(out_file)),
        "--keywords",
        keywords.strip(),
        "--max-links-per-source",
        str(max_links_per_source),
    ]
    return args


def render_subprocess_output(result: subprocess.CompletedProcess[str]) -> None:
    """Display captured fetcher output."""
    if result.stdout.strip():
        with st.expander("Fetcher stdout", expanded=False):
            st.code(result.stdout, language="text")
    if result.stderr.strip():
        with st.expander("Fetcher stderr / logs", expanded=result.returncode != 0):
            st.code(result.stderr, language="text")


def render_downloads(csv_path: Path, raw_path: Path | None = None) -> None:
    """Render CSV preview and download buttons."""
    df = load_output_csv(csv_path)
    st.success(f"CSV created: {csv_path.name}")
    st.metric("Rows generated", len(df))
    st.dataframe(df.head(100), use_container_width=True)

    st.download_button(
        label="Download CSV",
        data=csv_path.read_bytes(),
        file_name=csv_path.name,
        mime="text/csv",
    )

    if raw_path and raw_path.exists():
        st.download_button(
            label="Download raw JSONL",
            data=raw_path.read_bytes(),
            file_name=raw_path.name,
            mime="application/jsonl",
        )


def render_literature_credentials() -> dict[str, str]:
    """Render API credential fields used by scientific and review fetchers."""
    st.subheader("API credentials")
    ncbi_email = st.text_input("NCBI email", value=os.environ.get("NCBI_EMAIL", ""))
    ncbi_api_key = st.text_input("Optional NCBI API key", value="", type="password")

    if not ncbi_email.strip():
        st.warning("NCBI recommends providing an email address for E-utilities requests.")

    return {
        "ncbi_email": ncbi_email,
        "ncbi_api_key": ncbi_api_key,
    }


def render_media_credentials() -> dict[str, str]:
    """Render API credential fields used by the Firecrawl media fetcher."""
    st.subheader("API credentials")
    firecrawl_api_key = st.text_input(
        "Firecrawl API key",
        value="",
        type="password",
        help="Leave blank to use FIRECRAWL_API_KEY from the current environment.",
    )

    return {
        "firecrawl_api_key": firecrawl_api_key,
    }


def render_literature_fetcher(label: str) -> None:
    """Render and handle a scientific or review fetcher form."""
    info = LITERATURE_FETCHERS[label]
    st.caption(info["description"])
    include_reviews = bool(info["include_reviews"])

    with st.form(f"{info['key']}_fetch_form"):
        st.subheader("Sources and query")
        default_query = st.text_area(
            "Default query applied to sources with a blank query",
            value=info["query"],
            height=90,
            key=f"{info['key']}_default_query",
        )
        sources_df = st.data_editor(
            pd.DataFrame(info["sources"]),
            num_rows="dynamic",
            use_container_width=True,
            column_config={
                "name": st.column_config.TextColumn("Journal/source name", required=True),
                "query": st.column_config.TextColumn("Query for this source"),
            },
            key=f"{info['key']}_sources_editor",
        )

        st.subheader("Fetch settings")
        settings_col1, settings_col2, settings_col3 = st.columns(3)
        with settings_col1:
            mindate = st.date_input(
                "Minimum publication date",
                value=info["mindate"],
                key=f"{info['key']}_mindate",
            )
        with settings_col2:
            maxdate = st.date_input(
                "Maximum publication date",
                value=info["maxdate"],
                key=f"{info['key']}_maxdate",
            )
            require_abstract = st.checkbox(
                "Require abstracts",
                value=True,
                key=f"{info['key']}_require_abstract",
            )
        with settings_col3:
            target = st.number_input(
                "Target number of records",
                min_value=1,
                value=info["target"],
                step=1,
                key=f"{info['key']}_target",
            )
            allow_preprints = st.checkbox(
                "Include preprints",
                value=False,
                key=f"{info['key']}_allow_preprints",
            )

        exclude_title_terms = st.text_input(
            "Disregard records when the title contains these terms",
            value="repurposing",
            help="Comma-separated, case-insensitive title terms. Example: repurposing, commentary",
            key=f"{info['key']}_exclude_title_terms",
        )

        output_col1, output_col2, output_col3 = st.columns(3)
        with output_col1:
            out_file = st.text_input(
                "Output CSV file name",
                value=info["out"],
                key=f"{info['key']}_out",
            )
        with output_col2:
            raw_out_file = st.text_input(
                "Optional raw JSONL file name",
                value="",
                key=f"{info['key']}_raw_out",
            )
        with output_col3:
            log_level = st.selectbox(
                "Log level",
                ["INFO", "DEBUG", "WARNING", "ERROR"],
                key=f"{info['key']}_log_level",
            )

        credential_values = render_literature_credentials()
        submitted = st.form_submit_button(f"Run {label.lower()}", type="primary")

    if not submitted:
        return

    script_path = FETCHER_SCRIPTS[info["script_key"]]
    if not script_path.exists():
        st.error(f"Could not find {script_path.name} in {APP_DIR}.")
        return

    sources = sources_df.fillna("").to_dict(orient="records")
    if include_reviews and allow_preprints and not any(
        str(source.get("name", "")).strip().casefold() == "biorxiv"
        for source in sources
    ):
        sources.append({"name": "bioRxiv", "query": default_query})
    errors = validate_literature_inputs(
        sources=sources,
        mindate=mindate,
        maxdate=maxdate,
        target=int(target),
        out_file=out_file.strip(),
        raw_out_file=raw_out_file.strip(),
    )
    if errors:
        for error in errors:
            st.error(error)
        return

    config_path = create_literature_config_file(
        sources,
        default_query,
        f"{info['key']}_sources_",
    )
    raw_path = output_path(raw_out_file.strip()) if raw_out_file.strip() else None
    csv_path = output_path(out_file.strip())
    env = set_environment_variables(credential_values, LITERATURE_API_ENV_FIELDS)
    args = build_literature_fetcher_args(
        fetcher_key=info["key"],
        config_path=config_path,
        mindate=mindate,
        maxdate=maxdate,
        target=int(target),
        out_file=out_file.strip(),
        raw_out_file=raw_out_file.strip(),
        include_reviews=include_reviews,
        require_abstract=require_abstract,
        allow_preprints=allow_preprints,
        exclude_title_terms=exclude_title_terms,
        log_level=log_level,
    )

    st.info("Search started.")
    st.write(f"Generated config: `{config_path}`")
    with st.expander("Generated config preview", expanded=False):
        st.json(json.loads(config_path.read_text(encoding="utf-8")))

    with st.spinner(f"Running {script_path.name}..."):
        result = run_fetcher_script(script_path, args, env)

    render_subprocess_output(result)

    if result.returncode != 0:
        st.error(f"Fetcher failed with exit code {result.returncode}.")
        return
    if not csv_path.exists():
        st.error(f"Fetcher completed, but the CSV was not created: {csv_path}")
        return

    render_downloads(csv_path, raw_path)


def render_media_fetcher() -> None:
    """Render and handle the Firecrawl media source fetcher form."""
    st.caption("Fetch media source articles with Firecrawl using a user-provided sources JSON file.")

    with st.form("media_fetch_form"):
        st.subheader("Sources and keywords")
        sources_path_value = st.text_input(
            "Firecrawl sources JSON path",
            value="sources.json",
            help="Relative paths are resolved from the app folder.",
        )
        keywords = st.text_area(
            "Keywords",
            value=NEWS_DEFAULT_KEYWORDS,
            height=70,
            help="Comma-separated keywords. Leave blank to keep all discovered articles.",
        )

        st.subheader("Fetch settings")
        settings_col1, _ = st.columns(2)
        with settings_col1:
            max_links_per_source = st.number_input(
                "Max links per source",
                min_value=1,
                value=20,
                step=1,
            )

        out_file = st.text_input("Output CSV file name", value="firecrawl_news_results.csv")
        credential_values = render_media_credentials()
        submitted = st.form_submit_button("Run media sources", type="primary")

    if not submitted:
        return

    script_path = FETCHER_SCRIPTS["media"]
    if not script_path.exists():
        st.error(f"Could not find {script_path.name} in {APP_DIR}.")
        return

    errors = validate_media_inputs(
        sources_path=sources_path_value,
        max_links_per_source=int(max_links_per_source),
        out_file=out_file.strip(),
    )
    if errors:
        for error in errors:
            st.error(error)
        return

    sources_path = resolve_user_path(sources_path_value)
    csv_path = output_path(out_file.strip())
    args = build_media_fetcher_args(
        sources_path=sources_path,
        keywords=keywords,
        max_links_per_source=int(max_links_per_source),
        out_file=out_file.strip(),
    )
    env = set_environment_variables(credential_values, MEDIA_API_ENV_FIELDS)

    st.info("Media search started.")
    st.caption(f"Running {script_path.name}. Output will appear here as Firecrawl works through sources and articles.")
    stdout_placeholder = st.empty()
    stderr_placeholder = st.empty()
    with st.spinner(f"Running {script_path.name}..."):
        result = run_fetcher_script_streaming(
            script_path,
            args,
            env,
            stdout_placeholder=stdout_placeholder,
            stderr_placeholder=stderr_placeholder,
        )

    if result.timed_out:
        st.error("Fetcher timed out before finishing. Try lowering Max links per source or reducing sources.json.")
        return
    if result.returncode != 0:
        st.error(f"Fetcher failed with exit code {result.returncode}.")
        return
    if not csv_path.exists():
        st.error(f"Fetcher completed, but the CSV was not created: {csv_path}")
        return

    render_downloads(csv_path)


def main() -> None:
    """Render the Streamlit app."""
    st.set_page_config(page_title="ReceptorAI Fetchers", layout="wide")
    st.title("ReceptorAI Fetchers")

    selected_fetcher = st.selectbox("Which fetcher do you want to use?", FETCHER_OPTIONS)

    st.markdown("### Run instructions")
    st.code(
        "pip install streamlit pandas requests feedparser beautifulsoup4 ftfy lxml python-dotenv\n"
        "streamlit run app.py",
        language="bash",
    )

    if selected_fetcher == "Media sources":
        render_media_fetcher()
    else:
        render_literature_fetcher(selected_fetcher)


if __name__ == "__main__":
    main()
