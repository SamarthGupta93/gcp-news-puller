#!/usr/bin/env python3
"""
Historical news puller using Google Cloud Blog's date-partitioned sitemaps.
Handles any date range back to 2014, unlike main.py which is limited to
the most recent ~20 articles in the RSS feed.

Usage:
    uv run scraper.py --date 07/31/2025              # with LLM metadata enrichment
    uv run scraper.py --date 07/31/2025 --no-enrich  # skip metadata extraction
"""

import argparse
import concurrent.futures
import csv
import json
import os
import sys
from calendar import monthrange
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from xml.etree import ElementTree as ET

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from google import genai

SITEMAP_URL = "https://cloud.google.com/transform/sitemap/cloudblog/en/{start}/{end}"
# Strict prefix: only URLs that begin with this path are included
BLOG_URL_PREFIX = "https://cloud.google.com/blog/products/ai-machine-learning/"
LOOKBACK_WEEKS = int(os.getenv("LOOKBACK_WEEKS", 4))
MAX_WORKERS = int(os.getenv("MAX_WORKERS", 10))
HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}

# Base types — aligned with Google's Vertex AI release notes labels so both
# sources share a common vocabulary for the ML model.
EVENT_TYPES = [
    "feature",          # new capability added to a product
    "announcement",     # major product milestone or launch
    "change",           # behavioral, config, or API change
    "deprecated",       # end-of-life or removal notice
    "breaking_change",  # breaking change to API or behavior
    "fixed",            # bug fix
    "security",         # security update
    # Blog-specific types (no direct equivalent in release notes)
    "case_study",       # customer story
    "guide",            # tutorial or how-to
    "research",         # research publication
    "event_recap",      # conference/event announcement or recap
    "integration",      # new partner or product integration
    "other",
]

# Subtypes add precision where event_type alone is ambiguous.
# Most useful for "feature" and "announcement": was it GA, preview, pricing, etc.
# Ticket-volume signal interpretation:
#   ga_release      → broad adoption → tickets spike 4–8 wks out
#   preview_release → early adopters → lower immediate volume
#   pricing_change  → billing questions → immediate spike
#   breaking_change → migration/integration issues → spike within days
EVENT_SUBTYPES = [
    "ga_release",       # generally available
    "preview_release",  # preview, beta, or experimental
    "pricing_change",   # pricing or quota update
    "model_release",    # new model launch
    "new_region",       # regional availability expansion
    "sdk_update",       # SDK or client library update
    "quota_change",     # quota increase or decrease
    "partnership",      # new partner or ecosystem integration
    "benchmark",        # performance or capability benchmark
]


@dataclass
class Article:
    title: str
    url: str
    published_at: datetime
    summary: str
    products: list[str] = field(default_factory=list)
    event_type: str = "other"
    event_subtype: str = ""   # optional precision on top of event_type (e.g. ga_release, preview_release)


def get_partitions(start_date: datetime, end_date: datetime) -> list[tuple[str, str]]:
    """Return (start, end) date strings for 2-week sitemap partitions that overlap [start_date, end_date]."""
    partitions = []
    year, month = start_date.year, start_date.month

    while (year, month) <= (end_date.year, end_date.month):
        last_day = monthrange(year, month)[1]

        p1_start = datetime(year, month, 1)
        p1_end = datetime(year, month, 15)
        if p1_start <= end_date and p1_end >= start_date:
            partitions.append((p1_start.strftime("%Y-%m-%d"), p1_end.strftime("%Y-%m-%d")))

        p2_start = datetime(year, month, 16)
        p2_end = datetime(year, month, last_day)
        if p2_start <= end_date and p2_end >= start_date:
            partitions.append((p2_start.strftime("%Y-%m-%d"), p2_end.strftime("%Y-%m-%d")))

        month = month % 12 + 1
        if month == 1:
            year += 1

    return partitions


