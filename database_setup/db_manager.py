import mysql.connector
import os
from dotenv import load_dotenv
from datetime import datetime
import json

load_dotenv()

def get_db_connection():
    # Returns a new MySQL connection using credentials from the .env file.
    # Returns None (rather than raising) so callers can handle the failure gracefully.
    try:
        connection = mysql.connector.connect(
            host="127.0.0.1",
            port=3306,
            user="root",
            password=os.getenv("DB_PASSWORD"),  # loaded from .env by python-dotenv
            database="lol_prediction_db",
            auth_plugin='mysql_native_password' 
        )
        return connection
    except Exception as e:
        print(f"Database Connection Error: {e}")
        return None

def save_player(puuid, summoner_name):
    # INSERT IGNORE means we silently skip if the PUUID already exists,
    # so repeated logins for the same player don't cause errors.
    conn = get_db_connection()
    if conn:
        cursor = conn.cursor()
        sql = "INSERT IGNORE INTO PLAYER (summoner_id, summoner_name) VALUES (%s, %s)"
        cursor.execute(sql, (puuid, summoner_name))
        conn.commit()
        cursor.close()
        conn.close()
        print(f"Saved {summoner_name} to database.")

def initialize_db():
    conn = get_db_connection()
    if conn:
        cursor = conn.cursor()
        # Create PLAYER table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS PLAYER (
                summoner_id VARCHAR(100) PRIMARY KEY,
                summoner_name VARCHAR(45),
                wins INT DEFAULT 0,
                losses INT DEFAULT 0,
                highest_season_tier VARCHAR(20)
            )
        """)
        # Create GAME table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS GAME (
                game_id VARCHAR(50) PRIMARY KEY,
                game_date DATETIME,
                game_length INT,
                winning_team CHAR(10)
            )
        """)
        # Create MATCH_DATA table for raw JSON storage
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS MATCH_DATA (
                match_id VARCHAR(50) PRIMARY KEY,
                game_date DATETIME,
                game_length INT,
                winning_team CHAR(10),
                raw_json LONGTEXT
            )
        """)
        conn.commit()
        cursor.close()
        conn.close()
        print("Database initialized: Tables verified/created.")

def save_match_data(match_json):
    # Stores the full Riot match JSON plus pre-extracted summary fields.
    # The raw_json column lets us re-derive any stat later without re-calling the API.
    if not match_json:
        return

    metadata = match_json.get("metadata", {})
    info = match_json.get("info", {})

    match_id = metadata.get("matchId")
    if not match_id:
        return

    # gameCreation comes from Riot in milliseconds; convert to a Python datetime
    game_creation_ms = info.get("gameCreation")
    game_date = None
    if game_creation_ms:
        game_date = datetime.utcfromtimestamp(game_creation_ms / 1000)

    game_length = info.get("gameDuration")  # seconds
    # Find which team won by checking the 'win' flag on each team object
    winning_team = None
    teams = info.get("teams", [])
    for team in teams:
        if team.get("win"):
            winning_team = str(team.get("teamId"))  # '100' = blue side, '200' = red side
            break

    raw_json = json.dumps(match_json)

    conn = get_db_connection()
    if conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT IGNORE INTO MATCH_DATA (match_id, game_date, game_length, winning_team, raw_json)
            VALUES (%s, %s, %s, %s, %s)
            """,
            (match_id, game_date, game_length, winning_team, raw_json),
        )
        cursor.execute(
            """
            INSERT IGNORE INTO GAME (game_id, game_date, game_length, winning_team)
            VALUES (%s, %s, %s, %s)
            """,
            (match_id, game_date, game_length, winning_team),
        )
        conn.commit()
        cursor.close()
        conn.close()
        print(f"Saved match {match_id} to database.")

