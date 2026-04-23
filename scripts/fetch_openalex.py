"""Fetch FORRT contributors' publications from OpenAlex and cache in SQLite.

For every contributor ORCID in `data/contributors.csv`:
  1. Resolve to an OpenAlex Author (`/authors/orcid:<ORCID>`).
  2. If the author's `updated_date` has not changed since our last run, skip.
  3. Otherwise, paginate `/works?filter=author.id:<AID>` via cursor, upserting
     each work into the local SQLite DB.

Raw works are stored verbatim so the classifier can be re-run locally without
refetching. The DB is committed to the repo, giving cheap incremental runs
in GitHub Actions.

Usage:
  python scripts/fetch_openalex.py [--limit N] [--force] [--orcid ORCID]
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "works.sqlite"
CONTRIBUTORS_PATH = ROOT / "data" / "contributors.csv"

API_BASE = "https://api.openalex.org"
MAILTO = os.environ.get("OPENALEX_MAILTO", "info@forrt.org")
USER_AGENT = f"forrt-bibliography/0.1 (mailto:{MAILTO})"
PAGE_SIZE = 200

# Select only the fields we use — cuts response size by >60%.
WORK_SELECT = ",".join([
    "id", "doi", "title", "display_name", "publication_year", "publication_date",
    "type", "cited_by_count", "open_access", "abstract_inverted_index",
    "primary_location", "authorships", "updated_date", "language",
])


SCHEMA = """
CREATE TABLE IF NOT EXISTS authors (
    orcid TEXT PRIMARY KEY,
    openalex_id TEXT,
    display_name TEXT,
    works_count INTEGER,
    updated_date TEXT,
    last_fetched_at TEXT,
    last_error TEXT
);

CREATE TABLE IF NOT EXISTS works (
    openalex_id TEXT PRIMARY KEY,
    doi TEXT,
    title TEXT,
    abstract TEXT,
    publication_year INTEGER,
    publication_date TEXT,
    work_type TEXT,
    language TEXT,
    cited_by_count INTEGER,
    is_oa INTEGER,
    oa_status TEXT,
    oa_url TEXT,
    venue TEXT,
    updated_date TEXT,
    authors_json TEXT
);

CREATE INDEX IF NOT EXISTS ix_works_year ON works(publication_year);

CREATE TABLE IF NOT EXISTS work_authors (
    work_id TEXT NOT NULL,
    contributor_orcid TEXT NOT NULL,
    author_position TEXT,
    PRIMARY KEY (work_id, contributor_orcid)
);

