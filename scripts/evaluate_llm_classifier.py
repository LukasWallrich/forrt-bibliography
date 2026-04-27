"""Evaluate cheap LLMs for FORRT open-scholarship inclusion decisions.

The curated `Publications` sheet is treated as labelled training/evaluation
data, not as the site classifier. This script:

1. Pulls labelled positive/excluded examples from the sheet.
2. Builds challenge examples from the OpenAlex cache using the current keyword
   filter, especially cases not present in the curated sheet.
3. Calls a selected LLM with a strict JSON prompt.
4. Caches model outputs by prompt/model/input hash.
5. Writes CSV/JSONL outputs for auditing and prompt/model comparison.

Examples:

  # Build the evaluation/challenge dataset without spending API calls.
  python scripts/evaluate_llm_classifier.py --dry-run --limit 20

  # Evaluate Gemini 3.1 Flash-Lite on a small balanced sample.
  GEMINI_API_KEY=... python scripts/evaluate_llm_classifier.py \
    --provider gemini --model gemini-3.1-flash-lite-preview --limit 100

  # Evaluate an OpenAI model.
  OPENAI_API_KEY=... python scripts/evaluate_llm_classifier.py \
    --provider openai --model gpt-5.4-nano --limit 100

  # Also publish the run to an editable Google Sheet tab.
  GSHEET_CREDENTIALS=... GEMINI_API_KEY=... python scripts/evaluate_llm_classifier.py \
    --audit-sheet-id ... --audit-worksheet "LLM eval"
"""

from __future__ import annotations

import argparse
import csv
import io
import hashlib
import json
import os
import random
import re
import sqlite3
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd
import requests
import yaml

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "works.sqlite"
CLUSTERS_PATH = ROOT / "data" / "clusters.yaml"
CURRENT_WORKS_PATH = ROOT / "site" / "data" / "works.json"
OUT_DIR = ROOT / "data" / "llm_evals"

SHEET_ID = "1BxYioDDE2GftOFdQGtH0lVguEUWNQ_k8Ls-bdRn8RRo"
SHEET_URL = f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/export?format=xlsx"
SHEET_NAME = "Publications"

PROMPT_VERSION = "about-open-scholarship-v1"
DEFAULT_GEMINI_MODEL = "gemini-3.1-flash-lite-preview"
DEFAULT_OPENAI_MODEL = "gpt-5.4-nano"

DECISIONS = {"include", "exclude", "uncertain"}
ABOUTNESS = {
    "about_open_scholarship",
    "uses_open_practice_only",
    "not_related",
    "unclear",
}


@dataclass
class Example:
    id: str
    source: str
    title: str
    abstract: str
    doi: str = ""
    year: str = ""
    venue: str = ""
    work_type: str = ""
    authors: str = ""
    keyword_matches: str = ""
    gold_decision: str = ""
    gold_clusters: str = ""
    gold_notes: str = ""


def normalize_doi(doi: str | None) -> str:
    if not doi:
        return ""
    doi = doi.strip().lower()
    doi = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", doi)
    doi = re.sub(r"^doi:\s*", "", doi)
    return doi.strip()


def normalize_title(title: str | None) -> str:
    if not title:
        return ""
    title = title.lower()
    title = re.sub(r"[^\w\s]+", " ", title)
    return re.sub(r"\s+", " ", title).strip()


def text_hash(*parts: str) -> str:
    h = hashlib.sha256()
    for part in parts:
        h.update((part or "").encode("utf-8"))
        h.update(b"\0")
    return h.hexdigest()[:16]


def load_clusters() -> list[dict]:
    with CLUSTERS_PATH.open() as fh:
        return yaml.safe_load(fh)["clusters"]


def subcluster_lookup(clusters: list[dict]) -> dict[str, int]:
    lookup = {}
    for c in clusters:
        lookup[normalize_title(c["name"])] = c["number"]
        for sub in c.get("sub_clusters", []):
            lookup[normalize_title(sub)] = c["number"]
    return lookup


def compile_keyword_patterns(clusters: list[dict]) -> list[tuple[int, str, re.Pattern]]:
    patterns = []
    for c in clusters:
        for kw in c.get("keywords", []):
            tokens = [re.escape(t) for t in kw.split()]
            patterns.append((
                c["number"],
                kw,
                re.compile(r"\b" + r"\s+".join(tokens) + r"\b", re.IGNORECASE),
            ))
    return patterns


