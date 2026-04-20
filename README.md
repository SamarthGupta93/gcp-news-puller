# news-puller

Fetches Google Cloud AI/ML signals from two sources and combines them into a single dated JSON file for downstream use (e.g. as features in a ticket-volume forecasting model).

**Sources**
- **GCP Blog** — `cloud.google.com/blog/products/ai-machine-learning` (via date-partitioned sitemaps, full history back to 2014)
- **GCP Release Notes** — `docs.cloud.google.com/vertex-ai/docs/release-notes` (static HTML, full history)

---

## Prerequisites

| Tool | Version | Install |
|---|---|---|
| Python | ≥ 3.14 | [python.org](https://www.python.org/downloads/) |
| uv | any | `curl -LsSf https://astral.sh/uv/install.sh \| sh` |

---

## Setup

```bash
git clone <repo-url>
cd news-puller

# Install dependencies into an isolated virtualenv
uv sync
```

A `.env` file is included in the repo with `GOOGLE_API_KEY` left blank. Fill it in before running:

```
GOOGLE_API_KEY=your_google_api_key_here
```

Obtain a key from [Google AI Studio](https://aistudio.google.com/app/apikey). It is used to call Gemini for metadata enrichment (classifying `event_type` and `event_subtype` on blog articles).

> Without a key the tool still works — blog articles will have `event_type: "other"` and empty `products`. Use `--no-enrich` to suppress the warning.

---

## Usage

### Unified runner (recommended)

Runs both scrapers in parallel and writes a single combined JSON:

```bash
uv run main.py --date 07/31/2025
```

Output: `export/combined_2025-07-31.json`

| Flag | Default | Description |
|---|---|---|
| `--date` | required | End date in `MM/DD/YYYY` format. Results cover the 4 weeks before this date. |
| `--no-enrich` | off | Skip Gemini metadata extraction for blog articles. |
| `--export-dir` | `export/` | Directory to write output files. |

---

### Individual scrapers

Run each scraper independently if you only need one source.

**GCP Blog** (date-partitioned sitemaps + optional LLM enrichment):

```bash
uv run gcp_blog_scraper.py --date 07/31/2025
uv run gcp_blog_scraper.py --date 07/31/2025 --no-enrich
```

Output: `export/articles_2025-07-31.json` and `export/articles_2025-07-31.csv`

**GCP Release Notes** (static HTML scraper, no LLM needed):

```bash
uv run release_notes_scraper.py --date 07/31/2025
uv run release_notes_scraper.py --date 07/31/2025 --no-export   # print only
```

Output: `export/release_notes_2025-07-31.json` and `export/release_notes_2025-07-31.csv`

---

## Output schema

All three scripts produce records with a shared schema. The unified `combined_*.json` adds a `source` field.

```json
{
  "source":        "GCP Blog | GCP Release Notes",
  "date":          "YYYY-MM-DD",
  "title":         "Vertex AI Memory Bank in public preview",
  "url":           "https://...",
  "summary":       "Announcing Vertex AI Memory Bank...",
  "products":      ["Vertex AI"],
  "event_type":    "announcement",
  "event_subtype": "preview_release"
}
```

### `event_type` values

Both sources share a common vocabulary. Blog articles use Gemini to classify; release notes use Google's own labels mapped to the same values.

| Value | Description | Ticket signal |
|---|---|---|
| `feature` | New capability added | Moderate lag (users adopt) |
| `announcement` | Major product milestone or GA launch | Adoption-driven spike 4–8 wks out |
| `change` | Behavioral or config change | Low–medium |
| `deprecated` | End-of-life notice | Spike near deadline |
| `breaking_change` | Breaking API/behavior change | Spike within days |
| `fixed` | Bug fix | Low |
| `security` | Security update | Low–medium |
| `case_study` | Customer story | Low |
| `guide` | Tutorial or how-to | Low |
| `research` | Research publication | Low |
| `event_recap` | Conference announcement or recap | Low |
| `integration` | New partner or product integration | Moderate |
| `other` | — | — |

### `event_subtype` values (blog only)

Adds precision on top of `event_type`, most useful for `feature` and `announcement`.

`ga_release` · `preview_release` · `pricing_change` · `model_release` · `new_region` · `sdk_update` · `quota_change` · `partnership` · `benchmark`

---

## Project structure

```
news-puller/
├── main.py                   # Unified runner — runs both scrapers, exports combined JSON
├── gcp_blog_scraper.py       # GCP Blog scraper (sitemaps + Gemini enrichment)
├── release_notes_scraper.py  # Vertex AI release notes scraper (HTML)
├── rss.py                    # Lightweight RSS-only scraper (recent articles, no LLM)
├── export/                   # Output directory (auto-created on first run)
│   ├── combined_YYYY-MM-DD.json
│   ├── articles_YYYY-MM-DD.json
│   ├── articles_YYYY-MM-DD.csv
│   ├── release_notes_YYYY-MM-DD.json
│   └── release_notes_YYYY-MM-DD.csv
├── .env                      # API keys (not committed)
├── pyproject.toml
└── uv.lock
```

---

## Environment variables

| Variable | Required | Description |
|---|---|---|
| `GOOGLE_API_KEY` | For enrichment | Gemini Flash API key for blog `event_type` classification |
| `GOOGLE_MODEL` | No | Override the Gemini model (default: `gemini-2.5-flash-lite`) |
| `LOOKBACK_WEEKS` | No | Number of weeks to look back (default: `4`) |
| `MAX_WORKERS` | No | Concurrent HTTP threads for blog scraper (default: `10`) |
