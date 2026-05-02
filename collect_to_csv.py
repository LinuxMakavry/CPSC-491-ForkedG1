"""
collect_to_csv.py
-----------------
Bulk-collect League of Legends match data from the Riot API and save
features directly to a CSV file — no database required.

How it works
------------
1. Starts with a list of seed summoners (name#tag).
2. For each summoner, fetches recent match IDs.
3. For each match, extracts team-level features and appends one row to
   the CSV immediately (safe against interruption).
4. Discovers new PUUIDs from match participants and queues them so the
   collection snowballs over time.
5. Respects Riot development-key rate limits automatically:
     - 20 requests / 1 second
     - 100 requests / 2 minutes

Usage
-----
  python collect_to_csv.py
  python collect_to_csv.py --out data/matches.csv --matches-per-player 20 --max-matches 2000

After your API key resets (24 h), run the same command again.
Already-seen match IDs stored in --seen-file are skipped automatically.

Seed summoners
--------------
Edit the SEED_SUMMONERS list below, or pass --seeds-file pointing to a
text file with one  Name#Tag  entry per line.
"""

import argparse
import csv
import os
import sys
import time
from collections import deque

import requests
from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# Default seed players — edit freely.
# Format:  ("Summoner Name", "TAG")
# ---------------------------------------------------------------------------
SEED_SUMMONERS = [
    ("Dedgurs", "Meow"),
    ("Hide on bush", "KR1"),
    ("Faker", "KR1"),
]

REGION = "americas"
PLATFORM = "na1"  # used for ranked/league endpoints if added later

# Riot development key hard limits (conservative — stay well under)
# 20 req/s  →  enforce ~0.06 s between requests (slightly under)
# 100 req/2 min  →  track a rolling window and sleep if needed
_REQ_TIMESTAMPS: list[float] = []
_RATE_1S = 20       # max requests per second
_RATE_2MIN = 100    # max requests per 2 minutes


def _throttle():
    """Block until it is safe to make the next request."""
    now = time.monotonic()

    # Drop timestamps older than 2 minutes
    cutoff_2min = now - 120.0
    cutoff_1s = now - 1.0

    while True:
        _REQ_TIMESTAMPS[:] = [t for t in _REQ_TIMESTAMPS if t > cutoff_2min]
        recent_1s = [t for t in _REQ_TIMESTAMPS if t > cutoff_1s]

        if len(_REQ_TIMESTAMPS) >= _RATE_2MIN:
            # Must wait until the oldest 2-min timestamp expires
            sleep_for = _REQ_TIMESTAMPS[0] + 120.0 - time.monotonic()
            if sleep_for > 0:
                print(f"  [rate-limit] 2-min bucket full — sleeping {sleep_for:.1f}s")
                time.sleep(sleep_for + 0.1)
            continue

        if len(recent_1s) >= _RATE_1S:
            sleep_for = recent_1s[0] + 1.0 - time.monotonic()
            if sleep_for > 0:
                time.sleep(sleep_for + 0.05)
            continue

        break

    _REQ_TIMESTAMPS.append(time.monotonic())


def _get(url: str, headers: dict, retries: int = 5) -> requests.Response | None:
    """Make a throttled GET request with automatic retry on 429."""
    for attempt in range(retries):
        _throttle()
        try:
            resp = requests.get(url, headers=headers, timeout=15)
        except requests.RequestException as exc:
            print(f"  [network error] {exc} — retry {attempt + 1}/{retries}")
            time.sleep(2 ** attempt)
            continue

        if resp.status_code == 200:
            return resp

        if resp.status_code == 429:
            wait = int(resp.headers.get("Retry-After", 10))
            print(f"  [429] Rate limited — waiting {wait}s")
            time.sleep(wait + 1)
            continue

        if resp.status_code in (404, 403, 401):
            # 401/403 → bad/expired key; 404 → resource not found
            print(f"  [HTTP {resp.status_code}] {url}")
            return None

        # Transient server errors
        if resp.status_code >= 500:
            wait = 2 ** attempt
            print(f"  [HTTP {resp.status_code}] server error — retry in {wait}s")
            time.sleep(wait)
            continue

        print(f"  [HTTP {resp.status_code}] unexpected — skipping")
        return None

    return None