def keyword_matches(text: str, patterns: list[tuple[int, str, re.Pattern]]) -> list[str]:
    return [f"{num}:{kw}" for num, kw, rx in patterns if rx.search(text)]


def fetch_labelled_examples(clusters: list[dict]) -> tuple[list[Example], set[str], set[str]]:
    df = pd.read_excel(SHEET_URL, sheet_name=SHEET_NAME, dtype=str).fillna("")
    lookup = subcluster_lookup(clusters)
    examples = []
    labelled_dois: set[str] = set()
    labelled_titles: set[str] = set()

    for i, row in df.iterrows():
        title = row.get("Title", "").strip()
        if not title:
            continue
        doi = normalize_doi(row.get("DOI", ""))
        title_norm = normalize_title(title)
        if doi:
            labelled_dois.add(doi)
        labelled_titles.add(title_norm)
        excluded = str(row.get("Display exclusion", "")).strip().lower() not in {
            "",
            "0",
            "false",
            "no",
            "nan",
        }
        cluster = lookup.get(normalize_title(row.get("Sub-Cluster", "")))
        examples.append(Example(
            id=f"sheet:{i + 2}",
            source="labelled_sheet",
            doi=doi,
            title=title,
            abstract=row.get("Abstract", ""),
            keyword_matches="",
            gold_decision="exclude" if excluded else "include",
            gold_clusters=str(cluster or ""),
            gold_notes=row.get("Exclusion reason", "") or row.get("Summary", ""),
            work_type=row.get("Resource Type", ""),
        ))
    return examples, labelled_dois, labelled_titles


def load_openalex_challenges(
    clusters: list[dict],
    labelled_dois: set[str],
    labelled_titles: set[str],
    max_examples: int,
) -> list[Example]:
    if not DB_PATH.exists():
        return []
    patterns = compile_keyword_patterns(clusters)
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT openalex_id, doi, title, abstract, publication_year, work_type, venue, "
        "       authors_json, cited_by_count "
        "FROM works WHERE title IS NOT NULL AND title != ''"
    ).fetchall()

    candidates = []
    weak_terms = {
        "replication", "preprint", "preprints", "effect size", "confidence interval",
        "confidence intervals", "analysis plan", "qualitative research",
        "research software", "fabrication", "falsification", "plagiarism",
    }
    for r in rows:
        doi = normalize_doi(r["doi"])
        title_norm = normalize_title(r["title"])
        if (doi and doi in labelled_dois) or title_norm in labelled_titles:
            continue
        text = f"{r['title'] or ''}\n{r['abstract'] or ''}"
        matches = keyword_matches(text, patterns)
        if not matches:
            continue
        match_terms = {m.split(":", 1)[1] for m in matches}
        bucket_score = 2 if match_terms <= weak_terms else 1
        authors = []
        try:
            authors = [a.get("name", "") for a in json.loads(r["authors_json"] or "[]")[:8]]
        except json.JSONDecodeError:
            pass
        candidates.append((
            bucket_score,
            r["cited_by_count"] or 0,
            Example(
                id=f"openalex:{r['openalex_id']}",
                source="keyword_challenge",
                doi=doi,
                title=r["title"] or "",
                abstract=r["abstract"] or "",
                year=str(r["publication_year"] or ""),
                venue=r["venue"] or "",
                work_type=r["work_type"] or "",
                authors="; ".join(authors),
                keyword_matches=", ".join(matches),
            ),
        ))

    candidates.sort(key=lambda x: (-x[0], -x[1], x[2].title))
    return [ex for _, _, ex in candidates[:max_examples]]


