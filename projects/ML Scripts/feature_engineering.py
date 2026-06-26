from preprocessing import rank_to_numeric


def extract_team_features(match_json):
    # Takes raw Riot match JSON and returns a post-game feature dict for ONE match.
    # NOTE: These are end-of-game stats and suffer from outcome leakage (the winning
    # team almost always dominates all metrics). Kept for backward compatibility.

    participants = match_json["info"]["participants"]

    team1 = participants[:5]
    team2 = participants[5:]

    def aggregate_team(team):
        total_gold = sum(p["goldEarned"] for p in team)
        total_kills = sum(p["kills"] for p in team)
        total_assists = sum(p["assists"] for p in team)
        total_cs = sum(p["totalMinionsKilled"] + p["neutralMinionsKilled"] for p in team)
        total_vision = sum(p["visionScore"] for p in team)

        return {
            "gold": total_gold,
            "kills": total_kills,
            "assists": total_assists,
            "cs": total_cs,
            "vision": total_vision
        }

    t1 = aggregate_team(team1)
    t2 = aggregate_team(team2)

    # Objectives
    team_stats = match_json["info"]["teams"]

    t1_obj = team_stats[0]["objectives"]
    t2_obj = team_stats[1]["objectives"]

    features = {
        "gold_diff": t1["gold"] - t2["gold"],
        "kill_diff": t1["kills"] - t2["kills"],
        "assist_diff": t1["assists"] - t2["assists"],
        "cs_diff": t1["cs"] - t2["cs"],
        "vision_diff": t1["vision"] - t2["vision"],
        "tower_diff": t1_obj["tower"]["kills"] - t2_obj["tower"]["kills"],
        "dragon_diff": t1_obj["dragon"]["kills"] - t2_obj["dragon"]["kills"],
        "baron_diff": t1_obj["baron"]["kills"] - t2_obj["baron"]["kills"],
        "win": 1 if team_stats[0]["win"] else 0
    }

    return features


def extract_15min_features(timeline_json, match_json=None):
    """Extract team-level differential features at the 15-minute mark from timeline data.

    Uses per-minute participant frames for gold/CS and scans all events up to 15 min
    for kills, towers, and dragons. Baron is excluded (spawns at 20 min).

    Args:
        timeline_json: Raw Riot match timeline JSON (/matches/{id}/timeline).
        match_json:    Optional raw match JSON used to attach the 'win' label.

    Returns:
        Dict with keys: gold_diff, kill_diff, cs_diff, tower_diff, dragon_diff,
        and optionally 'win' (1 = team 100 wins) if match_json is supplied.
    """
    TARGET_MS = 15 * 60 * 1000  # 900,000 ms — the 15-minute snapshot

    frames = timeline_json.get("info", {}).get("frames", [])
    if not frames:
        raise ValueError("No timeline frames found in timeline JSON")

    # Walk forward through frames and keep the last one at or before 15 min.
    # Frames are ~60 s apart so frame index ~15 corresponds to the 15-min mark.
    frame_15 = None
    for frame in frames:
        if frame.get("timestamp", 0) <= TARGET_MS:
            frame_15 = frame
        else:
            break
    if frame_15 is None:
        # Game ended before 15 min (very rare) — use the last available frame
        frame_15 = frames[-1]

    pf = frame_15.get("participantFrames", {})

    # Participant IDs 1-5 belong to team 100 (blue side); 6-10 to team 200 (red side)
    t1_gold = sum(pf.get(str(i), {}).get("totalGold", 0) for i in range(1, 6))
    t2_gold = sum(pf.get(str(i), {}).get("totalGold", 0) for i in range(6, 11))

    t1_cs = sum(
        pf.get(str(i), {}).get("minionsKilled", 0) + pf.get(str(i), {}).get("jungleMinionsKilled", 0)
        for i in range(1, 6)
    )
    t2_cs = sum(
        pf.get(str(i), {}).get("minionsKilled", 0) + pf.get(str(i), {}).get("jungleMinionsKilled", 0)
        for i in range(6, 11)
    )

    # Scan all events in every frame up to 15 min for kill/tower/dragon counts
    t1_kills = t2_kills = 0
    t1_towers = t2_towers = 0
    t1_dragons = t2_dragons = 0

    for frame in frames:
        if frame.get("timestamp", 0) > TARGET_MS:
            break
        for event in frame.get("events", []):
            etype = event.get("type", "")

            if etype == "CHAMPION_KILL":
                killer_id = event.get("killerId", 0)
                if 1 <= killer_id <= 5:
                    t1_kills += 1
                elif 6 <= killer_id <= 10:
                    t2_kills += 1

            elif etype == "BUILDING_KILL":
                # teamId = the team whose building was destroyed.
                # If teamId=200, a red-side tower fell → blue team (100) scored a tower kill.
                team_id = event.get("teamId", 0)
                if team_id == 200:
                    t1_towers += 1
                elif team_id == 100:
                    t2_towers += 1

            elif etype == "ELITE_MONSTER_KILL":
                if event.get("monsterType", "") == "DRAGON":
                    killer_team = event.get("killerTeamId", 0)
                    if killer_team == 100:
                        t1_dragons += 1
                    elif killer_team == 200:
                        t2_dragons += 1

    features = {
        "gold_diff":   t1_gold   - t2_gold,
        "kill_diff":   t1_kills  - t2_kills,
        "cs_diff":     t1_cs     - t2_cs,
        "tower_diff":  t1_towers - t2_towers,
        "dragon_diff": t1_dragons - t2_dragons,
    }

    # Attach win label from match JSON if provided (1 = team 100 / blue side wins)
    if match_json is not None:
        team_stats = match_json.get("info", {}).get("teams", [])
        if team_stats:
            features["win"] = 1 if team_stats[0].get("win") else 0

    return features