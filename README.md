# FORRT Bibliography

A searchable, browsable view of [FORRT](https://forrt.org/) contributors' open-scholarship publications, powered by [OpenAlex](https://openalex.org/).

Rebuilds the [LMU OSC bibliometric page](https://www.resources.osc.lmu.de/bibliography/) with:

- **Per-publication browsing** (not just aggregate stats) with full-text search
- **FORRT cluster classification** — papers tagged against FORRT's Open Science cluster taxonomy
- **Dashboard** with yearly trends and a co-authorship network
- **Incremental updates** via GitHub Actions + committed SQLite cache

## Pipeline

```
Google Sheets (Tenzing)  -->  scripts/fetch_contributors.py  -->  data/contributors.csv
                                                                       |
                                                                       v
                              scripts/fetch_openalex.py         data/works.sqlite
FORRT Clusters Doc       -->  scripts/build_clusters.py   -->   data/clusters.yaml
                                                                       |
                                                                       v
                                       scripts/build_site.py   -->   site/data/*.json
```

## Local development

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e .
cp .env.example .env  # optional — only needed to pull fresh sheets

# Build from committed caches (no credentials required):
python scripts/build_clusters.py
python scripts/fetch_openalex.py --limit 20
python scripts/build_site.py

# Serve locally:
python -m http.server -d site 8000
```

## Refreshing contributor list

Credentials required (service-account JSON in `GSHEET_CREDENTIALS` env var).

```bash
python scripts/fetch_contributors.py
```

Without credentials, the script falls back to `data/contributors.csv` committed in the repo.

## Configuration

- `OPENALEX_MAILTO` — email for OpenAlex polite pool (default: `info@forrt.org`)
- `GSHEET_CREDENTIALS` — service account JSON (single-line) for Tenzing index sheet

## First-time GitHub setup

1. Push this repo to GitHub.
2. **Repository Settings → Pages** → Source: *GitHub Actions*.
3. **Repository Settings → Secrets and variables → Actions**:
   - Secret `GSHEET_CREDENTIALS`: the service-account JSON, pasted on a single
     line, for an account that has been shared read access on the private
     Tenzing INDEX sheet
     (`1MUD54FQUhfcBKrvr5gCYoh2wgbJ6Lf7oAJRAqsQ-Nag`).
   - (optional) Variable `OPENALEX_MAILTO`: contact email for OpenAlex's polite
     pool. Defaults to `info@forrt.org`.
4. Trigger the workflow manually once (Actions tab → *Update bibliography* →
   *Run workflow*) to populate the Pages deployment.

Subsequent runs happen weekly (Sunday 04:30 UTC). The OpenAlex SQLite cache
is stored via `actions/cache` so each run only fetches new or updated works.
The small JSON outputs (plus refreshed `contributors.csv`) are committed back
to the repo by a bot commit.
