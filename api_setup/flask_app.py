import sys
import os
import json

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

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
CORS(app)

_FRONTEND = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'frontend_prototype'))

@app.route('/')
def serve_index():
    return send_from_directory(_FRONTEND, 'index.html')

@app.route('/<path:filename>')
def serve_static(filename):
    return send_from_directory(_FRONTEND, filename)

# --- Lazy model loader ---
_MODEL = None
_MODEL_PATHS = [
    os.path.abspath(
        os.path.join(os.path.dirname(__file__), '..', 'projects', 'ML Scripts', 'models', 'latest_xgb.json')
    ),
    os.path.abspath(
        os.path.join(os.path.dirname(__file__), '..', 'projects', 'tests', 'output', 'xgb_model.json')
    ),
]

FEATURE_COLS = [
    "gold_diff", "kill_diff", "assist_diff", "cs_diff",
    "vision_diff", "tower_diff", "dragon_diff", "baron_diff"
]

def _get_model():
    global _MODEL
    if _MODEL is None:
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
        features = extract_team_features(match_json)
    except Exception as e:
        return None, f"Invalid match JSON: {e}"
    model = _get_model()
    if model is None:
        return None, "Model not loaded"
    arr = np.array([[features[k] for k in FEATURE_COLS]])
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
        puuid = provider.get_puuid(game_name, tag_line)
    except Exception:
        return jsonify({"error": "Riot API unavailable"}), 503

    if not puuid:
        return jsonify({"error": "Player not found"}), 404

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
        provider.fetch_and_store_matches(puuid, count=count)
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


if __name__ == "__main__":
    app.run(debug=True, port=5000)