def load_current_false_positive_candidates(
    clusters: list[dict],
    labelled_dois: set[str],
    labelled_titles: set[str],
    max_examples: int,
) -> list[Example]:
    """Return currently displayed, unlabelled works most likely to be false positives."""
    if not DB_PATH.exists() or not CURRENT_WORKS_PATH.exists():
        return []

    current = json.loads(CURRENT_WORKS_PATH.read_text())
    current_ids = {w["id"] for w in current}
    current_meta = {w["id"]: w for w in current}

    patterns = compile_keyword_patterns(clusters)
    weak_terms = {
        "analysis plan", "bayesian inference", "bayesian statistics",
        "confidence interval", "confidence intervals", "effect size",
        "fabrication", "falsification", "p-value", "p values", "plagiarism",
        "preprint", "preprints", "postprint", "postprints", "qualitative methods",
        "qualitative research", "replication", "research software", "thematic analysis",
    }
    suspicious_types = {"dataset", "libguides", "peer-review", "paratext", "other"}
    suspicious_venue_fragments = [
        "osf preprints", "zenodo", "dataverse", "repository", "figshare",
        "preprints", "thesis", "dissertation",
    ]

    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    placeholders = ",".join("?" for _ in current_ids)
    rows = con.execute(
        "SELECT openalex_id, doi, title, abstract, publication_year, work_type, venue, "
        "       authors_json, cited_by_count "
        f"FROM works WHERE openalex_id IN ({placeholders})",
        sorted(current_ids),
    ).fetchall()

    candidates = []
    for r in rows:
        doi = normalize_doi(r["doi"])
        title_norm = normalize_title(r["title"])
        if (doi and doi in labelled_dois) or title_norm in labelled_titles:
            continue

        text = f"{r['title'] or ''}\n{r['abstract'] or ''}"
        matches = keyword_matches(text, patterns)
        if not matches:
            continue
        match_terms = {m.split(":", 1)[1] for m in matches}
        weak_only = match_terms <= weak_terms
        venue = (r["venue"] or "").lower()
        type_suspicious = (r["work_type"] or "").lower() in suspicious_types
        venue_suspicious = any(fragment in venue for fragment in suspicious_venue_fragments)
        title_suspicious = bool(re.search(
            r"\b(preprint|peer review of|replication data for|supplement|dataset)\b",
            r["title"] or "",
            re.IGNORECASE,
        ))

        score = (
            (4 if weak_only else 0)
            + (2 if type_suspicious else 0)
            + (2 if venue_suspicious else 0)
            + (2 if title_suspicious else 0)
            + (1 if not (r["abstract"] or "").strip() else 0)
        )
        if score == 0:
            continue

        authors = []
        try:
            authors = [a.get("name", "") for a in json.loads(r["authors_json"] or "[]")[:8]]
        except json.JSONDecodeError:
            pass
        meta = current_meta.get(r["openalex_id"], {})
        candidates.append((
            score,
            r["cited_by_count"] or meta.get("cited_by_count") or 0,
            Example(
                id=f"current-fp:{r['openalex_id']}",
                source="current_false_positive_candidate",
                doi=doi,
                title=r["title"] or "",
                abstract=r["abstract"] or "",
                year=str(r["publication_year"] or meta.get("year") or ""),
                venue=r["venue"] or meta.get("venue") or "",
                work_type=r["work_type"] or meta.get("type") or "",
                authors="; ".join(authors),
                keyword_matches=", ".join(matches),
                gold_decision="exclude",
                gold_notes="candidate negative: currently displayed, unlabelled, weak/suspicious keyword evidence",
            ),
        ))

    candidates.sort(key=lambda x: (-x[0], -x[1], x[2].title))
    return [ex for _, _, ex in candidates[:max_examples]]


