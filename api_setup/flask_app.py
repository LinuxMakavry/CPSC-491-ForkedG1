import sys
import os
import json

# Add project root to sys.path so sibling packages (api_setup, database_setup) are importable
_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

# Add ML Scripts folder so feature_engineering can be imported without a package prefix
_ML_SCRIPTS = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'projects', 'ML Scripts'))
if _ML_SCRIPTS not in sys.path:
    sys.path.insert(0, _ML_SCRIPTS)

from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS
import numpy as np
from feature_engineering import extract_team_features

from api_setup.api_controller import RiotAPIProvider
from database_setup.db_manager import (
    save_player, get_player_stats,
    get_matches_for_player, get_raw_match_json, get_recent_matches,
    update_player_wins_losses
)

app = Flask(__name__)
CORS(app)  # Allow cross-origin requests from the frontend during local development

# Absolute path to the frontend folder so Flask can serve static files
_FRONTEND = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'frontend_prototype'))

@app.route('/')
def serve_index():
    # Serve the SPA entry point — Flask doubles as both API server and static host
    return send_from_directory(_FRONTEND, 'index.html')

@app.route('/<path:filename>')
def serve_static(filename):
    # Serve any other frontend asset (app.js, styles.css, etc.)
    return send_from_directory(_FRONTEND, filename)

# --- Lazy model loader ---
# The XGBoost model is loaded on first prediction request, not at startup,
# so the server still starts even if the model file hasn't been trained yet.
_MODEL = None
_MODEL_PATHS = [
    os.path.abspath(
        os.path.join(os.path.dirname(__file__), '..', 'projects', 'ML Scripts', 'models', 'latest_xgb.json')
    ),
    # Fallback path from the test suite output directory
    os.path.abspath(
        os.path.join(os.path.dirname(__file__), '..', 'projects', 'tests', 'output', 'xgb_model.json')
    ),
]

# The 8 team-level differential features the XGBoost model was trained on
FEATURE_COLS = [
    "gold_diff", "kill_diff", "assist_diff", "cs_diff",
    "vision_diff", "tower_diff", "dragon_diff", "baron_diff"
]

def _get_model():
    global _MODEL
    if _MODEL is None:
        # Walk the model path list and load the first one that exists on disk
        model_path = next((path for path in _MODEL_PATHS if os.path.exists(path)), None)
        if model_path is None:
            return None
        import xgboost as xgb
        _MODEL = xgb.Booster()
        _MODEL.load_model(model_path)
    return _MODEL

def _run_prediction(match_json):
    import xgboost as xgb
    try:
        # extract_team_features computes blue-minus-red differentials from raw Riot JSON
        features = extract_team_features(match_json)
    except Exception as e:
        return None, f"Invalid match JSON: {e}"
    model = _get_model()
    if model is None:
        return None, "Model not loaded"
    # Build a (1, 8) array in the same column order the model was trained on
    arr = np.array([[features[k] for k in FEATURE_COLS]])
    # Model outputs P(Team 1 wins); >= 0.5 → Team 1, < 0.5 → Team 2
    prob = float(model.predict(xgb.DMatrix(arr))[0])
    return {
        "team1_win_probability": prob,
        "predicted_winner": "Team 1" if prob >= 0.5 else "Team 2",
        "features_used": {k: features[k] for k in FEATURE_COLS}
    }, None


# --- Endpoints ---

