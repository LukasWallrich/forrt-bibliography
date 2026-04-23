"""Classify works and emit site data JSON.

Reads `data/works.sqlite` and `data/clusters.yaml`, classifies each work by
keyword matching against title + abstract, and writes:

  site/data/works.json        — open-scholarship works only (for table view)
  site/data/contributors.json — FORRT contributors with per-author stats
  site/data/stats.json        — aggregate counts, year trends, cluster totals
  site/data/network.json      — co-authorship graph between FORRT contributors
  site/data/clusters.json     — cluster taxonomy (for filter UI)
  site/data/meta.json         — generation timestamp, totals, data sources
"""

from __future__ import annotations

import json
import re
import sqlite3
import unicodedata
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "works.sqlite"
CLUSTERS_PATH = ROOT / "data" / "clusters.yaml"
OUT_DIR = ROOT / "site" / "data"


def compile_keyword_regex(keywords: list[str]) -> re.Pattern:
    """Build a single regex that matches any of `keywords` at word boundaries.

    Multi-word phrases are joined by `\\s+` (so newlines / double spaces in
    the source still match). Hyphens are treated as literal non-word chars;
    that's fine because we search lower-cased text.
    """
    parts = []
    for kw in keywords:
        tokens = [re.escape(t) for t in kw.split()]
        parts.append(r"\b" + r"\s+".join(tokens) + r"\b")
    return re.compile("(?:" + "|".join(parts) + ")", re.IGNORECASE)


def classify_text(text: str, cluster_regexes: list[tuple[int, re.Pattern]]) -> list[int]:
    """Return the cluster numbers whose keywords match anywhere in `text`."""
    return [num for num, rx in cluster_regexes if rx.search(text)]


def load_clusters() -> dict:
    with CLUSTERS_PATH.open() as fh:
        return yaml.safe_load(fh)


