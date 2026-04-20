#!/usr/bin/env python3
"""
Pulls AI/ML news articles from the Google Cloud Blog RSS feed.
The RSS feed only includes summaries of the most recent ~20 posts, 
so this script is designed to be run weekly to build a historical dataset.
"""
import argparse
import sys
from datetime import datetime, timedelta

import feedparser

RSS_FEED_URL = "https://cloudblog.withgoogle.com/products/ai-machine-learning/rss/"
LOOKBACK_WEEKS = 4


def fetch_articles(before_date: datetime) -> list[dict]:
    start_date = before_date - timedelta(weeks=LOOKBACK_WEEKS)

    feed = feedparser.parse(RSS_FEED_URL)

    if feed.bozo and not feed.entries:
        print(f"Error: Failed to fetch RSS feed from {RSS_FEED_URL}", file=sys.stderr)
        print(f"Reason: {feed.bozo_exception}", file=sys.stderr)
        sys.exit(1)

    articles = []
    for entry in feed.entries:
        if not hasattr(entry, "published_parsed") or entry.published_parsed is None:
            continue

        published = datetime(*entry.published_parsed[:6])

        if start_date <= published <= before_date:
            summary = entry.get("summary", "")
            # Strip any HTML tags from summary (feedparser may include them)
            import re
            summary = re.sub(r"<[^>]+>", "", summary).strip()
            if len(summary) > 200:
                summary = summary[:200] + "..."

            articles.append({
                "title": entry.title,
                "url": entry.link,
                "published": published,
                "summary": summary,
            })

    return sorted(articles, key=lambda x: x["published"], reverse=True)


def main():
    parser = argparse.ArgumentParser(
        description="Pull AI/ML news articles from Google Cloud Blog within a 4-week window."
    )
    parser.add_argument(
        "--date",
        required=True,
        help="End date (articles published on or before this date). Format: MM/DD/YYYY",
    )
    args = parser.parse_args()

    try:
        before_date = datetime.strptime(args.date, "%m/%d/%Y")
    except ValueError:
        print(f"Error: Invalid date '{args.date}'. Expected format: MM/DD/YYYY (e.g., 07/31/2025)")
        sys.exit(1)

    start_date = before_date - timedelta(weeks=LOOKBACK_WEEKS)
    print(f"Google Cloud Blog — AI & Machine Learning")
    print(f"Period : {start_date.strftime('%m/%d/%Y')} → {before_date.strftime('%m/%d/%Y')}")
    print(f"Source : {RSS_FEED_URL}\n")

    articles = fetch_articles(before_date)

    if not articles:
        print("No articles found in the specified period.")
        return

    print(f"Found {len(articles)} article(s):\n")
    for i, article in enumerate(articles, 1):
        pub = article["published"].strftime("%Y-%m-%d")
        print(f"{i}. [{pub}] {article['title']}")
        print(f"   {article['url']}")
        if article["summary"]:
            print(f"   {article['summary']}")
        print()


if __name__ == "__main__":
    main()