def fetch_article_urls(
    session: requests.Session, start_date: datetime, end_date: datetime
) -> list[tuple[str, datetime]]:
    """Return (url, pub_date) pairs for ai-machine-learning articles in [start_date, end_date]."""
    ns = {"s": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    articles = []

    for p_start, p_end in get_partitions(start_date, end_date):
        url = SITEMAP_URL.format(start=p_start, end=p_end)
        try:
            r = session.get(url, timeout=15)
            r.raise_for_status()
            root = ET.fromstring(r.text)
            for u in root.findall("s:url", ns):
                loc_el = u.find("s:loc", ns)
                mod_el = u.find("s:lastmod", ns)
                loc = loc_el.text if loc_el is not None else ""
                # Strict: only /blog/products/ai-machine-learning/ — no other product sections
                if not loc.startswith(BLOG_URL_PREFIX):
                    continue
                if mod_el is None:
                    continue
                pub_date = datetime.strptime(mod_el.text[:10], "%Y-%m-%d")
                if start_date <= pub_date <= end_date:
                    articles.append((loc, pub_date))
        except Exception as e:
            print(f"Warning: Could not fetch sitemap for {p_start}–{p_end}: {e}", file=sys.stderr)

    return articles


def extract_metadata(
    client: "genai.Client", title: str, summary: str
) -> tuple[list[str], str, str]:
    """Use Gemini Flash to extract products, event_type, and event_subtype."""
    prompt = f"""Extract metadata from this Google Cloud Blog article about AI/ML.

Title: {title}
Description: {summary}

Return a JSON object with exactly these fields:
- "products": list of Google Cloud product names explicitly mentioned (e.g. ["Vertex AI", "BigQuery ML", "Cloud Run"])
- "event_type": one of {json.dumps(EVENT_TYPES)}
- "event_subtype": one of {json.dumps(EVENT_SUBTYPES)}, or "" if none applies.
  Use this to add precision when event_type alone is ambiguous — for example, a "feature"
  event_type should use "ga_release" or "preview_release" as the subtype when the text
  makes it clear which stage the release is at.

Return ONLY valid JSON, no explanation."""

    try:
        response = client.models.generate_content(
            model=os.environ.get("GOOGLE_MODEL", "gemini-2.5-flash-lite"),
            contents=prompt,
        )
        text = response.text.strip()
        if text.startswith("```"):
            text = text.split("```")[1].lstrip("json").strip()
        data = json.loads(text)
        products = data.get("products", [])
        event_type = data.get("event_type", "other")
        event_subtype = data.get("event_subtype", "")
        if event_type not in EVENT_TYPES:
            event_type = "other"
        if event_subtype not in EVENT_SUBTYPES:
            event_subtype = ""
        return products, event_type, event_subtype
    except Exception:
        return [], "other", ""


def fetch_article_details(
    session: requests.Session,
    url: str,
    pub_date: datetime,
    llm_client: "genai.Client | None" = None,
) -> Article | None:
    """Fetch an article page, extract structured fields, optionally enrich with LLM metadata."""
    try:
        r = session.get(url, timeout=15)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")

        title = ""
        summary = ""
        for script in soup.find_all("script", type="application/ld+json"):
            try:
                data = json.loads(script.string)
                if data.get("@type") == "BlogPosting":
                    title = data.get("headline", "")
                    summary = data.get("description", "")
                    break
            except Exception:
                pass

        if not title:
            title_tag = soup.find("title")
            title = title_tag.get_text(strip=True).split("|")[0].strip() if title_tag else url.split("/")[-1]

        if len(summary) > 300:
            summary = summary[:300] + "..."

        article = Article(title=title, url=url, published_at=pub_date, summary=summary)

        if llm_client and title:
            article.products, article.event_type, article.event_subtype = extract_metadata(llm_client, title, summary)

        return article
    except Exception:
        return None


def export_articles(articles: list[Article], date_slug: str, export_dir: Path) -> None:
    """Export articles to JSON and CSV in export_dir."""
    export_dir.mkdir(parents=True, exist_ok=True)

    # --- JSON (full fidelity, list fields preserved) ---
    json_path = export_dir / f"articles_{date_slug}.json"
    records = [
        {
            "title": a.title,
            "url": a.url,
            "published_at": a.published_at.strftime("%Y-%m-%d"),
            "summary": a.summary,
            "products": a.products,
            "event_type": a.event_type,
            "event_subtype": a.event_subtype,
        }
        for a in articles
    ]
    json_path.write_text(json.dumps(records, indent=2, ensure_ascii=False))

    # --- CSV (flat, products joined for spreadsheet review) ---
    csv_path = export_dir / f"articles_{date_slug}.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=["published_at", "title", "event_type", "event_subtype", "products", "summary", "url"],
        )
        writer.writeheader()
        for a in articles:
            writer.writerow(
                {
                    "published_at": a.published_at.strftime("%Y-%m-%d"),
                    "title": a.title,
                    "event_type": a.event_type,
                    "event_subtype": a.event_subtype,
                    "products": ", ".join(a.products),
                    "summary": a.summary,
                    "url": a.url,
                }
            )

    print(f"Exported {len(articles)} articles:")
    print(f"  JSON : {json_path}")
    print(f"  CSV  : {csv_path}")


