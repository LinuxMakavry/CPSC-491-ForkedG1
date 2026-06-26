import argparse
import json
import os
import sys

import pandas as pd

CURRENT_DIR = os.path.abspath(os.path.dirname(__file__))
REPO_ROOT = os.path.abspath(os.path.join(CURRENT_DIR, "..", ".."))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)
if CURRENT_DIR not in sys.path:
    sys.path.insert(0, CURRENT_DIR)

from database_setup.db_manager import get_raw_match_json, get_timeline_for_match, get_match_ids_with_timelines
from feature_engineering import extract_15min_features


def build_dataset_from_db(limit=500, out_csv=None):
    """Build a training dataset using 15-minute timeline features.

    Only matches that have both MATCH_DATA and TIMELINE_DATA stored are included.
    The win label is derived from the match JSON (1 = blue side / team 100 wins).
    """
    rows = []
    skipped = 0
    # Only pull matches that have timeline data — required for mid-game features
    eligible = get_match_ids_with_timelines(limit=limit)

    for row in eligible:
        match_id = row.get("match_id")
        raw_match = get_raw_match_json(match_id)
        raw_timeline = get_timeline_for_match(match_id)

        if not raw_match or not raw_timeline:
            skipped += 1
            continue

        try:
            match_json    = json.loads(raw_match)
            timeline_json = json.loads(raw_timeline)
            # Pass both so the win label is attached from match JSON
            features = extract_15min_features(timeline_json, match_json=match_json)
            rows.append(features)
        except Exception:
            skipped += 1

    df = pd.DataFrame(rows)
    if not df.empty:
        df.fillna(0, inplace=True)

    if out_csv:
        os.makedirs(os.path.dirname(out_csv) or ".", exist_ok=True)
        df.to_csv(out_csv, index=False)

    return {
        "rows": int(df.shape[0]),
        "skipped": int(skipped),
        "limit": int(limit),
        "dataset_path": out_csv,
    }


def main():
    default_out = os.path.join(CURRENT_DIR, "data", "train.csv")
    parser = argparse.ArgumentParser(description="Build ML dataset from 15-min timeline features in MySQL.")
    parser.add_argument("--limit", type=int, default=500, help="Max recent matches to read from DB.")
    parser.add_argument("--out-csv", default=default_out, help="Output dataset CSV path.")
    args = parser.parse_args()

    result = build_dataset_from_db(limit=args.limit, out_csv=args.out_csv)
    print("Dataset build complete.")
    print("Rows:", result["rows"])
    print("Skipped:", result["skipped"])
    print("Limit:", result["limit"])
    print("Saved to:", result["dataset_path"])


if __name__ == "__main__":
    main()