CREATE INDEX IF NOT EXISTS ix_work_authors_orcid ON work_authors(contributor_orcid);
"""


def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    return con


def make_session() -> requests.Session:
    s = requests.Session()
    s.headers["User-Agent"] = USER_AGENT
    return s


def reconstruct_abstract(inverted: dict | None) -> str:
    if not inverted:
        return ""
    # Flatten {word: [positions]} into ordered words.
    positions: dict[int, str] = {}
    for word, idxs in inverted.items():
        for i in idxs:
            positions[i] = word
    return " ".join(positions[i] for i in sorted(positions))


def get_json(session: requests.Session, url: str, params: dict | None = None,
             retries: int = 4) -> dict:
    params = {**(params or {}), "mailto": MAILTO}
    for attempt in range(retries):
        try:
            r = session.get(url, params=params, timeout=30)
            if r.status_code == 429:
                time.sleep(2 ** attempt)
                continue
            r.raise_for_status()
            return r.json()
        except requests.RequestException as e:
            if attempt == retries - 1:
                raise
            time.sleep(2 ** attempt)
    raise RuntimeError("unreachable")


def resolve_author(session: requests.Session, orcid: str) -> dict | None:
    try:
        return get_json(session, f"{API_BASE}/authors/orcid:{orcid}")
    except requests.HTTPError as e:
        if e.response is not None and e.response.status_code == 404:
            return None
        raise


def iter_works(session: requests.Session, author_id: str):
    """Paginate over all works for an author using cursor pagination."""
    short_id = author_id.rsplit("/", 1)[-1]
    cursor = "*"
    while cursor:
        page = get_json(session, f"{API_BASE}/works", params={
            "filter": f"author.id:{short_id}",
            "per-page": PAGE_SIZE,
            "cursor": cursor,
            "select": WORK_SELECT,
        })
        for work in page.get("results", []):
            yield work
        cursor = page.get("meta", {}).get("next_cursor")


def upsert_work(con: sqlite3.Connection, work: dict) -> list[dict]:
    oa = work.get("open_access") or {}
    primary = work.get("primary_location") or {}
    source = (primary.get("source") or {}) if primary else {}

    authorships = []
    for a in work.get("authorships", []) or []:
        author = a.get("author") or {}
        authorships.append({
            "name": author.get("display_name") or "",
            "openalex_id": (author.get("id") or "").rsplit("/", 1)[-1],
            "orcid": (author.get("orcid") or "").rsplit("/", 1)[-1] if author.get("orcid") else "",
            "position": a.get("author_position") or "",
            "is_corresponding": bool(a.get("is_corresponding")),
            "institutions": [
                (i.get("display_name") or "") for i in (a.get("institutions") or [])
            ],
        })

    con.execute(
        """INSERT INTO works (openalex_id, doi, title, abstract, publication_year,
                               publication_date, work_type, language, cited_by_count,
                               is_oa, oa_status, oa_url, venue, updated_date,
                               authors_json)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(openalex_id) DO UPDATE SET
               doi=excluded.doi, title=excluded.title, abstract=excluded.abstract,
               publication_year=excluded.publication_year,
               publication_date=excluded.publication_date,
               work_type=excluded.work_type, language=excluded.language,
               cited_by_count=excluded.cited_by_count, is_oa=excluded.is_oa,
               oa_status=excluded.oa_status, oa_url=excluded.oa_url,
               venue=excluded.venue, updated_date=excluded.updated_date,
               authors_json=excluded.authors_json
           WHERE excluded.updated_date IS NOT NULL
             AND (works.updated_date IS NULL OR excluded.updated_date >= works.updated_date)
        """,
        (
            (work.get("id") or "").rsplit("/", 1)[-1],
            (work.get("doi") or "").replace("https://doi.org/", "") or None,
            work.get("title") or work.get("display_name"),
            reconstruct_abstract(work.get("abstract_inverted_index")),
            work.get("publication_year"),
            work.get("publication_date"),
            work.get("type"),
            work.get("language"),
            work.get("cited_by_count") or 0,
            1 if oa.get("is_oa") else 0,
            oa.get("oa_status"),
            oa.get("oa_url"),
            source.get("display_name"),
            work.get("updated_date"),
            json.dumps(authorships, ensure_ascii=False),
        ),
    )
    return authorships


def link_contributors(con: sqlite3.Connection, work_id: str,
                      authors: list[dict], contributor_orcids: set[str]) -> None:
    for a in authors:
        orcid = a.get("orcid") or ""
        if orcid and orcid in contributor_orcids:
            con.execute(
                """INSERT OR IGNORE INTO work_authors (work_id, contributor_orcid,
                                                      author_position)
                   VALUES (?,?,?)""",
                (work_id, orcid, a.get("position") or ""),
            )


def load_contributors() -> list[dict]:
    if not CONTRIBUTORS_PATH.exists():
        raise SystemExit(f"Missing {CONTRIBUTORS_PATH}; run fetch_contributors.py first.")
    with CONTRIBUTORS_PATH.open() as fh:
        return [r for r in csv.DictReader(fh) if r.get("orcid")]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, help="Process at most N contributors")
    ap.add_argument("--orcid", action="append", help="Restrict to given ORCID(s)")
    ap.add_argument("--force", action="store_true",
                    help="Ignore cached author updated_date — refetch all works")
    args = ap.parse_args()

    contributors = load_contributors()
    if args.orcid:
        wanted = set(args.orcid)
        contributors = [c for c in contributors if c["orcid"] in wanted]
    if args.limit:
        contributors = contributors[: args.limit]

    print(f"Processing {len(contributors)} contributors (mailto={MAILTO})")
    contributor_orcids = {c["orcid"] for c in contributors}

    con = connect()
    session = make_session()

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    total_works_seen = 0
    skipped = 0

    for i, c in enumerate(contributors, 1):
        orcid = c["orcid"]
        name = c["full_name"]
        row = con.execute("SELECT openalex_id, updated_date FROM authors WHERE orcid = ?",
                          (orcid,)).fetchone()

        try:
            author = resolve_author(session, orcid)
        except Exception as e:
            print(f"[{i}/{len(contributors)}] {name} ({orcid}): resolve failed: {e}",
                  file=sys.stderr)
            con.execute(
                "INSERT OR REPLACE INTO authors (orcid, last_fetched_at, last_error) "
                "VALUES (?,?,?)", (orcid, now, str(e)))
            con.commit()
            continue

        if author is None:
            print(f"[{i}/{len(contributors)}] {name} ({orcid}): no OpenAlex author")
            con.execute(
                "INSERT OR REPLACE INTO authors (orcid, last_fetched_at, last_error) "
                "VALUES (?,?,?)", (orcid, now, "not_found"))
            con.commit()
            continue

        aid = author.get("id") or ""
        updated = author.get("updated_date")
        works_count = author.get("works_count") or 0

        if (not args.force
                and row is not None
                and row["openalex_id"] == aid
                and row["updated_date"] == updated
                and updated is not None):
            print(f"[{i}/{len(contributors)}] {name}: unchanged (cached), "
                  f"{works_count} works")
            skipped += 1
            con.execute(
                "UPDATE authors SET last_fetched_at = ?, last_error = NULL "
                "WHERE orcid = ?", (now, orcid))
            con.commit()
            continue

        print(f"[{i}/{len(contributors)}] {name}: fetching {works_count} works …")
        fetched = 0
        try:
            for work in iter_works(session, aid):
                authorships = upsert_work(con, work)
                link_contributors(con, (work.get("id") or "").rsplit("/", 1)[-1],
                                  authorships, contributor_orcids)
                fetched += 1
                if fetched % 500 == 0:
                    con.commit()
        except Exception as e:
            print(f"  ERROR mid-fetch: {e}", file=sys.stderr)
            con.execute(
                "INSERT OR REPLACE INTO authors (orcid, openalex_id, display_name, "
                "works_count, updated_date, last_fetched_at, last_error) "
                "VALUES (?,?,?,?,?,?,?)",
                (orcid, aid, author.get("display_name"), works_count,
                 updated, now, str(e)))
            con.commit()
            continue

        total_works_seen += fetched
        con.execute(
            "INSERT OR REPLACE INTO authors (orcid, openalex_id, display_name, "
            "works_count, updated_date, last_fetched_at, last_error) "
            "VALUES (?,?,?,?,?,?,NULL)",
            (orcid, aid, author.get("display_name"), works_count, updated, now))
        con.commit()

    print(f"\nDone. {skipped} authors unchanged, "
          f"{len(contributors) - skipped} refreshed, {total_works_seen} works fetched this run.")
    total_works = con.execute("SELECT COUNT(*) AS n FROM works").fetchone()["n"]
    total_links = con.execute("SELECT COUNT(*) AS n FROM work_authors").fetchone()["n"]
    print(f"DB totals: {total_works} works, {total_links} contributor-links.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