def get_puuid(api_key: str, game_name: str, tag_line: str) -> str | None:
    url = (
        f"https://{REGION}.api.riotgames.com/riot/account/v1/accounts"
        f"/by-riot-id/{requests.utils.quote(game_name)}/{requests.utils.quote(tag_line)}"
    )
    resp = _get(url, {"X-Riot-Token": api_key})
    if resp:
        return resp.json().get("puuid")
    return None


def get_match_ids(api_key: str, puuid: str, count: int = 20) -> list[str]:
    url = (
        f"https://{REGION}.api.riotgames.com/lol/match/v5/matches"
        f"/by-puuid/{puuid}/ids?queue=420&count={count}"
    )
    resp = _get(url, {"X-Riot-Token": api_key})
    if resp:
        return resp.json()
    return []


def get_match(api_key: str, match_id: str) -> dict | None:
    url = f"https://{REGION}.api.riotgames.com/lol/match/v5/matches/{match_id}"
    resp = _get(url, {"X-Riot-Token": api_key})
    if resp:
        return resp.json()
    return None


def extract_features(match_json: dict) -> dict | None:
    """Extract team-level features from raw Riot match JSON.
    Returns None if the match cannot be parsed."""
    try:
        info = match_json["info"]
        participants = info["participants"]
        team_stats = info["teams"]

        if len(participants) != 10 or len(team_stats) != 2:
            return None

        team1 = participants[:5]
        team2 = participants[5:]

        def agg(team):
            return {
                "gold": sum(p["goldEarned"] for p in team),
                "kills": sum(p["kills"] for p in team),
                "deaths": sum(p["deaths"] for p in team),
                "assists": sum(p["assists"] for p in team),
                "cs": sum(p["totalMinionsKilled"] + p["neutralMinionsKilled"] for p in team),
                "vision": sum(p["visionScore"] for p in team),
                "damage": sum(p["totalDamageDealtToChampions"] for p in team),
                "healing": sum(p.get("totalHeal", 0) for p in team),
            }

        t1 = agg(team1)
        t2 = agg(team2)
        t1_obj = team_stats[0]["objectives"]
        t2_obj = team_stats[1]["objectives"]

        game_duration = info.get("gameDuration", 0)  # seconds

        return {
            "match_id": match_json["metadata"]["matchId"],
            "game_duration_s": game_duration,
            "gold_diff": t1["gold"] - t2["gold"],
            "kill_diff": t1["kills"] - t2["kills"],
            "death_diff": t1["deaths"] - t2["deaths"],
            "assist_diff": t1["assists"] - t2["assists"],
            "cs_diff": t1["cs"] - t2["cs"],
            "vision_diff": t1["vision"] - t2["vision"],
            "damage_diff": t1["damage"] - t2["damage"],
            "healing_diff": t1["healing"] - t2["healing"],
            "tower_diff": t1_obj["tower"]["kills"] - t2_obj["tower"]["kills"],
            "dragon_diff": t1_obj["dragon"]["kills"] - t2_obj["dragon"]["kills"],
            "baron_diff": t1_obj["baron"]["kills"] - t2_obj["baron"]["kills"],
            "rift_herald_diff": t1_obj.get("riftHerald", {}).get("kills", 0)
                                - t2_obj.get("riftHerald", {}).get("kills", 0),
            "t1_gold": t1["gold"],
            "t2_gold": t2["gold"],
            "t1_kills": t1["kills"],
            "t2_kills": t2["kills"],
            "win": 1 if team_stats[0]["win"] else 0,
        }
    except (KeyError, IndexError, TypeError):
        return None


def get_participant_puuids(match_json: dict) -> list[str]:
    """Return the 10 participant PUUIDs from a match."""
    try:
        return [p["puuid"] for p in match_json["info"]["participants"]]
    except (KeyError, TypeError):
        return []


