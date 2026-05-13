# WinRate AI

A League of Legends match analysis and win prediction web application. Search any summoner, view detailed match history, and see real-time AI predictions powered by an XGBoost model trained on ranked match data.

---

## Features

- **Player Search** — look up any NA summoner by name and tag (e.g. `Dedgurs#MEOW`)
- **Dashboard** — win rate, wins/losses, average KDA/CS/game length, most played champions
- **Champion Stats** — per-champion win rate, KDA, CS, damage, and gold across your recent games
- **Match History** — full match list with champion icons, role, KDA, CS, damage, gold, vision, game mode, and date
- **AI Win Prediction** — XGBoost model predicts the match outcome from team-level stats; badges show AI WIN / AI LOSS with accuracy tracking
- **Load More** — fetch additional matches from Riot on demand

---

## Architecture

```
Frontend (Vanilla JS MVC)
    └── fetch() → Flask REST API (Python)
                      ├── Riot Games API  (match data ingestion)
                      ├── MySQL Database  (player + match storage)
                      └── XGBoost Model  (win prediction)
```

| Layer | Tech |
|---|---|
| Frontend | HTML / CSS / Vanilla JS (MVC pattern) |
| Backend API | Python 3.11, Flask 3.0 |
| Database | MySQL 8, mysql-connector-python |
| ML Model | XGBoost, trained on ~1,200+ ranked matches |
| Data Source | Riot Games API (Americas region) |

---

## Prerequisites

- Python 3.11
- MySQL 8 running locally
- A [Riot Developer API key](https://developer.riotgames.com/) (free, rotates every 24 hours)

---

## Setup

### 1. Clone and install dependencies

```bash
git clone <repo-url>
cd CPSC-491-ForkedG1
pip install -r requirements.txt
```

### 2. Configure environment variables

Copy `.env.example` to `.env` and fill in your values:

```
RIOT_API_KEY=RGAPI-xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
DB_PASSWORD=your_mysql_root_password
```

### 3. Initialize the database

Start MySQL, then run:

```bash
py -3.11 -m database_setup.db_manager
```

This creates the `lol_prediction_db` schema with the `PLAYER`, `GAME`, and `MATCH_DATA` tables.

Alternatively, run the SQL directly:

```bash
mysql -u root -p < database_setup/init_db.sql
```

---

## Running the App

### Start the Flask API server

```bash
py -3.11 -m flask --app api_setup/flask_app.py run --port 5000
```

### Open the frontend

Navigate to **http://localhost:5000** in your browser. Flask serves the frontend directly — no separate step needed.

---

## Project Structure

```
├── api_setup/
│   ├── api_controller.py     # Riot API wrapper with rate limiting
│   ├── flask_app.py          # Flask REST API endpoints
│   └── tests/                # API and security tests
│
├── database_setup/
│   ├── db_manager.py         # MySQL CRUD operations
│   ├── init_db.sql           # Schema definition
│   └── tests/                # Database integration tests
│
├── frontend_prototype/
│   ├── index.html            # Entry point
│   ├── app.js                # MVC application (Model / View / Controller)
│   └── styles.css            # Stylesheet
│
├── projects/
│   └── ML Scripts/
│       ├── xgboost_model.py       # XGBoost training script
│       ├── feature_engineering.py # Extracts team-level diffs from raw JSON
│       ├── build_dataset_from_db.py
│       └── models/
│           └── latest_xgb.json   # Trained model (loaded at runtime)
│
├── data/
│   └── matches.csv           # Training dataset
│
├── demo.py                   # CLI demo: fetch + store a player's matches
├── collect_to_csv.py         # Bulk data collection script
└── requirements.txt
```

---

## AI Model

The XGBoost model predicts which team wins a match using 8 team-level features computed from the raw match JSON:

| Feature | Description |
|---|---|
| `gold_diff` | Team 1 gold earned minus Team 2 gold earned |
| `kill_diff` | Kill difference |
| `assist_diff` | Assist difference |
| `cs_diff` | Creep score difference |
| `vision_diff` | Vision score difference |
| `tower_diff` | Tower kills difference |
| `dragon_diff` | Dragon kills difference |
| `baron_diff` | Baron kills difference |

**Current model performance** (trained on 1,227 matches):
- Accuracy: **97.15%**
- F1 Score: **97.21%**
- AUC: **99.86%**

To retrain:

```bash
py -3.11 projects/ML\ Scripts/xgboost_model.py
```

---

## API Endpoints

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/api/player/<name>/<tag>` | Look up player by summoner name + tag |
| `POST` | `/api/matches/fetch` | Fetch and store matches from Riot API |
| `GET` | `/api/matches/<puuid>?count=N` | Get stored matches for a player (default 10, max 100) |
| `GET` | `/api/predict/match/<match_id>` | Run AI prediction on a stored match |
| `POST` | `/api/predict` | Run AI prediction on a raw match JSON body |

---

## Running Tests

```bash
# API tests
py -3.11 -m pytest api_setup/tests/

# Database tests
py -3.11 -m pytest database_setup/tests/

# ML pipeline tests
py -3.11 -m pytest projects/tests/
```

---

## Notes

- The Riot API dev key expires every 24 hours. Regenerate at [developer.riotgames.com](https://developer.riotgames.com/) and update `.env`.
- Arena (Queue 1700) and other non-Summoner's Rift modes may show **AI N/A** since the model is trained on 5v5 matches.
- The frontend calls `http://localhost:5000` — both the Flask server and the HTML file must be running locally.