def normalize_title(t: str | None) -> str:
    if not t:
        return ""
    t = unicodedata.normalize("NFKD", t).encode("ascii", "ignore").decode()
    t = t.lower()
    t = re.sub(r"[^\w\s]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def _author_keys(authorships: list[dict]) -> set[str]:
    """A coarse author-identity set for overlap comparison: ORCID when
    available, otherwise normalized surname (last whitespace-separated token)."""
    keys: set[str] = set()
    for a in authorships:
        orcid = (a.get("orcid") or "").strip()
        if orcid:
            keys.add(orcid)
            continue
        name = (a.get("name") or "").strip()
        if name:
            surname = name.split()[-1]
            keys.add(
                unicodedata.normalize("NFKD", surname).encode("ascii", "ignore").decode().lower()
            )
    return keys


def deduplicate(works: list[sqlite3.Row]) -> tuple[list[dict], dict]:
    """Drop duplicate records sharing a normalised title + author identity.

    Within each group of works with the same normalised title:
      * Keep the "canonical" member — the one with the highest citation count
        (ties broken by non-preprint first, then by openalex_id for determinism).
      * For every other member that shares at least one author identity
        (ORCID or surname) with the canonical, drop it when either:
          (a) it is a preprint and the canonical is not (preprint ↔ journal);
          (b) it is a preprint and the canonical is also a preprint
              (duplicate preprint postings);
          (c) it has zero citations (zero-cite mirror / repository copy).
      * Members with no author overlap with the canonical are kept — different
        papers that happen to share a title.

    Returns (surviving_rows, stats).
    """
    rows = []
    for w in works:
        auths = json.loads(w["authors_json"] or "[]")
        rows.append({
            "row": w,
            "title_norm": normalize_title(w["title"]),
            "is_preprint": (w["work_type"] or "") == "preprint",
            "cited": w["cited_by_count"] or 0,
            "authors": _author_keys(auths),
        })

    groups: defaultdict[str, list[dict]] = defaultdict(list)
    untitled: list[dict] = []
    for r in rows:
        (groups[r["title_norm"]] if r["title_norm"] else untitled).append(r)

    dropped_pp_vs_journal = 0
    dropped_pp_vs_pp = 0
    dropped_zero_cite_mirror = 0
    keep_ids: set[str] = {r["row"]["openalex_id"] for r in untitled}

    def _rank(m: dict):
        # Higher cited first; among equal citations, non-preprint first;
        # final tie-break on openalex id for determinism.
        return (-m["cited"], 1 if m["is_preprint"] else 0, m["row"]["openalex_id"])

    for title, members in groups.items():
        if len(members) == 1:
            keep_ids.add(members[0]["row"]["openalex_id"])
            continue

        members_sorted = sorted(members, key=_rank)
        canonical = members_sorted[0]
        keep_ids.add(canonical["row"]["openalex_id"])

        for other in members_sorted[1:]:
            overlaps = bool(other["authors"] & canonical["authors"])
            if not overlaps:
                keep_ids.add(other["row"]["openalex_id"])
                continue
            if other["is_preprint"] and not canonical["is_preprint"]:
                dropped_pp_vs_journal += 1
            elif other["is_preprint"] and canonical["is_preprint"]:
                dropped_pp_vs_pp += 1
            elif other["cited"] == 0:
                dropped_zero_cite_mirror += 1
            else:
                # Both non-preprint with citations on each — probably genuinely
                # distinct despite same title; keep to avoid false positives.
                keep_ids.add(other["row"]["openalex_id"])

    stats = {
        "dropped_preprint_vs_journal": dropped_pp_vs_journal,
        "dropped_preprint_vs_preprint": dropped_pp_vs_pp,
        "dropped_zero_cite_mirror": dropped_zero_cite_mirror,
        "kept": len(keep_ids),
        "input": len(rows),
    }
    return [w for w in works if w["openalex_id"] in keep_ids], stats


def main() -> int:
    clusters_yaml = load_clusters()
    cluster_regexes: list[tuple[int, re.Pattern]] = []
    cluster_lookup: dict[int, dict] = {}
    for c in clusters_yaml["clusters"]:
        if not c["keywords"]:
            continue
        cluster_regexes.append((c["number"], compile_keyword_regex(c["keywords"])))
        cluster_lookup[c["number"]] = c

    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row

    # Map OpenAlex author id -> {name, orcid, forrt?}
    contributors_rows = con.execute(
        "SELECT orcid, openalex_id, display_name, works_count FROM authors "
        "WHERE openalex_id IS NOT NULL"
    ).fetchall()
    orcid_to_aid = {r["orcid"]: r["openalex_id"].rsplit("/", 1)[-1]
                    for r in contributors_rows}
    forrt_aids = {v for v in orcid_to_aid.values() if v}
    forrt_orcid_set = set(orcid_to_aid)
    contributor_info: dict[str, dict] = {}
    for r in contributors_rows:
        aid = (r["openalex_id"] or "").rsplit("/", 1)[-1]
        if aid:
            contributor_info[aid] = {
                "orcid": r["orcid"],
                "name": r["display_name"],
            }

    works_rows = con.execute(
        "SELECT openalex_id, doi, title, abstract, publication_year, publication_date, "
        "       work_type, language, cited_by_count, is_oa, oa_status, oa_url, venue, "
        "       authors_json "
        "FROM works"
    ).fetchall()

    total_works_raw = len(works_rows)
    works_rows, dedup_stats = deduplicate(works_rows)
    print(f"Dedup: {dedup_stats['input']} → {dedup_stats['kept']} works "
          f"(dropped {dedup_stats['dropped_preprint_vs_journal']} preprint↔journal, "
          f"{dedup_stats['dropped_preprint_vs_preprint']} preprint↔preprint, "
          f"{dedup_stats['dropped_zero_cite_mirror']} zero-cite mirrors)")
    total_works = len(works_rows)

    # Per-author counts (for FORRT contributor summary cards).
    per_author_all: Counter[str] = Counter()
    per_author_open: Counter[str] = Counter()
    per_author_years: defaultdict[str, Counter[int]] = defaultdict(Counter)
    per_author_clusters: defaultdict[str, Counter[int]] = defaultdict(Counter)
    # Co-author graph between FORRT members (based on open-scholarship works).
    coauthor_edges: Counter[tuple[str, str]] = Counter()
    # Cluster totals (on open-scholarship works).
    cluster_counts: Counter[int] = Counter()
    year_totals: Counter[int] = Counter()
    year_open_totals: Counter[int] = Counter()

    open_works_out: list[dict] = []

    for w in works_rows:
        authorships = json.loads(w["authors_json"] or "[]")
        # Drop single-token titles like "correction" ; we still store everything.
        text_blob = f"{w['title'] or ''}\n{w['abstract'] or ''}".lower()

        # Classification
        matched = classify_text(text_blob, cluster_regexes) if text_blob.strip() else []
        is_open = bool(matched)

        year = w["publication_year"]
        if year:
            year_totals[year] += 1
            if is_open:
                year_open_totals[year] += 1

        # Per-author counts (attribute to every FORRT author on the work).
        forrt_aids_on_work: list[str] = []
        for a in authorships:
            aid = a.get("openalex_id") or ""
            orc = a.get("orcid") or ""
            if aid in forrt_aids or (orc and orc in forrt_orcid_set):
                if not aid and orc:
                    aid = orcid_to_aid.get(orc, "")
                if aid and aid not in forrt_aids_on_work:
                    forrt_aids_on_work.append(aid)

        for aid in forrt_aids_on_work:
            per_author_all[aid] += 1
            if year:
                per_author_years[aid][year] += 1
            if is_open:
                per_author_open[aid] += 1

        if is_open:
            for num in matched:
                cluster_counts[num] += 1
            for aid in forrt_aids_on_work:
                for num in matched:
                    per_author_clusters[aid][num] += 1
            # Record co-authorship edges (open works only — keeps the graph legible).
            for i in range(len(forrt_aids_on_work)):
                for j in range(i + 1, len(forrt_aids_on_work)):
                    a, b = sorted([forrt_aids_on_work[i], forrt_aids_on_work[j]])
                    coauthor_edges[(a, b)] += 1

            open_works_out.append({
                "id": w["openalex_id"],
                "doi": w["doi"],
                "title": w["title"],
                "year": year,
                "date": w["publication_date"],
                "type": w["work_type"],
                "venue": w["venue"],
                "cited_by_count": w["cited_by_count"] or 0,
                "is_oa": bool(w["is_oa"]),
                "oa_status": w["oa_status"],
                "oa_url": w["oa_url"],
                "clusters": matched,
                "authors": [
                    {
                        "name": a.get("name", ""),
                        "orcid": a.get("orcid", "") or "",
                        "openalex_id": a.get("openalex_id", "") or "",
                        "forrt": (a.get("openalex_id", "") in forrt_aids
                                  or a.get("orcid", "") in forrt_orcid_set),
                    }
                    for a in authorships
                ],
            })

    # Sort works newest-first, then by citations.
    open_works_out.sort(
        key=lambda w: (w["year"] or 0, w["cited_by_count"]),
        reverse=True,
    )

    # Build contributor entries, sorted by open-work count (desc).
    contributors_out = []
    for aid, info in contributor_info.items():
        clust = per_author_clusters.get(aid, Counter())
        dominant = clust.most_common(1)[0][0] if clust else None
        contributors_out.append({
            "openalex_id": aid,
            "orcid": info["orcid"],
            "name": info["name"],
            "total_works": per_author_all.get(aid, 0),
            "open_works": per_author_open.get(aid, 0),
            "dominant_cluster": dominant,
            "years": dict(per_author_years.get(aid, {})),
        })
    contributors_out.sort(key=lambda c: (-c["open_works"], c["name"].lower()))

    # Build network (cap edges for browser perf; keep top edges by weight).
    edges_sorted = sorted(coauthor_edges.items(), key=lambda kv: -kv[1])
    MAX_EDGES = 1500
    edges_kept = edges_sorted[:MAX_EDGES]
    kept_nodes = {aid for (a, b), _ in edges_kept for aid in (a, b)}
    nodes_out = []
    for aid in kept_nodes:
        c = per_author_clusters.get(aid, Counter())
        dominant = c.most_common(1)[0][0] if c else None
        nodes_out.append({
            "id": aid,
            "label": contributor_info.get(aid, {}).get("name") or aid,
            "open_works": per_author_open.get(aid, 0),
            "orcid": contributor_info.get(aid, {}).get("orcid") or "",
            "cluster": dominant,
        })
    network_out = {
        "nodes": nodes_out,
        "edges": [
            {"source": a, "target": b, "weight": w}
            for (a, b), w in edges_kept
        ],
    }

    # Top-cited and most-recent open works for the dashboard highlight card.
    most_cited = sorted(open_works_out, key=lambda w: -(w["cited_by_count"] or 0))[:10]
    recent = [w for w in open_works_out if w.get("year")]
    recent = sorted(recent, key=lambda w: (w.get("date") or "", w["year"]), reverse=True)[:10]

    # Top venues among open works.
    venue_counter: Counter[str] = Counter()
    for w in open_works_out:
        if w.get("venue"):
            venue_counter[w["venue"]] += 1
    top_venues = [{"venue": v, "count": n} for v, n in venue_counter.most_common(6)]

    peak_year = None
    if year_open_totals:
        peak_year = max(year_open_totals, key=lambda y: year_open_totals[y])

    # Strip to lightweight dicts for the highlight card.
    def _brief(w):
        return {
            "id": w["id"], "title": w["title"], "year": w["year"],
            "doi": w["doi"], "venue": w["venue"],
            "cited_by_count": w["cited_by_count"],
            "clusters": w["clusters"],
            "authors": [a["name"] for a in w["authors"][:6]],
            "n_authors": len(w["authors"]),
        }

    stats = {
        "total_works": total_works,
        "open_works": len(open_works_out),
        "contributors_total": len(contributor_info),
        "contributors_with_open_work": sum(1 for a in per_author_open.values() if a > 0),
        "year_totals": dict(sorted(year_totals.items())),
        "year_open_totals": dict(sorted(year_open_totals.items())),
        "cluster_counts": {str(k): v for k, v in sorted(cluster_counts.items())},
        "highlights": {
            "most_cited": [_brief(w) for w in most_cited],
            "recent": [_brief(w) for w in recent],
            "top_venues": top_venues,
            "peak_year": peak_year,
            "peak_year_open_works": year_open_totals.get(peak_year, 0) if peak_year else 0,
        },
    }

    meta = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "sources": {
            "openalex": "https://api.openalex.org",
            "clusters_doc": clusters_yaml.get("source"),
            "contributors": "FORRT Tenzing sheets",
        },
        "totals": {
            "works_in_db_raw": total_works_raw,
            "works_in_db": total_works,
            "open_works": len(open_works_out),
            "contributors": len(contributor_info),
        },
        "dedup": dedup_stats,
    }

    clusters_out = {
        "clusters": [
            {"number": c["number"], "name": c["name"], "sub_clusters": c["sub_clusters"]}
            for c in clusters_yaml["clusters"]
        ],
    }
    keywords_out = {
        "clusters": [
            {"number": c["number"], "name": c["name"],
             "sub_clusters": c["sub_clusters"], "keywords": c["keywords"]}
            for c in clusters_yaml["clusters"]
        ],
    }

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "works.json").write_text(
        json.dumps(open_works_out, ensure_ascii=False, separators=(",", ":")))
    (OUT_DIR / "contributors.json").write_text(
        json.dumps(contributors_out, ensure_ascii=False, separators=(",", ":")))
    (OUT_DIR / "network.json").write_text(
        json.dumps(network_out, ensure_ascii=False, separators=(",", ":")))
    (OUT_DIR / "clusters.json").write_text(
        json.dumps(clusters_out, ensure_ascii=False, separators=(",", ":")))
    (OUT_DIR / "keywords.json").write_text(
        json.dumps(keywords_out, ensure_ascii=False, indent=2))
    (OUT_DIR / "stats.json").write_text(
        json.dumps(stats, ensure_ascii=False, indent=2))
    (OUT_DIR / "meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2))

    print(f"Total works in DB: {total_works}")
    print(f"Open-scholarship works: {len(open_works_out)}")
    print(f"FORRT contributors: {len(contributor_info)} "
          f"({stats['contributors_with_open_work']} with >=1 open-scholarship work)")
    print(f"Cluster counts: {dict(cluster_counts)}")
    print(f"Wrote JSON under {OUT_DIR}/")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