def update_player_wins_losses(puuid):
    """Recomputes wins/losses for a player from stored MATCH_DATA and updates PLAYER table."""
    conn = get_db_connection()
    if not conn:
        return
    cursor = conn.cursor(dictionary=True)
    # JSON_SEARCH finds all MATCH_DATA rows where this PUUID appears in participants.
    # This is slower than a join table but avoids a schema migration for the prototype.
    cursor.execute(
        """
        SELECT raw_json FROM MATCH_DATA
        WHERE JSON_SEARCH(raw_json, 'one', %s, NULL, '$.metadata.participants') IS NOT NULL
        """,
        (puuid,)
    )
    rows = cursor.fetchall()
    wins = 0
    losses = 0
    for row in rows:
        try:
            match_json = json.loads(row["raw_json"])
            # Walk participants list to find this specific player's outcome
            for p in match_json.get("info", {}).get("participants", []):
                if p.get("puuid") == puuid:
                    if p.get("win"):
                        wins += 1
                    else:
                        losses += 1
                    break
        except Exception:
            continue
    cursor.execute(
        "UPDATE PLAYER SET wins = %s, losses = %s WHERE summoner_id = %s",
        (wins, losses, puuid)
    )
    conn.commit()
    cursor.close()
    conn.close()
    print(f"Updated stats for {puuid}: {wins}W / {losses}L")

def get_player_stats(puuid):
    conn = get_db_connection()
    if conn:
        cursor = conn.cursor(dictionary=True)
        cursor.execute(
            "SELECT summoner_id, summoner_name, wins, losses, highest_season_tier FROM PLAYER WHERE summoner_id = %s",
            (puuid,)
        )
        result = cursor.fetchone()
        cursor.close()
        conn.close()
        return result
    return None

def get_matches_for_player(puuid, limit=10):
    # Fetch the N most recent matches for a player and parse out their individual stats
    # from the embedded raw_json so the caller gets a clean flat dict per match.
    conn = get_db_connection()
    if conn:
        cursor = conn.cursor(dictionary=True)
        cursor.execute(
            """
            SELECT match_id, game_date, game_length, winning_team, raw_json
            FROM MATCH_DATA
            WHERE JSON_SEARCH(raw_json, 'one', %s, NULL, '$.metadata.participants') IS NOT NULL
            ORDER BY game_date DESC LIMIT %s
            """,
            (puuid, limit)
        )
        rows = cursor.fetchall()
        cursor.close()
        conn.close()
        results = []
        for row in rows:
            player_won = None
            player_stats = {}
            queue_id = None
            try:
                match_json = json.loads(row['raw_json'])
                queue_id = match_json.get('info', {}).get('queueId')
                for p in match_json.get('info', {}).get('participants', []):
                    if p.get('puuid') == puuid:
                        player_won = bool(p.get('win'))
                        cs = p.get('totalMinionsKilled', 0) + p.get('neutralMinionsKilled', 0)
                        player_stats = {
                            'champion':     p.get('championName', ''),
                            'position':     p.get('teamPosition', ''),  # blank for ARAM/Arena
                            'kills':        p.get('kills', 0),
                            'deaths':       p.get('deaths', 0),
                            'assists':      p.get('assists', 0),
                            'cs':           cs,  # totalMinionsKilled + neutralMinionsKilled
                            'damage':       p.get('totalDamageDealtToChampions', 0),
                            'gold':         p.get('goldEarned', 0),
                            'vision':       p.get('visionScore', 0),
                        }
                        break
            except Exception:
                pass
            results.append({
                'match_id':    row['match_id'],
                'game_date':   row['game_date'],
                'game_length': row['game_length'],
                'winning_team': row['winning_team'],
                'player_won':  player_won,
                'queue_id':    queue_id,
                **player_stats,
            })
        return results
    return []

def get_raw_match_json(match_id):
    conn = get_db_connection()
    if conn:
        cursor = conn.cursor()
        cursor.execute("SELECT raw_json FROM MATCH_DATA WHERE match_id = %s", (match_id,))
        row = cursor.fetchone()
        cursor.close()
        conn.close()
        return row[0] if row else None
    return None

def get_recent_matches(limit=20):
    conn = get_db_connection()
    if conn:
        cursor = conn.cursor(dictionary=True)
        cursor.execute(
            "SELECT match_id, game_date, game_length, winning_team FROM MATCH_DATA ORDER BY game_date DESC LIMIT %s",
            (limit,)
        )
        results = cursor.fetchall()
        cursor.close()
        conn.close()
        return results
    return []

if __name__ == "__main__":
    initialize_db()
