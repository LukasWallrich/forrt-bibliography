"""Build a canonical list of FORRT contributors with ORCIDs.

Mirrors the pattern used by forrtproject.github.io's tenzing.py:

  * INDEX sheet (private) lists every FORRT project and its public Tenzing
    sheet URL. Requires GSHEET_CREDENTIALS (service account JSON) OR an
    interactive gws CLI session.
  * Each Tenzing sheet is public and exportable as CSV.

We merge all contributor rows, dedupe by ORCID (falling back to name), and
write `data/contributors.csv` with columns:
    orcid, first_name, middle_name, surname, full_name, projects

Projects is a semicolon-joined list of project names the contributor is
credited on (any role). Without credentials, the script re-uses the existing
`data/contributors.csv` (falling back to the FORRT cache shipped with the
main site repo if present under `/tmp/forrt-site/`).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pandas as pd

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

ROOT = Path(__file__).resolve().parent.parent
OUT_PATH = ROOT / "data" / "contributors.csv"

INDEX_SPREADSHEET_ID = "1MUD54FQUhfcBKrvr5gCYoh2wgbJ6Lf7oAJRAqsQ-Nag"
INDEX_WORKSHEET_NAME = "TENZING SHEETS SOURCE"

# Extra-roles sheet (public): people credited via a simple Role column rather
# than a Tenzing CRediT matrix.
EXTRA_ROLES_URL = (
    "https://docs.google.com/spreadsheets/d/e/2PACX-1vSCsxHTnSSjYqhQSR2kT3g"
    "IYg82HiODjPat9y2TFPrZESYWxz4k8CZsOesXPD3C5dngZEGujtKmNZsa/pub?output=csv"
)

ORCID_RE = re.compile(r"\b\d{4}-\d{4}-\d{4}-\d{3}[0-9X]\b")


def convert_to_csv_url(url: str) -> str:
    parsed = urlparse(url)
    m = re.search(r"/d/([a-zA-Z0-9_-]{20,})", parsed.path)
    if not m:
        raise ValueError(f"Can't parse spreadsheet id from {url}")
    sid = m.group(1)
    gid = None
    q = parse_qs(parsed.query)
    if "gid" in q:
        gid = q["gid"][0]
    else:
        fm = re.search(r"gid=(\d+)", parsed.fragment)
        if fm:
            gid = fm.group(1)
    if gid:
        return f"https://docs.google.com/spreadsheets/d/{sid}/export?format=csv&gid={gid}"
    return f"https://docs.google.com/spreadsheets/d/{sid}/export?format=csv"


def read_index_via_gspread() -> pd.DataFrame | None:
    """Read the private INDEX sheet using a service-account JSON (for CI)."""
    creds_json = os.environ.get("GSHEET_CREDENTIALS")
    if not creds_json:
        return None
    try:
        import gspread
        from google.oauth2.service_account import Credentials
    except ImportError as e:
        raise RuntimeError(f"gspread/google-auth not installed: {e}")

    creds_dict = json.loads(creds_json)
    creds = Credentials.from_service_account_info(
        creds_dict, scopes=["https://www.googleapis.com/auth/spreadsheets.readonly"]
    )
    client = gspread.authorize(creds)
    wb = client.open_by_key(INDEX_SPREADSHEET_ID)
    ws = wb.worksheet(INDEX_WORKSHEET_NAME)
    return pd.DataFrame(ws.get_all_records())


def read_index_via_gws() -> pd.DataFrame | None:
    """Read the private INDEX sheet using the gws CLI (for interactive dev)."""
    try:
        result = subprocess.run(
            ["gws", "sheets", "spreadsheets", "values", "get",
             "--params",
             json.dumps({"spreadsheetId": INDEX_SPREADSHEET_ID,
                         "range": f"{INDEX_WORKSHEET_NAME}!A1:Z"})],
            capture_output=True, text=True, check=True, timeout=30,
        )
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
        return None
    try:
        data = json.loads(result.stdout).get("values", [])
    except json.JSONDecodeError:
        return None
    if not data:
        return None
    header, *rows = data
    # Pad short rows to header length
    rows = [r + [""] * (len(header) - len(r)) for r in rows]
    return pd.DataFrame(rows, columns=header)


def fetch_tenzing_rows(df_index: pd.DataFrame) -> pd.DataFrame:
    frames = []
    failures = []
    for _, row in df_index.iterrows():
        project = str(row.get("Project Name", "")).strip()
        link = str(row.get("Tenzing Link", "")).strip()
        project_url = str(row.get("Project URL", "")).strip()
        if not (project and link):
            continue
        try:
            csv_url = convert_to_csv_url(link)
            df = pd.read_csv(csv_url, dtype=str).fillna("")
        except Exception as e:
            failures.append((project, str(e)))
            print(f"  WARN: {project}: {e}", file=sys.stderr)
            continue
        df["Project Name"] = project
        df["Project URL"] = project_url
        frames.append(df)
        print(f"  ✓ {project}: {len(df)} rows")
    if failures:
        print(f"\n{len(failures)} sheet(s) failed to fetch", file=sys.stderr)
    if not frames:
        raise RuntimeError("No Tenzing sheets successfully fetched")
    return pd.concat(frames, ignore_index=True)


def fetch_extra_roles() -> pd.DataFrame:
    try:
        return pd.read_csv(EXTRA_ROLES_URL, dtype=str).fillna("")
    except Exception as e:
        print(f"WARN: extra roles fetch failed: {e}", file=sys.stderr)
        return pd.DataFrame()


def normalize_orcid(s: str) -> str:
    if not s:
        return ""
    s = s.strip()
    m = ORCID_RE.search(s)
    return m.group(0) if m else ""


def build_contributor_table(tenzing: pd.DataFrame, extras: pd.DataFrame) -> pd.DataFrame:
    """Dedupe on ORCID (preferred) or normalized full name; aggregate projects."""
    frames = []

    if not tenzing.empty:
        t = tenzing.rename(columns={
            "First name": "first_name",
            "Middle name": "middle_name",
            "Surname": "surname",
            "ORCID iD": "orcid",
            "Project Name": "project",
        })
        frames.append(t[["first_name", "middle_name", "surname", "orcid", "project"]])

    if not extras.empty:
        e = extras.rename(columns={
            "First name": "first_name",
            "Middle name": "middle_name",
            "Surname": "surname",
            "ORCID": "orcid",
            "FORRT project(s)": "project",
        })
        cols_present = [c for c in ("first_name", "middle_name", "surname", "orcid", "project")
                        if c in e.columns]
        frames.append(e[cols_present])

    if not frames:
        raise RuntimeError("No contributor data collected")

    df = pd.concat(frames, ignore_index=True).fillna("")

    for col in ("first_name", "middle_name", "surname", "orcid", "project"):
        if col not in df.columns:
            df[col] = ""
        df[col] = df[col].astype(str).str.replace(r"[\r\n\t]+", " ", regex=True).str.strip()
    df["orcid"] = df["orcid"].apply(normalize_orcid)

    df = df[(df["first_name"] != "") & (df["surname"] != "")]

    df["full_name"] = (df["first_name"] + " " + df["surname"]).str.strip()
    df["dedupe_key"] = df["orcid"].where(df["orcid"] != "", df["full_name"].str.lower())

    def _first_nonblank(series: pd.Series) -> str:
        for v in series:
            if v:
                return v
        return ""

    grouped = df.groupby("dedupe_key", as_index=False).agg(
        first_name=("first_name", _first_nonblank),
        middle_name=("middle_name", _first_nonblank),
        surname=("surname", _first_nonblank),
        orcid=("orcid", _first_nonblank),
        full_name=("full_name", "first"),
        projects=("project", lambda x: "; ".join(sorted({p for p in x if p}))),
    )
    grouped = grouped.drop(columns=[]).sort_values("surname", key=lambda s: s.str.lower())
    return grouped[["orcid", "first_name", "middle_name", "surname", "full_name", "projects"]]


def main() -> int:
    df_index = read_index_via_gspread() or read_index_via_gws()
    if df_index is None:
        if OUT_PATH.exists():
            print(f"No credentials; keeping existing {OUT_PATH}")
            return 0
        print("ERROR: No credentials and no existing contributors.csv. "
              "Run `gws auth login` or set GSHEET_CREDENTIALS.", file=sys.stderr)
        return 2

    print(f"Index has {len(df_index)} projects")
    tenzing = fetch_tenzing_rows(df_index)
    extras = fetch_extra_roles()

    df = build_contributor_table(tenzing, extras)
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(OUT_PATH, index=False)

    with_orcid = (df["orcid"] != "").sum()
    print(f"\nWrote {OUT_PATH}: {len(df)} contributors, {with_orcid} with ORCID")
    return 0


if __name__ == "__main__":
    sys.exit(main())