@app.route("/api/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "service": "WinRate AI API"}), 200


@app.route("/api/player/<game_name>/<tag_line>", methods=["GET"])
def get_player(game_name, tag_line):
    try:
        provider = RiotAPIProvider()
        # Resolve Riot ID (name#tag) to the account-level PUUID via Riot API
        puuid = provider.get_puuid(game_name, tag_line)
    except Exception:
        return jsonify({"error": "Riot API unavailable"}), 503

    if not puuid:
        return jsonify({"error": "Player not found"}), 404

    # Upsert player record so PUUID is tracked in the local DB
    save_player(puuid, game_name)
    stats = get_player_stats(puuid) or {}
    wins = stats.get("wins", 0) or 0
    losses = stats.get("losses", 0) or 0
    total = wins + losses
    win_rate = f"{round(wins / total * 100)}%" if total > 0 else "0%"

    return jsonify({
        "summoner_name": game_name,
        "puuid": puuid,
        "wins": wins,
        "losses": losses,
        "total_games": wins + losses,
        "win_rate": win_rate
    }), 200


@app.route("/api/matches/fetch", methods=["POST"])
def fetch_matches():
    body = request.get_json(silent=True) or {}
    puuid = body.get("puuid")
    if not puuid:
        return jsonify({"error": "puuid is required"}), 400
    count = body.get("count", 5)
    try:
        provider = RiotAPIProvider()
        # Pull match IDs from Riot, download full match JSON, and store in MATCH_DATA table
        provider.fetch_and_store_matches(puuid, count=count)
        # Recompute wins/losses from stored match data and update PLAYER table
        update_player_wins_losses(puuid)
    except Exception as e:
        return jsonify({"error": f"Failed to fetch matches: {e}"}), 503
    return jsonify({"message": "Matches fetched and stored", "count": count}), 200


@app.route("/api/matches/<puuid>", methods=["GET"])
def get_matches(puuid):
    try:
        limit = min(int(request.args.get('count', 10)), 100)
    except (TypeError, ValueError):
        limit = 10
    rows = get_matches_for_player(puuid, limit=limit)
    matches = []
    for row in rows:
        matches.append({
            "match_id":    row["match_id"],
            "game_date":   row["game_date"].isoformat() if row["game_date"] else None,
            "game_length": row["game_length"],
            "winning_team": row["winning_team"],
            "player_won":  row.get("player_won"),
            "champion":    row.get("champion", ""),
            "position":    row.get("position", ""),
            "kills":       row.get("kills", 0),
            "deaths":      row.get("deaths", 0),
            "assists":     row.get("assists", 0),
            "cs":          row.get("cs", 0),
            "damage":      row.get("damage", 0),
            "gold":        row.get("gold", 0),
            "vision":      row.get("vision", 0),
            "queue_id":    row.get("queue_id"),
        })
    return jsonify({"puuid": puuid, "matches": matches}), 200


@app.route("/api/predict", methods=["POST"])
def predict():
    match_json = request.get_json(silent=True)
    if not match_json:
        return jsonify({"error": "Request body must be a valid match JSON object"}), 400
    result, err = _run_prediction(match_json)
    if err:
        status = 503 if "Model not loaded" in err else 400
        return jsonify({"error": err}), status
    return jsonify(result), 200


@app.route("/api/predict/match/<match_id>", methods=["GET"])
def predict_from_db(match_id):
    raw = get_raw_match_json(match_id)
    if raw is None:
        return jsonify({"error": "Match not found in database"}), 404
    try:
        match_json = json.loads(raw)
    except Exception:
        return jsonify({"error": "Stored match JSON is malformed"}), 500
    result, err = _run_prediction(match_json)
    if err:
        status = 503 if "Model not loaded" in err else 400
        return jsonify({"error": err}), status
    return jsonify(result), 200


def _build_player_context(puuid):
    """Build a rich text summary of player stats from stored match data for the LLM."""
    stats = get_player_stats(puuid) or {}
    matches = get_matches_for_player(puuid, limit=50)

    if not matches:
        return "No match data available for this player yet."

    summoner_name = stats.get("summoner_name", "Unknown")
    wins = stats.get("wins", 0) or 0
    losses = stats.get("losses", 0) or 0
    total = wins + losses
    wr = round(wins / total * 100) if total > 0 else 0

    valid = [m for m in matches if m.get("kills") is not None]

    # Compute overall averages across all stored matches with participant data
    avg_kills   = round(sum(m["kills"]   for m in valid) / len(valid), 1) if valid else 0
    avg_deaths  = round(sum(m["deaths"]  for m in valid) / len(valid), 1) if valid else 0
    avg_assists = round(sum(m["assists"] for m in valid) / len(valid), 1) if valid else 0
    avg_cs      = round(sum(m["cs"]      for m in valid) / len(valid), 1) if valid else 0
    avg_vision  = round(sum(m["vision"]  for m in valid) / len(valid), 1) if valid else 0
    avg_damage  = round(sum(m["damage"]  for m in valid) / len(valid))    if valid else 0
    avg_gold    = round(sum(m["gold"]    for m in valid) / len(valid))    if valid else 0

    # Per-champion breakdown
    champ_map = {}
    for m in valid:
        champ = m.get("champion") or "Unknown"
        if champ not in champ_map:
            champ_map[champ] = {"games": 0, "wins": 0, "kills": 0, "deaths": 0, "assists": 0, "cs": 0}
        d = champ_map[champ]
        d["games"] += 1
        if m.get("player_won"):
            d["wins"] += 1
        d["kills"]   += m["kills"]
        d["deaths"]  += m["deaths"]
        d["assists"] += m["assists"]
        d["cs"]      += m["cs"]

    # Sort champions by games played descending so the LLM sees the most relevant first
    champ_list = sorted(champ_map.items(), key=lambda x: x[1]["games"], reverse=True)

    # Per-role breakdown
    role_map = {}
    for m in valid:
        role = m.get("position") or "UNKNOWN"
        if role not in role_map:
            role_map[role] = {"games": 0, "wins": 0}
        role_map[role]["games"] += 1
        if m.get("player_won"):
            role_map[role]["wins"] += 1

    # Recent form: last 5 vs rest to detect improvement or decline
    last5 = valid[:5]
    prior = valid[5:]
    last5_wr = round(sum(1 for m in last5 if m.get("player_won")) / len(last5) * 100) if last5 else None
    prior_wr = round(sum(1 for m in prior if m.get("player_won")) / len(prior) * 100) if prior else None

    lines = [
        f"=== PLAYER: {summoner_name} ===",
        f"Record: {wins}W / {losses}L ({wr}% win rate) across {total} stored games",
        f"Avg KDA: {avg_kills}/{avg_deaths}/{avg_assists}",
        f"Avg CS: {avg_cs} | Avg Vision: {avg_vision} | Avg Damage: {avg_damage:,} | Avg Gold: {avg_gold:,}",
        "",
        "=== CHAMPION BREAKDOWN (sorted by games played) ===",
    ]
    for champ, d in champ_list[:10]:
        g = d["games"]
        cwr  = round(d["wins"]    / g * 100) if g > 0 else 0
        ck   = round(d["kills"]   / g, 1)
        cd   = round(d["deaths"]  / g, 1)
        ca   = round(d["assists"] / g, 1)
        ccs  = round(d["cs"]      / g, 1)
        lines.append(f"  {champ}: {g} games, {cwr}% WR, {ck}/{cd}/{ca} KDA, {ccs} avg CS")

    lines += ["", "=== ROLE BREAKDOWN ==="]
    # Map Riot's internal position strings to human-readable labels.
    # UNKNOWN covers ARAM, Arena, and other modes where position is undefined.
    ROLE_NOTE = {
        "UNKNOWN": "UNKNOWN (ARAM/Arena/other modes)",
        "TOP": "TOP", "JUNGLE": "JUNGLE", "MIDDLE": "MIDDLE",
        "BOTTOM": "BOTTOM", "UTILITY": "UTILITY (Support)",
    }
    for role, d in sorted(role_map.items(), key=lambda x: x[1]["games"], reverse=True):
        g = d["games"]
        rwr = round(d["wins"] / g * 100) if g > 0 else 0
        label = ROLE_NOTE.get(role, role)
        lines.append(f"  {label}: {g} games, {rwr}% WR")

    if last5_wr is not None:
        trend = ""
        if prior_wr is not None:
            trend = " (improving)" if last5_wr > prior_wr else " (declining)" if last5_wr < prior_wr else " (stable)"
        lines += ["", "=== RECENT FORM ===", f"  Last 5 games: {last5_wr}% WR{trend}"]
        if prior_wr is not None:
            lines.append(f"  Prior games:  {prior_wr}% WR")

    return "\n".join(lines)


@app.route("/api/chat", methods=["POST"])
def chat():
    body = request.get_json(silent=True) or {}
    puuid    = body.get("puuid", "").strip()
    question = body.get("question", "").strip()
    history  = body.get("history", [])

    if not puuid or not question:
        return jsonify({"error": "puuid and question are required"}), 400

    if not isinstance(history, list):
        history = []

    # Build a structured text block from DB stats to inject into the system prompt (RAG pattern)
    context = _build_player_context(puuid)

    # System prompt instructs the model to stay grounded in the player's actual data
    system_prompt = (
        "You are WinRateAI Coach, a personalized League of Legends coaching assistant.\n"
        "You have access to the player's match history statistics below.\n"
        "Always reference the player's actual data in your responses.\n"
        "Be concise and actionable. Tie every suggestion directly to their statistics.\n\n"
        + context
    )

    messages = [{"role": "system", "content": system_prompt}]

    # Include last 10 turns of conversation history to maintain multi-turn context
    for entry in history[-10:]:
        if (isinstance(entry, dict)
                and entry.get("role") in ("user", "assistant")
                and isinstance(entry.get("content"), str)):
            # Cap each history message at 2000 chars to keep token usage predictable
            messages.append({"role": entry["role"], "content": entry["content"][:2000]})

    # Append the current user question; cap at 1000 chars to prevent prompt injection bloat
    messages.append({"role": "user", "content": question[:1000]})

    try:
        from openai import OpenAI
        # NRP-hosted gpt-oss endpoint; uses OpenAI-compatible API with a custom base_url
        client = OpenAI(
            api_key=os.getenv("NRP_LLM_API_KEY"),
            base_url="https://ellm.nrp-nautilus.io/v1"
        )
        response = client.chat.completions.create(
            model="gpt-oss",
            messages=messages,
            max_tokens=2000,   # Raised from 500 to avoid mid-sentence cutoffs
            temperature=0.7    # Moderate creativity; lower = more deterministic advice
        )
        reply = response.choices[0].message.content
        return jsonify({"reply": reply}), 200
    except Exception as e:
        return jsonify({"error": f"LLM unavailable: {str(e)}"}), 503


if __name__ == "__main__":
    app.run(debug=True, port=5000)
