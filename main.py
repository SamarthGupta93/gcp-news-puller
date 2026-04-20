#!/usr/bin/env python3
"""
Unified runner: fetches GCP Blog articles and Vertex AI release notes in parallel,
combines them into a single JSON file sorted by date.

Usage:
    uv run main.py --date 07/31/2025
    uv run main.py --date 07/31/2025 --no-enrich   # skip LLM metadata for blog
"""

import argparse
import concurrent.futures
import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

import requests
from dotenv import load_dotenv
from google import genai

from gcp_blog_scraper import (
    HEADERS,
    LOOKBACK_WEEKS,
    MAX_WORKERS,
    fetch_article_details,
    fetch_article_urls,
)
from release_notes_scraper import fetch_release_notes


def collect_blog_articles(
    before_date: datetime,
    start_date: datetime,
    llm_client: "genai.Client | None",
) -> list:
    session = requests.Session()
    session.headers.update(HEADERS)

    article_urls = fetch_article_urls(session, start_date, before_date)
    articles = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {
            executor.submit(fetch_article_details, session, url, date, llm_client): (url, date)
            for url, date in article_urls
        }
        for future in concurrent.futures.as_completed(futures):
            result = future.result()
            if result:
                articles.append(result)
    return articles


def normalize_blog(article) -> dict:
    return {
        "source": "GCP Blog",
        "date": article.published_at.strftime("%Y-%m-%d"),
        "title": article.title,
        "url": article.url,
        "summary": article.summary,
        "products": article.products,
        "event_type": article.event_type,
        "event_subtype": article.event_subtype,
    }


def normalize_release_note(note) -> dict:
    return {
        "source": "GCP Release Notes",
        "date": note.date.strftime("%Y-%m-%d"),
        "title": note.title,
        "url": note.url,
        "summary": note.description,
        "products": [note.product] if note.product else [],
        "event_type": note.note_type,
        "event_subtype": "",
    }


def main():
    load_dotenv()

    parser = argparse.ArgumentParser(
        description="Fetch GCP Blog articles and Vertex AI release notes, combined into one JSON."
    )
    parser.add_argument("--date", required=True, help="End date in MM/DD/YYYY format")
    parser.add_argument("--no-enrich", action="store_true", help="Skip LLM metadata extraction for blog articles")
    parser.add_argument("--export-dir", default="export", help="Output directory (default: export/)")
    args = parser.parse_args()

    try:
        before_date = datetime.strptime(args.date, "%m/%d/%Y")
    except ValueError:
        print(f"Error: Invalid date '{args.date}'. Expected MM/DD/YYYY (e.g., 07/31/2025)")
        sys.exit(1)

    start_date = before_date - timedelta(weeks=LOOKBACK_WEEKS)

    llm_client = None
    if not args.no_enrich:
        api_key = os.environ.get("GOOGLE_API_KEY")
        if api_key:
            llm_client = genai.Client(api_key=api_key)
        else:
            print("Note: GOOGLE_API_KEY not set — skipping blog metadata extraction.\n")

    print(f"Period : {start_date.strftime('%m/%d/%Y')} → {before_date.strftime('%m/%d/%Y')}")
    print(f"Enrich : {'enabled' if llm_client else 'disabled'}\n")
    print("Fetching from both sources in parallel...", flush=True)

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        blog_future = executor.submit(collect_blog_articles, before_date, start_date, llm_client)
        rn_future = executor.submit(fetch_release_notes, start_date, before_date)
        blog_articles = blog_future.result()
        release_notes = rn_future.result()

    print(f"  GCP Blog          : {len(blog_articles)} article(s)")
    print(f"  GCP Release Notes : {len(release_notes)} entry/entries")

    combined = (
        [normalize_blog(a) for a in blog_articles]
        + [normalize_release_note(n) for n in release_notes]
    )
    combined.sort(key=lambda x: x["date"], reverse=True)

    print(f"  Combined total    : {len(combined)}\n")

    export_dir = Path(args.export_dir)
    export_dir.mkdir(parents=True, exist_ok=True)
    out_path = export_dir / f"combined_{before_date.strftime('%Y-%m-%d')}.json"
    out_path.write_text(json.dumps(combined, indent=2, ensure_ascii=False))
    print(f"Exported : {out_path}")


if __name__ == "__main__":
    main()