def build_prompt(example: Example, clusters: list[dict]) -> str:
    cluster_lines = "\n".join(
        f"{c['number']}. {c['name']} ({'; '.join(c.get('sub_clusters', [])[:6])})"
        for c in clusters
    )
    abstract = example.abstract.strip()
    if len(abstract) > 2400:
        abstract = abstract[:2400] + "..."
    return f"""You are classifying publications for a FORRT bibliography.

Goal: include works ABOUT open scholarship / open science / reproducible science / research reform.
Do not include a work merely because it uses or reports an open practice.

Include if the work is primarily about topics such as open scholarship, reproducibility,
replicability as a research-reform issue, meta-research, preregistration as a topic,
registered reports as a publication model, open/FAIR data as a topic, open access as
scholarly communication, research integrity, peer review, research assessment, academic
incentives, or other FORRT cluster topics.

Exclude if the work merely says it was preregistered, has open data/code/materials, is a
preprint/open-access article, reports confidence intervals/effect sizes/Bayesian analyses,
uses qualitative methods, or is a substantive replication of a domain finding without
being about replication/reproducibility/meta-research.

If title and abstract are insufficient, choose "uncertain".

FORRT clusters:
{cluster_lines}

Return only JSON with this shape:
{{
  "display_decision": "include" | "exclude" | "uncertain",
  "aboutness": "about_open_scholarship" | "uses_open_practice_only" | "not_related" | "unclear",
  "clusters": [cluster numbers],
  "confidence": number between 0 and 1,
  "short_reason": "one concise sentence",
  "evidence": ["up to three short phrases from title/abstract"]
}}

Publication:
Title: {example.title}
Abstract: {abstract or "[missing]"}
Venue: {example.venue or "[missing]"}
Type: {example.work_type or "[missing]"}
Keyword matches from recall filter: {example.keyword_matches or "[none]"}
"""


def load_cache(path: Path) -> dict[str, dict]:
    cache = {}
    if not path.exists():
        return cache
    with path.open() as fh:
        for line in fh:
            if line.strip():
                item = json.loads(line)
                cache[item["cache_key"]] = item
    return cache


def append_cache(path: Path, item: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as fh:
        fh.write(json.dumps(item, ensure_ascii=False) + "\n")


def cache_key(provider: str, model: str, prompt: str, example: Example) -> str:
    return text_hash(PROMPT_VERSION, provider, model, prompt, example.id, example.title, example.abstract)


def parse_json_response(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?", "", text).strip()
        text = re.sub(r"```$", "", text).strip()
    data = json.loads(text)
    if data.get("display_decision") not in DECISIONS:
        data["display_decision"] = "uncertain"
    if data.get("aboutness") not in ABOUTNESS:
        data["aboutness"] = "unclear"
    try:
        data["confidence"] = float(data.get("confidence", 0))
    except (TypeError, ValueError):
        data["confidence"] = 0
    data["confidence"] = max(0, min(1, data["confidence"]))
    data["clusters"] = [int(c) for c in data.get("clusters", []) if str(c).isdigit()]
    data["short_reason"] = str(data.get("short_reason", ""))[:500]
    data["evidence"] = [str(e)[:160] for e in data.get("evidence", [])[:3]]
    return data


def call_gemini(model: str, prompt: str, timeout: int) -> dict:
    key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if not key:
        raise RuntimeError("Set GEMINI_API_KEY or GOOGLE_API_KEY")
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    payload = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0,
            "responseMimeType": "application/json",
        },
    }
    r = requests.post(url, params={"key": key}, json=payload, timeout=timeout)
    r.raise_for_status()
    data = r.json()
    text = data["candidates"][0]["content"]["parts"][0]["text"]
    return parse_json_response(text)


def call_openai(model: str, prompt: str, timeout: int) -> dict:
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        raise RuntimeError("Set OPENAI_API_KEY")
    payload = {
        "model": model,
        "input": prompt,
        "text": {
            "format": {
                "type": "json_schema",
                "name": "forrt_publication_classification",
                "schema": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "display_decision": {"type": "string", "enum": sorted(DECISIONS)},
                        "aboutness": {"type": "string", "enum": sorted(ABOUTNESS)},
                        "clusters": {"type": "array", "items": {"type": "integer"}},
                        "confidence": {"type": "number"},
                        "short_reason": {"type": "string"},
                        "evidence": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": [
                        "display_decision", "aboutness", "clusters", "confidence",
                        "short_reason", "evidence",
                    ],
                },
                "strict": True,
            }
        },
    }
    r = requests.post(
        "https://api.openai.com/v1/responses",
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        json=payload,
        timeout=timeout,
    )
    r.raise_for_status()
    data = r.json()
    text = data.get("output_text")
    if not text:
        texts = []
        for item in data.get("output", []):
            for part in item.get("content", []):
                if part.get("type") in {"output_text", "text"}:
                    texts.append(part.get("text", ""))
        text = "\n".join(texts)
    return parse_json_response(text)


def call_model(provider: str, model: str, prompt: str, timeout: int) -> dict:
    if provider == "gemini":
        return call_gemini(model, prompt, timeout)
    if provider == "openai":
        return call_openai(model, prompt, timeout)
    raise ValueError(f"Unknown provider: {provider}")


