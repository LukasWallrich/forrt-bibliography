"""Parse the FORRT Clusters v4.1 Google Doc and emit cluster/sub-cluster keyword lists.

Downloads the public plaintext export of the cluster doc, extracts clusters
1-11 with their sub-cluster names, then converts each sub-cluster name into
a set of matching keywords (phrase + salient single-word alternates).

Output: data/clusters.yaml
"""

from __future__ import annotations

import re
import sys
import urllib.request
from pathlib import Path

import yaml

DOC_URL = (
    "https://docs.google.com/document/d/"
    "1_TRh7z3Bv_tdxGqjdWMm4kfQerTYvYw3e4wvQLNpTDQ/export?format=txt"
)
ROOT = Path(__file__).resolve().parent.parent
OUT_PATH = ROOT / "data" / "clusters.yaml"

CLUSTER_HEAD = re.compile(r"^Cluster\s+(\d+)\s*[:.]\s*(.+?)\s*$", re.IGNORECASE)
SUBCLUSTER_HEAD = re.compile(r"^Sub-?cluster\s+\d+\s*[:.]\s*(.+?)\s*$", re.IGNORECASE)
BULLET = re.compile(r"^\s*[\*•]\s+(.+?)\s*$")
ANNOTATION = re.compile(r"\[[a-z]{1,3}\]")

# Curated keyword lists per cluster.
#
# These are hand-picked to maximise specificity — every entry should be a
# reasonably unambiguous marker of its cluster's topic. We deliberately avoid:
#   * generic open-science terms that could fit anywhere (e.g. "data sharing"
#     shows up under FAIR, not under Replication Crisis);
#   * bland single words that match unrelated literatures ("equity", "culture");
#   * phrase fragments mechanically produced by sub-cluster-name splitting
#     ("writing one", "ii errors", "problem solving" etc.).
#
# Sub-cluster names are still shown on the keywords debug page and on each
# cluster's card, but they are no longer auto-coerced into keywords.
CLUSTER_KEYWORDS: dict[int, list[str]] = {
    1: [
        "replication crisis", "credibility revolution", "reproducibility crisis",
        "open science", "open scholarship",
        "questionable research practices", "qrp",
        "replication",  # broad but load-bearing for this cluster
    ],
    2: [
        "statistical power", "effect size", "p-value", "p values",
        "null hypothesis significance testing", "nhst",
        "confidence interval", "confidence intervals",
        "bayesian statistics", "bayesian inference",
        "questionable measurement practices", "measurement validity",
    ],
    3: [
        "big team science", "many labs", "adversarial collaboration",
        "community science", "participatory research", "citizen science",
        "science communication", "public engagement with science",
        "slow science", "slow scholarship",
        "reflexivity", "positionality",
    ],
    4: [
        "preregistration", "pre-registration", "pre registration",
        "registered report", "registered reports",
        "pre-analysis plan", "pre analysis plan", "analysis plan",
    ],
    5: [
        "computational reproducibility", "reproducible analysis",
        "reproducible workflow", "reproducible research",
        "open source software", "free and open source software",
        "research software engineering", "research software",
        "containerisation", "containerization",
    ],
    6: [
        "fair data", "fair principles", "fair data principles",
        "findable accessible interoperable reusable",
        "research data management", "data management plan",
        "metadata standards", "data repository", "data repositories",
        "data stewardship",
    ],
    7: [
        "open access", "gold open access", "green open access", "diamond open access",
        "open peer review", "post-publication peer review",
        "preprint", "preprints", "postprint", "postprints",
        "rights retention", "self-archiving",
    ],
    8: [
        "meta-research", "metascience", "meta-science",
        "replication study", "replication studies",
        "registered replication report", "registered replication",
        "direct replication", "conceptual replication",
        "many labs replication", "failed replication",
    ],
    9: [
        "neurodiversity", "citation politics", "hidden curriculum",
        "weird samples",
        "research assessment", "research evaluation",
        "academic incentives",
        "equity in academia", "diversity in academia",
        "racism in science",
    ],
    10: [
        "qualitative research", "qualitative methods", "qualitative inquiry",
        "thematic analysis", "grounded theory", "interpretive research",
        "transparency in qualitative research",
        "reflexive thematic analysis",
    ],
    11: [
        "research integrity", "research misconduct",
        "fabrication", "falsification", "plagiarism",
        "responsible conduct of research", "responsible research practices",
        "questionable research practices",
    ],
}