def main():
    parser = argparse.ArgumentParser(
        description="Pull AI/ML articles from Google Cloud Blog for any historical date range."
    )
    parser.add_argument("--date", required=True, help="End date in MM/DD/YYYY format")
    parser.add_argument("--no-enrich", action="store_true", help="Skip LLM metadata extraction")
    parser.add_argument("--export-dir", default="export", help="Directory for exported files (default: export/)")
    args = parser.parse_args()

    load_dotenv()

    try:
        before_date = datetime.strptime(args.date, "%m/%d/%Y")
    except ValueError:
        print(f"Error: Invalid date '{args.date}'. Expected MM/DD/YYYY (e.g., 07/31/2025)")
        sys.exit(1)

    start_date = before_date - timedelta(weeks=LOOKBACK_WEEKS)

    # Set up LLM client if enrichment is requested and key is available
    llm_client = None
    if not args.no_enrich:
        api_key = os.environ.get("GOOGLE_API_KEY")
        if api_key:
            llm_client = genai.Client(api_key=api_key)
        else:
            print("Note: GOOGLE_API_KEY not set — skipping metadata extraction (use --no-enrich to suppress).\n")

    print("Google Cloud Blog — AI & Machine Learning")
    print(f"Period  : {start_date.strftime('%m/%d/%Y')} → {before_date.strftime('%m/%d/%Y')}")
    print(f"Enrich  : {'products + event_type via gemini-2.5-flash-lite' if llm_client else 'disabled'}\n")

    session = requests.Session()
    session.headers.update(HEADERS)

    print("Scanning sitemaps...", flush=True)
    article_urls = fetch_article_urls(session, start_date, before_date)

    if not article_urls:
        print("No articles found in the specified period.")
        return

    label = "Fetching details + extracting metadata" if llm_client else "Fetching details"
    print(f"Found {len(article_urls)} article URLs. {label}...", flush=True)

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

    articles.sort(key=lambda x: x.published_at, reverse=True)

    print(f"\nFound {len(articles)} article(s):\n")
    for i, article in enumerate(articles, 1):
        pub = article.published_at.strftime("%Y-%m-%d")
        print(f"{i}. [{pub}] {article.title}")
        print(f"   url      : {article.url}")
        if article.summary:
            print(f"   summary  : {article.summary}")
        if llm_client:
            print(f"   products : {', '.join(article.products) if article.products else '—'}")
            event = article.event_type
            if article.event_subtype:
                event += f" / {article.event_subtype}"
            print(f"   event    : {event}")
        print()

    date_slug = before_date.strftime("%Y-%m-%d")
    export_articles(articles, date_slug, Path(args.export_dir))


if __name__ == "__main__":
    main()