def load_seen_ids(path: str) -> set[str]:
    if not os.path.exists(path):
        return set()
    with open(path, "r", encoding="utf-8") as f:
        return {line.strip() for line in f if line.strip()}


def append_seen_id(path: str, match_id: str):
    with open(path, "a", encoding="utf-8") as f:
        f.write(match_id + "\n")


def load_csv_match_ids(csv_path: str) -> set[str]:
    """Read match_ids already written to the CSV (used for dedup)."""
    if not os.path.exists(csv_path) or os.path.getsize(csv_path) == 0:
        return set()
    ids: set[str] = set()
    with open(csv_path, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            mid = row.get("match_id", "").strip()
            if mid:
                ids.add(mid)
    return ids


def load_seen_puuids(path: str) -> set[str]:
    if not os.path.exists(path):
        return set()
    with open(path, "r", encoding="utf-8") as f:
        return {line.strip() for line in f if line.strip()}


def append_seen_puuid(path: str, puuid: str):
    with open(path, "a", encoding="utf-8") as f:
        f.write(puuid + "\n")


def load_pending_queue(path: str) -> deque[str]:
    """Load the persisted PUUID queue from a previous interrupted run."""
    if not os.path.exists(path):
        return deque()
    with open(path, "r", encoding="utf-8") as f:
        return deque(line.strip() for line in f if line.strip())


def save_pending_queue(path: str, queue: deque[str]):
    """Write the current queue to disk (overwrites each time)."""
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for puuid in queue:
            f.write(puuid + "\n")


CSV_COLUMNS = [
    "match_id", "game_duration_s",
    "gold_diff", "kill_diff", "death_diff", "assist_diff",
    "cs_diff", "vision_diff", "damage_diff", "healing_diff",
    "tower_diff", "dragon_diff", "baron_diff", "rift_herald_diff",
    "t1_gold", "t2_gold", "t1_kills", "t2_kills",
    "win",
]


def ensure_csv_header(path: str):
    """Write CSV header only if the file is new/empty."""
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
            writer.writeheader()


def append_row(path: str, row: dict):
    with open(path, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS, extrasaction="ignore")
        writer.writerow(row)


def parse_args():
    parser = argparse.ArgumentParser(
        description="Collect LoL match data from Riot API → CSV (no DB needed)."
    )
    parser.add_argument(
        "--out", default="data/matches.csv",
        help="Output CSV path (default: data/matches.csv)"
    )
    parser.add_argument(
        "--seen-file", default="data/seen_match_ids.txt",
        help="File tracking already-collected match IDs for resume support."
    )
    parser.add_argument(
        "--seen-puuids", default="data/seen_puuids.txt",
        help="File tracking already-queried PUUIDs."
    )
    parser.add_argument(
        "--queue-file", default="data/pending_puuids.txt",
        help="Persisted PUUID queue — survives interruptions."
    )
    parser.add_argument(
        "--matches-per-player", type=int, default=20,
        help="Recent ranked matches to fetch per player (max 100, default 20)."
    )
    parser.add_argument(
        "--max-matches", type=int, default=5000,
        help="Stop after collecting this many NEW rows (default 5000)."
    )
    parser.add_argument(
        "--seeds-file", default=None,
        help="Optional text file with seed summoners, one 'Name#Tag' per line."
    )
    return parser.parse_args()


def main():
    args = parse_args()

    api_key = os.getenv("RIOT_API_KEY")
    if not api_key or api_key.lower().startswith("your_"):
        print("ERROR: Set RIOT_API_KEY in your .env file before running.")
        sys.exit(1)

    # ---- Load resume state ----
    # fetched_match_ids  = matches where API call was already made (skip re-fetch)
    # csv_match_ids      = matches already written to CSV (skip CSV write / dedup)
    fetched_match_ids = load_seen_ids(args.seen_file)
    csv_match_ids = load_csv_match_ids(args.out)
    seen_puuids = load_seen_puuids(args.seen_puuids)
    ensure_csv_header(args.out)

    print(f"Resuming: {len(csv_match_ids)} matches in CSV, "
          f"{len(fetched_match_ids)} fetched, "
          f"{len(seen_puuids)} PUUIDs already queried.")

    # ---- Restore persisted queue OR build from seeds ----
    puuid_queue: deque[str] = load_pending_queue(args.queue_file)

    if puuid_queue:
        # Filter out any that got processed since the queue was saved
        puuid_queue = deque(p for p in puuid_queue if p not in seen_puuids)
        print(f"Restored {len(puuid_queue)} PUUIDs from previous run's queue.")
    else:
        seeds = list(SEED_SUMMONERS)

        if args.seeds_file and os.path.exists(args.seeds_file):
            with open(args.seeds_file, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if "#" in line:
                        name, tag = line.split("#", 1)
                        seeds.append((name.strip(), tag.strip()))

        print(f"\nResolving {len(seeds)} seed summoner(s)...")
        for name, tag in seeds:
            puuid = get_puuid(api_key, name, tag)
            if puuid and puuid not in seen_puuids:
                puuid_queue.append(puuid)
                print(f"  Queued {name}#{tag} → {puuid[:16]}…")
            elif puuid:
                print(f"  Skipping {name}#{tag} (already queried).")
            else:
                print(f"  Could not resolve {name}#{tag} — check name/tag.")

    if not puuid_queue:
        print("No new PUUIDs to process. Add more seeds or update seen_puuids.txt.")
        sys.exit(0)

    # ---- Main crawl loop ----
    new_rows = 0
    api_errors = 0
    MAX_API_ERRORS = 10  # stop if key likely expired

    print(f"\nStarting crawl — target: {args.max_matches} new matches.\n")

    while puuid_queue and new_rows < args.max_matches:
        puuid = puuid_queue.popleft()

        if puuid in seen_puuids:
            continue

        append_seen_puuid(args.seen_puuids, puuid)
        seen_puuids.add(puuid)

        match_ids = get_match_ids(api_key, puuid, count=min(args.matches_per_player, 100))

        if match_ids is None:
            api_errors += 1
            if api_errors >= MAX_API_ERRORS:
                print("Too many API errors — key may be expired. Stopping.")
                break
            continue

        api_errors = 0  # reset on success

        for match_id in match_ids:
            if new_rows >= args.max_matches:
                break

            # Skip API call entirely only if already fetched AND PUUIDs harvested
            if match_id in fetched_match_ids:
                continue

            match_json = get_match(api_key, match_id)
            fetched_match_ids.add(match_id)
            append_seen_id(args.seen_file, match_id)

            if not match_json:
                api_errors += 1
                if api_errors >= MAX_API_ERRORS:
                    print("Too many API errors — key may be expired. Stopping.")
                    puuid_queue.clear()
                    break
                continue

            api_errors = 0

            # Write to CSV only if not already there (dedup)
            if match_id not in csv_match_ids:
                features = extract_features(match_json)
                if features:
                    append_row(args.out, features)
                    csv_match_ids.add(match_id)
                    new_rows += 1
                    if new_rows % 50 == 0:
                        print(f"  Collected {new_rows} rows so far "
                              f"({len(puuid_queue)} PUUIDs queued)…")

            # Always harvest participant PUUIDs (even for already-written matches)
            for participant_puuid in get_participant_puuids(match_json):
                if participant_puuid not in seen_puuids:
                    puuid_queue.append(participant_puuid)

            # Persist queue every 10 matches so Ctrl+C loses minimal progress
            if (new_rows % 10) == 0 or (match_id in csv_match_ids):
                save_pending_queue(args.queue_file, puuid_queue)

    # Save final queue state
    save_pending_queue(args.queue_file, puuid_queue)

    print(f"\nDone. {new_rows} new rows written to: {args.out}")
    print(f"Total unique matches seen (all runs): {len(fetched_match_ids)}")


if __name__ == "__main__":
    main()
