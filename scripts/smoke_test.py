"""Quick smoke test: sanity-check site JSON output after a full pipeline run.

Emits a pass/fail summary. Useful to catch regressions in a CI dry-run.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SITE_DATA = ROOT / "site" / "data"


def fail(msg: str):
    print(f"FAIL: {msg}")
    return False


def ok(msg: str):
    print(f"  ok: {msg}")
    return True


def main() -> int:
    required = ["works.json", "contributors.json", "clusters.json",
                "stats.json", "network.json", "meta.json"]
    for f in required:
        p = SITE_DATA / f
        if not p.exists():
            return int(not fail(f"missing {p}"))

    stats = json.loads((SITE_DATA / "stats.json").read_text())
    works = json.loads((SITE_DATA / "works.json").read_text())
    contribs = json.loads((SITE_DATA / "contributors.json").read_text())
    clusters = json.loads((SITE_DATA / "clusters.json").read_text())["clusters"]
    network = json.loads((SITE_DATA / "network.json").read_text())

    checks = []
    checks.append(ok(f"{len(works)} open-scholarship works, "
                     f"{stats['total_works']} total"))
    checks.append(ok(f"{len(contribs)} contributors, "
                     f"{stats['contributors_with_open_work']} with open work"))
    checks.append(ok(f"{len(clusters)} clusters in taxonomy"))
    checks.append(ok(f"{len(network['nodes'])} nodes / "
                     f"{len(network['edges'])} edges in co-author network"))

    if stats["open_works"] != len(works):
        return int(not fail(
            f"stats.open_works ({stats['open_works']}) != len(works.json) ({len(works)})"))

    missing_clusters = [w["id"] for w in works[:50] if not w["clusters"]]
    if missing_clusters:
        return int(not fail(
            f"{len(missing_clusters)} sampled 'open' works have no cluster: {missing_clusters[:3]}"))
    checks.append(ok("all sampled open works have >=1 cluster"))

    if any(not w.get("title") for w in works[:100]):
        return int(not fail("some works missing title"))
    checks.append(ok("titles present on sampled works"))

    forrt_highlights = sum(1 for w in works[:200]
                           for a in w["authors"] if a["forrt"])
    if forrt_highlights == 0:
        return int(not fail(
            "no FORRT-author highlights in first 200 works — linkage broken?"))
    checks.append(ok(f"{forrt_highlights} FORRT-author flags in first 200 works"))

    print("\nAll smoke tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
