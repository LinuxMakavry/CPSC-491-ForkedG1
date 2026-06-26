import os
import requests
import time
from dotenv import load_dotenv
from database_setup.db_manager import save_player, save_match_data, get_raw_match_json, save_timeline_data, get_timeline_for_match

load_dotenv()

class RiotAPIProvider:
    def __init__(self):
        self.api_key = os.getenv("RIOT_API_KEY")
        self.region = "americas"
        self.headers = {"X-Riot-Token": self.api_key}
        
        # Safeguard: Validate API Key on initialization
        if not self.api_key or self.api_key.lower().startswith("your_"):
            raise ValueError("RIOT_API_KEY is missing or invalid. Set a valid Riot API key in .env.")

    def _make_request(self, url):
        # Internal helper to handle rate limiting
        response = requests.get(url, headers=self.headers)

        if response.status_code == 429:
            # Safeguard: Read 'Retry-After' header or default to 10 seconds
            wait_time = int(response.headers.get("Retry-After", 10))
            print(f"Rate limit hit. Waiting {wait_time}s...")
            time.sleep(wait_time)
            return self._make_request(url)

        return response

    def get_puuid(self, game_name, tag_line):
        url = f"https://{self.region}.api.riotgames.com/riot/account/v1/accounts/by-riot-id/{game_name}/{tag_line}"
        response = self._make_request(url)
        if response.status_code == 200:
            puuid = response.json().get('puuid')
            if puuid:
                print(f"PUUID found: {puuid}")
                return puuid
            print("PUUID not found in response JSON.")
            return None

        if response.status_code == 401:
            print("Failed to get PUUID: 401 Unauthorized. Check your Riot API key and permissions.")
        elif response.status_code == 404:
            print("Failed to get PUUID: 404 Not Found. Verify summoner name/tag and region.")
        else:
            print(f"Failed to get PUUID. Status: {response.status_code}, Body: {response.text}")
        return None

    def get_match_ids(self, puuid, count=5):
        url = f"https://{self.region}.api.riotgames.com/lol/match/v5/matches/by-puuid/{puuid}/ids?count={count}"
        response = self._make_request(url)
        if response.status_code == 200:
            return response.json()
        print(f"Failed to get match IDs. Status: {response.status_code}, Body: {response.text}")
        return []

    def get_match(self, match_id):
        url = f"https://{self.region}.api.riotgames.com/lol/match/v5/matches/{match_id}"
        response = self._make_request(url)
        if response.status_code == 200:
            return response.json()
        return None

    def get_timeline(self, match_id):
        """Fetch per-minute timeline data for a match. Used for 15-min feature extraction."""
        url = f"https://{self.region}.api.riotgames.com/lol/match/v5/matches/{match_id}/timeline"
        response = self._make_request(url)
        if response.status_code == 200:
            return response.json()
        return None

    def fetch_and_store_matches(self, puuid, count=5):
        match_ids = self.get_match_ids(puuid, count=count)
        for match_id in match_ids:
            # Fetch and store match JSON if not already in DB
            if get_raw_match_json(match_id) is None:
                match_json = self.get_match(match_id)
                if match_json:
                    save_match_data(match_json)
            # Fetch and store timeline JSON if not already in DB
            if get_timeline_for_match(match_id) is None:
                timeline_json = self.get_timeline(match_id)
                if timeline_json:
                    save_timeline_data(match_id, timeline_json)

    def backfill_timelines(self, limit=100):
        """Fetch timeline data for stored matches that are missing it.

        Useful for populating TIMELINE_DATA for matches collected before timeline
        fetching was added to the normal flow. Respects rate limiting via _make_request.

        Args:
            limit: Max number of matches to backfill in one run.

        Returns:
            Dict with counts of fetched, skipped (already stored), and failed matches.
        """
        from database_setup.db_manager import get_recent_matches
        recent = get_recent_matches(limit=limit)
        fetched = skipped = failed = 0
        for row in recent:
            match_id = row["match_id"]
            if get_timeline_for_match(match_id) is not None:
                skipped += 1
                continue
            timeline_json = self.get_timeline(match_id)
            if timeline_json:
                save_timeline_data(match_id, timeline_json)
                fetched += 1
            else:
                failed += 1
        print(f"Backfill complete: {fetched} fetched, {skipped} already stored, {failed} failed")
        return {"fetched": fetched, "skipped": skipped, "failed": failed}

if __name__ == "__main__":
    try:
        provider = RiotAPIProvider()
        game_name = "Dedgurs"
        tag_line = "MEOW"
        
        puuid = provider.get_puuid(game_name, tag_line)
        
        if puuid:
            print(f"PUUID: {puuid}")
            # Link API retrieval to Database storage
            save_player(puuid, game_name) 
            
            matches = provider.get_match_ids(puuid)
            print(f"Matches: {matches}")
            provider.fetch_and_store_matches(puuid, count=5)
    except Exception as e:
        print(f"System Error: {e}")