def choose_examples(
    labelled: list[Example],
    challenges: list[Example],
    false_positives: list[Example],
    limit: int,
    seed: int,
    sample: str,
) -> list[Example]:
    rng = random.Random(seed)
    positives = [e for e in labelled if e.gold_decision == "include"]
    negatives = [e for e in labelled if e.gold_decision == "exclude"]
    rng.shuffle(positives)
    rng.shuffle(negatives)

    if sample == "false-positives":
        selected = false_positives[:limit] if limit > 0 else false_positives
        return selected
    if sample == "labelled":
        selected = positives + negatives
        return selected[:limit] if limit > 0 else selected

    if limit <= 0:
        return positives + negatives + false_positives + challenges

    n_labelled = max(1, int(limit * 0.7))
    selected = positives[:n_labelled]
    if negatives:
        selected.extend(negatives[: min(len(negatives), max(5, limit // 10))])
    remaining = max(0, limit - len(selected))
    fp_take = min(len(false_positives), max(remaining // 2, remaining if not challenges else 0))
    selected.extend(false_positives[:fp_take])
    selected.extend(challenges[: max(0, limit - len(selected))])
    return selected[:limit]


def metrics(rows: list[dict]) -> dict:
    labelled = [r for r in rows if r.get("gold_decision") in DECISIONS]
    if not labelled:
        return {}
    tp = sum(1 for r in labelled if r["gold_decision"] == "include" and r["model_decision"] == "include")
    fp = sum(1 for r in labelled if r["gold_decision"] != "include" and r["model_decision"] == "include")
    fn = sum(1 for r in labelled if r["gold_decision"] == "include" and r["model_decision"] != "include")
    exact = sum(1 for r in labelled if r["gold_decision"] == r["model_decision"])
    return {
        "labelled_n": len(labelled),
        "accuracy": exact / len(labelled),
        "include_precision": tp / (tp + fp) if tp + fp else None,
        "include_recall": tp / (tp + fn) if tp + fn else None,
        "tp": tp,
        "fp": fp,
        "fn": fn,
    }


def upload_audit_sheet(sheet_id: str, worksheet_name: str, rows: list[dict]) -> None:
    """Write evaluation rows to an editable Google Sheet worksheet.

    Requires GSHEET_CREDENTIALS to contain a service-account JSON object. The
    sheet must be shared with that service account.
    """
    creds_json = os.environ.get("GSHEET_CREDENTIALS")
    if not creds_json:
        raise RuntimeError("Set GSHEET_CREDENTIALS to upload an audit sheet")

    import gspread
    from google.oauth2.service_account import Credentials

    creds = Credentials.from_service_account_info(
        json.loads(creds_json),
        scopes=["https://www.googleapis.com/auth/spreadsheets"],
    )
    client = gspread.authorize(creds)
    workbook = client.open_by_key(sheet_id)
    try:
        worksheet = workbook.worksheet(worksheet_name)
        worksheet.clear()
    except gspread.WorksheetNotFound:
        worksheet = workbook.add_worksheet(title=worksheet_name, rows=100, cols=30)

    if not rows:
        return

    # Keep the audit columns in the sheet even though the model never writes them.
    human_cols = ["human_decision", "human_clusters", "human_notes", "reviewed_by", "reviewed_at"]
    fields = list(rows[0])
    for col in human_cols:
        if col not in fields:
            fields.append(col)

    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=fields)
    writer.writeheader()
    for row in rows:
        writer.writerow({**{c: "" for c in human_cols}, **row})
    values = list(csv.reader(io.StringIO(buf.getvalue())))
    worksheet.update(values, value_input_option="RAW")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--provider", choices=["gemini", "openai"], default="gemini")
    ap.add_argument("--model", help="Model id. Defaults depend on provider.")
    ap.add_argument("--limit", type=int, default=100)
    ap.add_argument("--challenge-limit", type=int, default=500)
    ap.add_argument(
        "--sample",
        choices=["mixed", "labelled", "false-positives"],
        default="mixed",
        help="Which evaluation set to run. false-positives means currently displayed unlabelled likely-noisy works.",
    )
    ap.add_argument("--seed", type=int, default=13)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--timeout", type=int, default=60)
    ap.add_argument("--sleep", type=float, default=0.0, help="Seconds between API calls")
    ap.add_argument("--out-dir", type=Path, default=OUT_DIR)
    ap.add_argument("--audit-sheet-id", help="Optional Google Sheet ID for editable audit output")
    ap.add_argument("--audit-worksheet", default="LLM classifier eval")
    args = ap.parse_args()

    model = args.model or (DEFAULT_GEMINI_MODEL if args.provider == "gemini" else DEFAULT_OPENAI_MODEL)
    clusters = load_clusters()
    labelled, labelled_dois, labelled_titles = fetch_labelled_examples(clusters)
    challenges = load_openalex_challenges(
        clusters, labelled_dois, labelled_titles, max_examples=args.challenge_limit
    )
    false_positives = load_current_false_positive_candidates(
        clusters, labelled_dois, labelled_titles, max_examples=args.challenge_limit
    )
    examples = choose_examples(
        labelled, challenges, false_positives, args.limit, args.seed, args.sample
    )

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    safe_model = re.sub(r"[^A-Za-z0-9_.-]+", "_", model)
    args.out_dir.mkdir(parents=True, exist_ok=True)
    csv_path = args.out_dir / f"{stamp}_{args.provider}_{safe_model}.csv"
    summary_path = args.out_dir / f"{stamp}_{args.provider}_{safe_model}_summary.json"
    cache_path = args.out_dir / "cache.jsonl"
    cache = load_cache(cache_path)

    rows = []
    for i, ex in enumerate(examples, 1):
        prompt = build_prompt(ex, clusters)
        key = cache_key(args.provider, model, prompt, ex)
        if args.dry_run:
            result = {
                "display_decision": "",
                "aboutness": "",
                "clusters": [],
                "confidence": "",
                "short_reason": "dry run; no model call",
                "evidence": [],
            }
            cache_status = "dry_run"
        elif key in cache:
            result = cache[key]["result"]
            cache_status = "hit"
        else:
            result = call_model(args.provider, model, prompt, args.timeout)
            cache_status = "miss"
            append_cache(cache_path, {
                "cache_key": key,
                "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "prompt_version": PROMPT_VERSION,
                "provider": args.provider,
                "model": model,
                "example_id": ex.id,
                "input_hash": text_hash(ex.title, ex.abstract),
                "result": result,
            })
            if args.sleep:
                time.sleep(args.sleep)

        row = {
            "example_id": ex.id,
            "source": ex.source,
            "doi": ex.doi,
            "title": ex.title,
            "year": ex.year,
            "venue": ex.venue,
            "work_type": ex.work_type,
            "keyword_matches": ex.keyword_matches,
            "gold_decision": ex.gold_decision,
            "gold_clusters": ex.gold_clusters,
            "model_decision": result.get("display_decision", ""),
            "model_aboutness": result.get("aboutness", ""),
            "model_clusters": ";".join(map(str, result.get("clusters", []))),
            "model_confidence": result.get("confidence", ""),
            "model_short_reason": result.get("short_reason", ""),
            "model_evidence": " | ".join(result.get("evidence", [])),
            "cache_status": cache_status,
        }
        rows.append(row)
        print(f"{i}/{len(examples)} {cache_status} {ex.source}: {row['model_decision']} {ex.title[:70]}")

    fields = list(rows[0]) if rows else []
    with csv_path.open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)

    summary = {
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "prompt_version": PROMPT_VERSION,
        "provider": args.provider,
        "model": model,
        "dry_run": args.dry_run,
        "examples": len(rows),
        "labelled_sheet_rows": len(labelled),
        "challenge_rows_available": len(challenges),
        "false_positive_rows_available": len(false_positives),
        "sample": args.sample,
        "metrics": metrics(rows) if not args.dry_run else {},
        "csv": str(csv_path),
        "cache": str(cache_path),
    }
    if args.audit_sheet_id:
        upload_audit_sheet(args.audit_sheet_id, args.audit_worksheet, rows)
        summary["audit_sheet_id"] = args.audit_sheet_id
        summary["audit_worksheet"] = args.audit_worksheet
    summary_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False))
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
