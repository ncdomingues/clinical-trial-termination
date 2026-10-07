"""Download completed and terminated phase 2/3 trials from the ClinicalTrials.gov API (v2).

Scope: interventional phase 2 or 3 trials that started between 2008 and 2019 and are now
COMPLETED or TERMINATED. Trials that started later are left out because many are still
running: keeping them would over-represent short trials among the finished ones.

Output: data/raw/trials.jsonl.gz (one JSON record per trial, results section excluded).
Run:    python src/download_data.py   (about 47,000 trials, a few minutes)
"""
import gzip
import json
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "raw" / "trials.jsonl.gz"
API = "https://clinicaltrials.gov/api/v2/studies"

PARAMS = {
    "filter.overallStatus": "COMPLETED,TERMINATED",
    "query.term": ("AREA[StudyType]INTERVENTIONAL AND AREA[Phase](PHASE2 OR PHASE3) "
                   "AND AREA[StartDate]RANGE[2008-01-01,2019-12-31]"),
    "fields": "protocolSection,derivedSection,hasResults",
    "pageSize": 1000,
    "countTotal": "true",
}


def main():
    OUT.parent.mkdir(parents=True, exist_ok=True)
    session = requests.Session()
    params, n, total = dict(PARAMS), 0, None
    with gzip.open(OUT, "wt", encoding="utf-8") as f:
        while True:
            resp = session.get(API, params=params, timeout=120)
            resp.raise_for_status()
            page = resp.json()
            total = total or page.get("totalCount")
            for study in page["studies"]:
                f.write(json.dumps(study) + "\n")
            n += len(page["studies"])
            print(f"\r  {n:,} / {total:,} trials", end="", flush=True)
            token = page.get("nextPageToken")
            if not token:
                break
            params = dict(PARAMS, pageToken=token)
            params.pop("countTotal")
    print(f"\nSaved {OUT.relative_to(ROOT)} ({OUT.stat().st_size / 1e6:.0f} MB)")


if __name__ == "__main__":
    main()
