#!/usr/bin/env python3
"""
Scraper for Vertex AI release notes.
Fetches entries published within 4 weeks before the specified date.

Usage:
    uv run release_notes_scraper.py --date 07/31/2025
    uv run release_notes_scraper.py --date 07/31/2025 --no-export
"""

import argparse
import csv
import json
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

RELEASE_NOTES_URL = "https://docs.cloud.google.com/vertex-ai/docs/release-notes"
LOOKBACK_WEEKS = int(os.getenv("LOOKBACK_WEEKS", 4))
HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}

# Google's own labels from the release notes page — kept as-is (authoritative source)
LABEL_MAP = {
    "Feature":      "feature",
    "Change":       "change",
    "Announcement": "announcement",
    "Deprecated":   "deprecated",
    "Fixed":        "fixed",
    "Breaking":     "breaking_change",
    "Security":     "security",
}


@dataclass
class ReleaseNote:
    date: datetime
    product: str
    note_type: str   # feature | change | announcement | deprecated | fixed | breaking_change | security
    title: str
    description: str
    url: str         # anchor link to the specific entry


def parse_date_header(h2) -> datetime | None:
    """Parse a date h2 element like id='April_17_2026' into a datetime."""
    text = h2.get_text(strip=True)
    try:
        return datetime.strptime(text, "%B %d, %Y")
    except ValueError:
        return None


def parse_entries_for_date(date: datetime, h2, base_url: str) -> list[ReleaseNote]:
    """Extract all release note entries that follow a date h2 until the next h2."""
    notes = []
    current_product = ""

    for sibling in h2.next_siblings:
        if sibling.name == "h2":
            break
        if not (hasattr(sibling, "name") and sibling.name):
            continue

        # Track the current product section
        if sibling.name == "strong" and "release-note-product-title" in (sibling.get("class") or []):
            current_product = sibling.get_text(strip=True)
            continue

        if sibling.name != "div" or "devsite-release-note" not in (sibling.get("class") or []):
            continue

        # --- label / note_type ---
        label_el = sibling.find("span", class_="devsite-label")
        raw_label = label_el.get_text(strip=True) if label_el else ""
        note_type = LABEL_MAP.get(raw_label, raw_label.lower())

        # --- title: first <strong> inside the content div ---
        content_div = sibling.find("div")
        title = ""
        if content_div:
            strong = content_div.find("strong")
            title = strong.get_text(strip=True) if strong else ""

        # --- description: all <p> text joined, consecutive title duplicate collapsed ---
        # Two HTML patterns exist:
        #   (a) <p><strong>Title</strong> body text...</p>  → single para, title in-line
        #   (b) <p><strong>Title</strong></p><p>Title body...</p> → two paras, title repeated
        # Joining (b) produces "Title Title body..."; collapse to "Title body...".
        paragraphs = sibling.find_all("p")
        description = " ".join(p.get_text(" ", strip=True) for p in paragraphs)
        description = re.sub(r"\s+", " ", description).strip()
        if title and description.startswith(f"{title} {title}"):
            description = description[len(title) + 1:]

        # --- anchor URL ---
        entry_id = sibling.get("id", "")
        url = f"{base_url}#{entry_id}" if entry_id else base_url

        notes.append(
            ReleaseNote(
                date=date,
                product=current_product,
                note_type=note_type,
                title=title,
                description=description,
                url=url,
            )
        )

    return notes


def fetch_release_notes(start_date: datetime, end_date: datetime) -> list[ReleaseNote]:
    """Fetch the release notes page and return entries within [start_date, end_date]."""
    r = requests.get(RELEASE_NOTES_URL, headers=HEADERS, timeout=30)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")

    notes = []
    date_id_pattern = re.compile(r"^[A-Z][a-z]+_\d+_\d{4}$")

    for h2 in soup.find_all("h2"):
        if not date_id_pattern.match(h2.get("id") or ""):
            continue

        date = parse_date_header(h2)
        if date is None:
            continue

        # Past the window — stop early (page is newest-first)
        if date < start_date:
            break

        if date <= end_date:
            notes.extend(parse_entries_for_date(date, h2, RELEASE_NOTES_URL))

    # Deduplicate by description — same note can appear under multiple product sections
    seen: set[str] = set()
    unique = []
    for note in notes:
        if note.description not in seen:
            seen.add(note.description)
            unique.append(note)
    return unique


def export_notes(notes: list[ReleaseNote], date_slug: str, export_dir: Path) -> None:
    """Export release notes to JSON and CSV in export_dir."""
    export_dir.mkdir(parents=True, exist_ok=True)

    # --- JSON ---
    json_path = export_dir / f"release_notes_{date_slug}.json"
    records = [
        {
            "date": n.date.strftime("%Y-%m-%d"),
            "product": n.product,
            "note_type": n.note_type,
            "title": n.title,
            "description": n.description,
            "url": n.url,
        }
        for n in notes
    ]
    json_path.write_text(json.dumps(records, indent=2, ensure_ascii=False))

    # --- CSV ---
    csv_path = export_dir / f"release_notes_{date_slug}.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=["date", "product", "note_type", "title", "description", "url"],
        )
        writer.writeheader()
        for n in notes:
            writer.writerow(
                {
                    "date": n.date.strftime("%Y-%m-%d"),
                    "product": n.product,
                    "note_type": n.note_type,
                    "title": n.title,
                    "description": n.description,
                    "url": n.url,
                }
            )

    print(f"Exported {len(notes)} entries:")
    print(f"  JSON : {json_path}")
    print(f"  CSV  : {csv_path}")


def main():
    load_dotenv()

    parser = argparse.ArgumentParser(
        description="Fetch Vertex AI release notes within 4 weeks before the specified date."
    )
    parser.add_argument("--date", required=True, help="End date in MM/DD/YYYY format")
    parser.add_argument("--no-export", action="store_true", help="Print results only, skip file export")
    parser.add_argument("--export-dir", default="export", help="Directory for exported files (default: export/)")
    args = parser.parse_args()

    try:
        before_date = datetime.strptime(args.date, "%m/%d/%Y")
    except ValueError:
        print(f"Error: Invalid date '{args.date}'. Expected MM/DD/YYYY (e.g., 07/31/2025)")
        sys.exit(1)

    start_date = before_date - timedelta(weeks=LOOKBACK_WEEKS)

    print("Vertex AI — Release Notes")
    print(f"Period : {start_date.strftime('%m/%d/%Y')} → {before_date.strftime('%m/%d/%Y')}")
    print(f"Source : {RELEASE_NOTES_URL}\n")

    print("Fetching release notes page...", flush=True)
    notes = fetch_release_notes(start_date, before_date)

    if not notes:
        print("No release notes found in the specified period.")
        return

    print(f"Found {len(notes)} entries:\n")
    for i, note in enumerate(notes, 1):
        print(f"{i}. [{note.date.strftime('%Y-%m-%d')}] [{note.note_type}] {note.title or '(no title)'}")
        print(f"   product : {note.product}")
        if note.description:
            desc = note.description[:200] + "..." if len(note.description) > 200 else note.description
            print(f"   desc    : {desc}")
        print(f"   url     : {note.url}")
        print()

    if not args.no_export:
        date_slug = before_date.strftime("%Y-%m-%d")
        export_notes(notes, date_slug, Path(args.export_dir))


if __name__ == "__main__":
    main()