def fetch_doc() -> str:
    req = urllib.request.Request(DOC_URL, headers={"User-Agent": "forrt-bibliography/0.1"})
    with urllib.request.urlopen(req, timeout=60) as r:
        raw = r.read().decode("utf-8-sig")
    return raw.replace("\r\n", "\n")


def clean(line: str) -> str:
    line = ANNOTATION.sub("", line)
    return line.strip().strip("*").strip()


SEPARATOR = re.compile(r"^_{5,}$")


def parse_clusters(text: str) -> list[dict]:
    """Parse the doc's table-of-contents section for clusters + sub-clusters.

    The doc begins with (1) a compact cluster-name-only TOC, then (2) a full
    TOC listing each cluster heading followed by its sub-clusters (as plain
    lines for clusters 1-10, and "Sub-cluster N: ..." for cluster 11), then
    (3) a horizontal separator, then the long detailed body with citations.

    We locate section (2) by finding the *second* occurrence of "Cluster 1:"
    and stop at the first separator line after cluster 11.
    """
    lines = [ln.rstrip() for ln in text.split("\n")]

    # Find the second "Cluster 1:" heading (start of detailed TOC).
    cluster1_hits = [i for i, ln in enumerate(lines) if CLUSTER_HEAD.match(clean(ln))
                     and CLUSTER_HEAD.match(clean(ln)).group(1) == "1"]
    if len(cluster1_hits) < 2:
        raise RuntimeError("Could not locate detailed TOC section (need 2+ 'Cluster 1:' headings)")
    start = cluster1_hits[1]

    clusters: dict[int, dict] = {}
    current = None
    for line in lines[start:]:
        stripped = clean(line)
        if SEPARATOR.match(stripped) and current is not None and current["number"] == 11:
            break
        m = CLUSTER_HEAD.match(stripped)
        if m:
            num = int(m.group(1))
            name = clean(m.group(2))
            clusters[num] = {"number": num, "name": name, "sub_clusters": []}
            current = clusters[num]
            continue
        if current is None or not stripped:
            continue
        sm = SUBCLUSTER_HEAD.match(stripped)
        if sm:
            sc = clean(sm.group(1))
        elif current["number"] == 11:
            # Cluster 11 uses only "Sub-cluster N:" headings — ignore stray lines
            # like "Ideas for contributors" that follow the sub-cluster list.
            continue
        else:
            # Plain sub-cluster line (clusters 1-10 in the TOC).
            bm = BULLET.match(line)
            sc = clean(bm.group(1)) if bm else stripped
        if sc and sc not in current["sub_clusters"]:
            current["sub_clusters"].append(sc)

    return [clusters[k] for k in sorted(clusters)]


def build_keywords(cluster: dict) -> dict:
    """Attach the curated keyword list for this cluster.

    We do not derive keywords from sub-cluster names anymore — that produced
    too many generic / fragmentary phrases. Sub-cluster names are still
    retained on the cluster record for display on the keywords debug page
    and elsewhere.
    """
    keywords = [kw.strip().lower() for kw in CLUSTER_KEYWORDS.get(cluster["number"], [])]
    return {
        "number": cluster["number"],
        "name": cluster["name"],
        "sub_clusters": cluster["sub_clusters"],
        "keywords": keywords,
    }


def main() -> int:
    text = fetch_doc()
    clusters = parse_clusters(text)
    if not clusters:
        print("ERROR: parsed zero clusters", file=sys.stderr)
        return 1

    enriched = [build_keywords(c) for c in clusters]

    all_keywords: list[str] = []
    seen_all: set[str] = set()
    for c in enriched:
        for kw in c["keywords"]:
            if kw not in seen_all:
                seen_all.add(kw)
                all_keywords.append(kw)

    payload = {
        "source": DOC_URL,
        "clusters": enriched,
        "open_scholarship_keywords": sorted(all_keywords),
    }
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with OUT_PATH.open("w") as fh:
        yaml.safe_dump(payload, fh, sort_keys=False, allow_unicode=True, width=100)

    print(f"Wrote {OUT_PATH}")
    for c in enriched:
        print(f"  Cluster {c['number']}: {c['name']} "
              f"({len(c['sub_clusters'])} sub-clusters, {len(c['keywords'])} keywords)")
    print(f"  Total unique keywords: {len(all_keywords)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
