// MVC Prototype for WinRate AI

/* =====================================================================
   MODEL — owns all application state and communicates with the Flask API
   ===================================================================== */
class AppModel {
  constructor() {
    this.user = null; // logged in user
    this.view = 'login';
    this.puuid = null;   // Riot account-level identifier, used as the DB primary key
    this.data = {
      stats: { wins: 0, losses: 0, total: 0, winRate: '0%' },
      matches: [],
      matchLimit: 10,    // how many matches to request from the API at once
      hasMore: true,     // false when all stored matches have been loaded
      predictions: {},   // keyed by match_id → XGBoost prediction result
      predictionsFailed: new Set(),  // match IDs where prediction will never succeed (404/400)
      loading: false,
      error: null,
      chatHistory: [],   // array of {role, content} objects for the Coach tab
      chatLoading: false,
      chatRatings: {},   // keyed by message index → 'up' | 'down'
    };
  }

  async login(username, tagLine) {
    if (!username || !tagLine) return { success: false, message: 'Both fields are required.' };
    if (username.length < 2 || tagLine.length < 1) return { success: false, message: 'Username and tag must be at least 2 characters long.' };
    this.data.loading = true;
    this.data.error = null;
    const setMsg = msg => {
      const el = document.getElementById('loginMessage');
      if (el) el.innerText = msg;
    };
    try {
      // Step 1: Resolve Riot ID → PUUID and upsert player in the DB
      const resp = await fetch(
        `http://localhost:5000/api/player/${encodeURIComponent(username)}/${encodeURIComponent(tagLine)}`
      );
      if (!resp.ok) {
        const err = await resp.json();
        this.data.loading = false;
        return { success: false, message: err.error || 'Player not found.' };
      }
      const playerData = await resp.json();
      this.puuid = playerData.puuid;
      this.user = { name: playerData.summoner_name };
      setMsg('Fetching match history...');
      // Step 2: Pull latest 10 matches from Riot API and store in MATCH_DATA table
      await fetch('http://localhost:5000/api/matches/fetch', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ puuid: this.puuid, count: 10 }),
      });
      // Step 3: Re-query player stats now that wins/losses have been recomputed from stored matches
      const statsResp = await fetch(
        `http://localhost:5000/api/player/${encodeURIComponent(username)}/${encodeURIComponent(tagLine)}`
      );
      const stats = statsResp.ok ? await statsResp.json() : playerData;
      const wins = stats.wins || 0;
      const losses = stats.losses || 0;
      this.data.stats = { wins, losses, total: wins + losses, winRate: stats.win_rate || '0%' };
      // Step 4: Load the initial match list for display
      const matchResp = await fetch(`http://localhost:5000/api/matches/${encodeURIComponent(this.puuid)}?count=${this.data.matchLimit}`);
      if (matchResp.ok) {
        const matchData = await matchResp.json();
        this.data.matches = matchData.matches || [];
        this.data.hasMore = this.data.matches.length >= this.data.matchLimit;
      }
      this.data.loading = false;
      this.data.predictions = {};
      this.data.matchLimit = 10;
      this.data.hasMore = true;
      this.view = 'home';
      return { success: true, message: `Found ${this.user.name}!` };
    } catch (e) {
      this.data.loading = false;
      return { success: false, message: 'Server unavailable. Is the Flask server running?' };
    }
  }

  async loadMatches() {
    if (!this.puuid || this.data.matches.length > 0) return;
    this.data.loading = true;
    try {
      const resp = await fetch(`http://localhost:5000/api/matches/${encodeURIComponent(this.puuid)}?count=${this.data.matchLimit}`);
      if (resp.ok) {
        const data = await resp.json();
        this.data.matches = data.matches || [];
        this.data.hasMore = this.data.matches.length >= this.data.matchLimit;
      }
    } catch (e) {
      this.data.error = 'Could not load match history.';
    } finally {
      this.data.loading = false;
    }
  }

  async loadMore() {
    if (!this.puuid || this.data.loading) return;
    this.data.loading = true;
    this.data.matchLimit += 10;
    try {
      // First pull more matches from Riot into the DB
      await fetch('http://localhost:5000/api/matches/fetch', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ puuid: this.puuid, count: this.data.matchLimit }),
      });
      // Then re-query the DB for the expanded set
      const resp = await fetch(`http://localhost:5000/api/matches/${encodeURIComponent(this.puuid)}?count=${this.data.matchLimit}`);
      if (resp.ok) {
        const data = await resp.json();
        this.data.matches = data.matches || [];
        this.data.hasMore = this.data.matches.length >= this.data.matchLimit;
      }
    } catch (e) {
      this.data.matchLimit -= 10; // roll back on failure so next attempt requests the right count
    } finally {
      this.data.loading = false;
    }
  }

  async refreshPredictions() {
    // Clear all cached predictions and failed set, then reload
    this.data.predictions = {};
    this.data.predictionsFailed = new Set();
    await this.loadPredictions();
  }

  async loadPredictions() {
    if (!this.data.matches.length) return;
    // Only request predictions for matches we haven't resolved yet
    const missing = this.data.matches.filter(
      m => !(m.match_id in this.data.predictions) && !this.data.predictionsFailed.has(m.match_id)
    );
    if (!missing.length) return;
    // Fire all prediction requests in parallel for speed
    const results = await Promise.all(
      missing.map(async m => {
        try {
          const r = await fetch(`http://localhost:5000/api/predict/match/${m.match_id}`);
          if (r.ok) return [m.match_id, await r.json(), false];
          // 404 = match not in DB for prediction; 400 = unsupported game mode (Arena etc)
          // These won't recover on retry, mark as permanently failed
          if (r.status === 404 || r.status === 400) return [m.match_id, null, true];
          // 503 = model not loaded or server issue — transient, don't cache
          return [m.match_id, null, false];
        } catch (e) {
          return [m.match_id, null, false];
        }
      })
    );
    for (const [id, pred, permanent] of results) {
      if (pred) {
        this.data.predictions[id] = pred;
      } else if (permanent) {
        // Permanently failed matches are stored in a Set to avoid retrying them
        this.data.predictionsFailed.add(id);
      }
      // transient failures (null, !permanent) are not stored — will retry next visit
    }
  }

  async sendChat(question) {
    if (!question || !question.trim() || !this.puuid || this.data.chatLoading) return;
    this.data.chatLoading = true;
    // Snapshot history before appending the new user message so the server
    // receives prior turns only (the current question is sent separately)
    const historyToSend = this.data.chatHistory.map(m => ({ role: m.role, content: m.content }));
    this.data.chatHistory.push({ role: 'user', content: question.trim() });
    try {
      const resp = await fetch('http://localhost:5000/api/chat', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ puuid: this.puuid, question: question.trim(), history: historyToSend }),
      });
      if (resp.ok) {
        const json = await resp.json();
        this.data.chatHistory.push({ role: 'assistant', content: json.reply });
      } else {
        this.data.chatHistory.push({ role: 'assistant', content: 'Sorry, I could not generate a response. Please try again.' });
      }
    } catch (e) {
      this.data.chatHistory.push({ role: 'assistant', content: 'Coach is unavailable. Is the server running?' });
    } finally {
      this.data.chatLoading = false;
    }
  }

  logout() {
    this.user = null;
    this.puuid = null;
    this.data.matches = [];
    this.data.matchLimit = 10;
    this.data.hasMore = true;
    this.data.predictions = {};
    this.data.predictionsFailed = new Set();
    this.data.chatHistory = [];
    this.data.chatLoading = false;
    this.data.chatRatings = {};
    this.view = 'login';
  }

  navigate(viewName) {
    if (!this.user && viewName !== 'login') {
      this.view = 'login';
      return;
    }
    this.view = viewName;
  }
}

/* =====================================================================
   VIEW — pure rendering; no state mutations; rebuilds the entire DOM on
   each render() call using innerHTML for simplicity
   ===================================================================== */
class AppView {
  constructor(model) {
    this.model = model;
    this.app = document.getElementById('app');
  }

  render() {
    const { user, view, data } = this.model;

    const isDark = document.body.classList.contains('dark');
    const nav = `
      <header class="navbar">
        <div class="brand">WinRate AI</div>
        ${user ? `
        <nav class="nav-links">
          <a class="nav-link ${view === 'home' ? 'active' : ''}" data-link="home">Dashboard</a>
          <a class="nav-link ${view === 'champStats' ? 'active' : ''}" data-link="champStats">Champion Stats</a>
          <a class="nav-link ${view === 'champions' ? 'active' : ''}" data-link="champions">Match History</a>
          <a class="nav-link ${view === 'coach' ? 'active' : ''}" data-link="coach">Coach</a>
        </nav>
        <div class="auth-buttons">
          <button class="button secondary" data-action="logout">New Search</button>
        </div>` : ''}
        <button class="theme-toggle" data-action="toggleTheme" title="Toggle dark mode">
          ${isDark
            ? `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="5"/><line x1="12" y1="1" x2="12" y2="3"/><line x1="12" y1="21" x2="12" y2="23"/><line x1="4.22" y1="4.22" x2="5.64" y2="5.64"/><line x1="18.36" y1="18.36" x2="19.78" y2="19.78"/><line x1="1" y1="12" x2="3" y2="12"/><line x1="21" y1="12" x2="23" y2="12"/><line x1="4.22" y1="19.78" x2="5.64" y2="18.36"/><line x1="18.36" y1="5.64" x2="19.78" y2="4.22"/></svg>`
            : `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/></svg>`
          }
        </button>
      </header>
    `;

    let mainContent;
    if (!user && view === 'login') {
      mainContent = this.loginTemplate();
    } else if (user && view === 'home') {
      mainContent = this.homeTemplate(data, user);
    } else if (user && view === 'champStats') {
      mainContent = this.champStatsTemplate(data);
    } else if (user && view === 'champions') {
      mainContent = this.championsTemplate(data);
    } else if (user && view === 'coach') {
      mainContent = this.coachTemplate(data);
    } else {
      mainContent = '<div class="card"><p class="status">This view is not available.</p></div>';
    }

    this.app.innerHTML = `${nav}<main class="main">${mainContent}</main>`;
  }

  loginTemplate() {
    return `
      <section class="card">
        <h1 class="heading">Search for a Player</h1>
        <div class="form-group">
          <label class="label" for="username">Summoner Name</label>
          <input id="username" class="text-input" type="text" placeholder="Enter summoner name" />
        </div>
        <div class="form-group">
          <label class="label" for="password">Tag (e.g. NA1)</label>
          <input id="password" class="text-input" type="text" placeholder="Enter tag line" />
        </div>
        <button class="button" data-action="login">Search</button>
        <p id="loginMessage" class="status"></p>
      </section>
    `;
  }

  homeTemplate(data, user) {
    const total = data.stats.total || (data.stats.wins + data.stats.losses);
    const gamesLabel = total > 0 ? `over ${total} games` : 'No games recorded yet';

    // Compute averages and champion stats from match list
    const matches = data.matches || [];
    let avgKDA = '', avgCS = '', avgLen = '';
    let champRows = '';
    if (matches.length) {
      const n = matches.length;
      const avgK = (matches.reduce((s, m) => s + (m.kills || 0), 0) / n).toFixed(1);
      const avgD = (matches.reduce((s, m) => s + (m.deaths || 0), 0) / n).toFixed(1);
      const avgA = (matches.reduce((s, m) => s + (m.assists || 0), 0) / n).toFixed(1);
      avgKDA = `<span class="kda-k">${avgK}</span> / <span class="kda-d">${avgD}</span> / <span class="kda-a">${avgA}</span>`;
      avgCS  = (matches.reduce((s, m) => s + (m.cs || 0), 0) / n).toFixed(0);
      const avgSecs = matches.reduce((s, m) => s + (m.game_length || 0), 0) / n;
      avgLen = `${Math.floor(avgSecs / 60)}m ${String(Math.round(avgSecs % 60)).padStart(2, '0')}s`;

      // Champion frequency + win rate
      const champMap = {};
      for (const m of matches) {
        if (!m.champion) continue;
        if (!champMap[m.champion]) champMap[m.champion] = { games: 0, wins: 0 };
        champMap[m.champion].games++;
        if (m.player_won) champMap[m.champion].wins++;
      }
      const sorted = Object.entries(champMap).sort((a, b) => b[1].games - a[1].games).slice(0, 5);
      champRows = sorted.map(([champ, s]) => {
        const wr = Math.round((s.wins / s.games) * 100);
        const wrClass = wr >= 50 ? 'champ-wr-good' : 'champ-wr-bad';
        return `<div class="champ-row">
          <span class="champ-name">${champ}</span>
          <span class="champ-games">${s.games}G</span>
          <span class="champ-wr ${wrClass}">${wr}% WR</span>
        </div>`;
      }).join('');
    }

    // ── Insights ────────────────────────────────────────────────────────
    // Each insight requires a minimum sample size (≥2 games per bucket)
    // and a meaningful gap (≥15 pp) before it's shown, to avoid noise.
    const insightItems = [];
    if (matches.length >= 2) {
      // 1. Current streak
      const streakType = matches[0].player_won ? 'W' : 'L';
      let streakCount = 1;
      for (let i = 1; i < matches.length; i++) {
        if (!!matches[i].player_won === !!matches[0].player_won) streakCount++;
        else break;
      }
      if (streakCount >= 2) {
        const sCls = streakType === 'W' ? 'good' : 'bad';
        insightItems.push({ icon: streakType === 'W' ? '🔥' : '❄️',
          label: streakType === 'W' ? 'Win streak' : 'Loss streak',
          value: `${streakCount} in a row`, cls: sCls });
      }

      // 2. Game length tendencies
      const buckets = { short: {w:0,g:0}, mid: {w:0,g:0}, long: {w:0,g:0} };
      for (const m of matches) {
        const mins = (m.game_length || 0) / 60;
        const b = mins < 22 ? 'short' : mins < 35 ? 'mid' : 'long';
        buckets[b].g++; if (m.player_won) buckets[b].w++;
      }
      const bLabels = { short: '<22 min', mid: '22–35 min', long: '35+ min' };
      const bWithData = Object.entries(buckets).filter(([,s]) => s.g >= 2);
      if (bWithData.length >= 2) {
        const bBest  = bWithData.reduce((a, b) => (b[1].w/b[1].g) > (a[1].w/a[1].g) ? b : a);
        const bWorst = bWithData.reduce((a, b) => (b[1].w/b[1].g) < (a[1].w/a[1].g) ? b : a);
        const bBestWR  = Math.round(bBest[1].w  / bBest[1].g  * 100);
        const bWorstWR = Math.round(bWorst[1].w / bWorst[1].g * 100);
        if (bBest[0] !== bWorst[0] && bBestWR - bWorstWR >= 15) {
          insightItems.push({ icon: '⏱️', label: 'Best game length',
            value: `${bLabels[bBest[0]]} — ${bBestWR}% WR`,
            sub: `Struggles in ${bLabels[bWorst[0]]} games (${bWorstWR}% WR)`,
            cls: bBestWR >= 50 ? 'good' : 'bad' });
        }
      }

      // 3. KDA impact
      const kdaHigh = matches.filter(m => !m.deaths || (m.kills + m.assists) / m.deaths >= 3);
      const kdaLow  = matches.filter(m =>  m.deaths  && (m.kills + m.assists) / m.deaths <  3);
      if (kdaHigh.length >= 2 && kdaLow.length >= 2) {
        const highWR  = Math.round(kdaHigh.filter(m => m.player_won).length / kdaHigh.length * 100);
        const lowWR   = Math.round(kdaLow.filter(m  => m.player_won).length / kdaLow.length  * 100);
        const kdaDiff = highWR - lowWR;
        if (Math.abs(kdaDiff) >= 15) {
          insightItems.push({ icon: '⚔️', label: 'KDA impact',
            value: `KDA ≥ 3.0 → ${highWR}% WR`,
            sub: `vs ${lowWR}% when below 3.0`,
            cls: kdaDiff > 0 ? 'good' : 'neutral' });
        }
      }

      // 4. Best role (≥2 games each)
      const iRoleMap = {};
      const ROLE_FULL = { TOP: 'Top', JUNGLE: 'Jungle', MIDDLE: 'Mid', BOTTOM: 'Bot', UTILITY: 'Support' };
      for (const m of matches) {
        if (!m.position) continue;
        if (!iRoleMap[m.position]) iRoleMap[m.position] = {w:0, g:0};
        iRoleMap[m.position].g++; if (m.player_won) iRoleMap[m.position].w++;
      }
      const roleEntries = Object.entries(iRoleMap).filter(([,s]) => s.g >= 2);
      if (roleEntries.length >= 2) {
        const bestRole  = roleEntries.reduce((a, b) => (b[1].w/b[1].g) > (a[1].w/a[1].g) ? b : a);
        const worstRole = roleEntries.reduce((a, b) => (b[1].w/b[1].g) < (a[1].w/a[1].g) ? b : a);
        const brWR = Math.round(bestRole[1].w  / bestRole[1].g  * 100);
        const wrWR = Math.round(worstRole[1].w / worstRole[1].g * 100);
        if (brWR - wrWR >= 15) {
          insightItems.push({ icon: '🏆', label: 'Strongest role',
            value: `${ROLE_FULL[bestRole[0]] || bestRole[0]} (${brWR}% WR)`,
            sub: `Weakest: ${ROLE_FULL[worstRole[0]] || worstRole[0]} (${wrWR}% WR)`,
            cls: brWR >= 50 ? 'good' : 'neutral' });
        }
      }

      // 5. Recent form — last 5 vs prior
      if (matches.length >= 8) {
        const recent   = matches.slice(0, 5);
        const older    = matches.slice(5);
        const recentWR = Math.round(recent.filter(m => m.player_won).length / recent.length * 100);
        const olderWR  = Math.round(older.filter(m  => m.player_won).length / older.length  * 100);
        const formDiff = recentWR - olderWR;
        if (Math.abs(formDiff) >= 15) {
          insightItems.push({ icon: formDiff > 0 ? '📈' : '📉', label: 'Recent form',
            value: `${formDiff > 0 ? 'Improving' : 'Declining'} — ${recentWR}% last 5 games`,
            sub: `vs ${olderWR}% in prior games`,
            cls: formDiff > 0 ? 'good' : 'bad' });
        }
      }
    }
    const insightsHtml = insightItems.length
      ? insightItems.map(it => `
          <div class="insight-row">
            <span class="insight-icon">${it.icon}</span>
            <div class="insight-body">
              <span class="insight-label">${it.label}</span>
              <span class="insight-value ${it.cls}">${it.value}</span>
              ${it.sub ? `<span class="insight-sub">${it.sub}</span>` : ''}
            </div>
          </div>`).join('')
      : '<p class="stat-sub">Load more matches for insights to appear.</p>';

    return `
      <section class="card">
        <h1 class="heading">${user.name}'s Dashboard</h1>
        <p class="status">Summoner: <strong>${user.name}</strong> &nbsp;|&nbsp; Region: NA</p>
      </section>
      <div class="dashboard-grid">
        <div class="card small">
          <h3>Win Rate</h3>
          <p class="stat-big">${data.stats.winRate}</p>
          <p class="stat-sub">${gamesLabel}</p>
        </div>
        <div class="card small">
          <h3>Wins</h3>
          <p class="stat-big stat-wins">${data.stats.wins}</p>
        </div>
        <div class="card small">
          <h3>Losses</h3>
          <p class="stat-big stat-losses">${data.stats.losses}</p>
        </div>
      </div>
      ${matches.length ? `
      <div class="dashboard-grid" style="margin-top:1rem">
        <div class="card small">
          <h3>Avg KDA</h3>
          <p class="stat-kda dash-avg">${avgKDA}</p>
        </div>
        <div class="card small">
          <h3>Avg CS</h3>
          <p class="stat-big dash-avg-plain">${avgCS}</p>
          <p class="stat-sub">per game</p>
        </div>
        <div class="card small">
          <h3>Avg Game Length</h3>
          <p class="stat-big dash-avg-plain">${avgLen}</p>
        </div>
      </div>
      <div class="card" style="margin-top:1rem">
        <h3 style="margin:0 0 0.75rem 0">Most Played Champions</h3>
        <div class="champ-list">${champRows || '<p class="stat-sub">No champion data yet</p>'}</div>
      </div>
      <div class="card" style="margin-top:1rem">
        <h3 style="margin:0 0 0.75rem 0">Insights</h3>
        <div class="insights-list">${insightsHtml}</div>
      </div>` : ''}
    `;
  }

  championsTemplate(data) {
    if (data.loading) {
      return `<section class="card"><p class="status">Fetching latest matches...</p></section>`;
    }
    if (!data.matches.length) {
      return `<section class="card"><h1 class="heading">Match History</h1><p class="status">No matches found.</p></section>`;
    }
    const preds = data.predictions || {};
    const ROLE_LABEL = { TOP: 'Top', JUNGLE: 'Jng', MIDDLE: 'Mid', BOTTOM: 'Bot', UTILITY: 'Sup', '': '' };
    const QUEUE_LABEL = {
      420: 'Ranked Solo', 440: 'Ranked Flex', 400: 'Normal Draft', 430: 'Normal Blind',
      450: 'ARAM', 700: 'Clash', 900: 'URF', 1020: 'One for All',
      1400: 'Ultimate Spellbook', 1900: 'Pick URF', 1700: 'Arena', 1710: 'Arena',
      490: 'Quickplay', 0: 'Custom',
    };
    const rows = data.matches.map(m => {
      const result = m.player_won === true ? 'win' : m.player_won === false ? 'loss' : 'unknown';
      const badge  = result === 'win' ? 'WIN' : result === 'loss' ? 'LOSS' : '—';
      const date   = m.game_date
        ? new Date(m.game_date).toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' })
        : 'Unknown date';
      const mins = Math.floor((m.game_length || 0) / 60);
      const secs = String((m.game_length || 0) % 60).padStart(2, '0');

      let predHtml = '';
      const pred = preds[m.match_id];
      if (pred) {
        // Determine which team the player was on so we can compare to the model's prediction.
        // winning_team is '100' (blue) or '200' (red); player was on the winning team if player_won.
        const playerTeam = m.player_won ? m.winning_team : (m.winning_team === '100' ? '200' : '100');
        const modelWin = (pred.predicted_winner === 'Team 1' && playerTeam === '100') ||
                         (pred.predicted_winner === 'Team 2' && playerTeam === '200');
        const correct = (modelWin === (m.player_won === true));
        const predClass = modelWin ? 'win' : 'loss';
        const wrongClass = correct ? '' : ' wrong';  // adds red outline when model was wrong
        predHtml = `<span class="pred-badge ${predClass}${wrongClass}">AI ${modelWin ? 'WIN' : 'LOSS'}</span>`;
      } else if (data.predictionsFailed && data.predictionsFailed.has(m.match_id)) {
        predHtml = `<span class="pred-badge unavailable">AI N/A</span>`;
      }

      const role = ROLE_LABEL[m.position || ''] || '';
      const queueLabel = m.queue_id != null ? (QUEUE_LABEL[m.queue_id] || `Queue ${m.queue_id}`) : '';
      const iconUrl = m.champion
        ? `https://ddragon.leagueoflegends.com/cdn/14.10.1/img/champion/${m.champion}.png`
        : '';
      const champHtml = m.champion
        ? `<div class="match-champ-block">
            <img class="match-champ-icon" src="${iconUrl}" alt="${m.champion}" onerror="this.style.display='none'">
            <div class="match-champ-info">
              <span class="stat-champion">${m.champion}${role ? ` <span class="stat-role">${role}</span>` : ''}</span>
            </div>
           </div>`
        : '';
      const kdaHtml = (m.kills !== undefined)
        ? `<span class="stat-kda"><span class="kda-k">${m.kills}</span>/<span class="kda-d">${m.deaths}</span>/<span class="kda-a">${m.assists}</span></span>`
        : '';
      const csHtml   = m.cs       !== undefined ? `<span class="stat-item" title="CS">${m.cs} CS</span>` : '';
      const dmgHtml  = m.damage   !== undefined ? `<span class="stat-item" title="Damage">${(m.damage/1000).toFixed(1)}k dmg</span>` : '';
      const goldHtml = m.gold     !== undefined ? `<span class="stat-item" title="Gold">${(m.gold/1000).toFixed(1)}k gold</span>` : '';
      const visHtml  = m.vision   !== undefined ? `<span class="stat-item" title="Vision">${m.vision} vis</span>` : '';

      return `
        <li class="match-row ${result}">
          <div class="match-left">
            <span class="match-badge ${result}">${badge}</span>
            <div class="match-left-info">
              <span class="match-date">${date}</span>
              ${queueLabel ? `<span class="match-queue">${queueLabel}</span>` : ''}
            </div>
            <span class="match-meta">${mins}:${secs}</span>
          </div>
          <div class="match-center">
            ${champHtml}
            ${kdaHtml}
            <div class="match-stats-row">
              ${csHtml}${dmgHtml}${goldHtml}${visHtml}
            </div>
          </div>
          <div class="match-right">
            ${predHtml}
            <span class="match-id">${m.match_id}</span>
          </div>
        </li>`;
    }).join('');

    const predCount = Object.keys(preds).length;
    const failedCount = (data.predictionsFailed || new Set()).size;
    const resolvedCount = predCount + failedCount;
    let accuracyNote = '';
    if (resolvedCount >= data.matches.length && data.matches.length > 0 && predCount > 0) {
      const correctCount = data.matches.filter(m => {
        const pred = preds[m.match_id];
        if (!pred) return false;
        const playerTeam = m.player_won ? m.winning_team : (m.winning_team === '100' ? '200' : '100');
        const modelWin = (pred.predicted_winner === 'Team 1' && playerTeam === '100') ||
                         (pred.predicted_winner === 'Team 2' && playerTeam === '200');
        return modelWin === (m.player_won === true);
      }).length;
      accuracyNote = `<p class="ai-accuracy">AI correctly predicted ${correctCount} of ${predCount} applicable games</p>`;
    }

    return `
      <section class="card">
        <div class="match-history-header">
          <h1 class="heading" style="margin:0">Match History</h1>
          <button class="button secondary" data-action="refreshAI" style="font-size:0.82rem;padding:0.3rem 0.7rem">Refresh AI</button>
        </div>
        ${accuracyNote}
        <ul class="match-list">${rows}</ul>
        ${data.hasMore ? `
        <div class="load-more-wrap">
          <button class="button secondary load-more-btn" data-action="loadMore" ${data.loading ? 'disabled' : ''}>
            ${data.loading ? 'Loading...' : 'Load More'}
          </button>
        </div>` : `<p class="stat-sub load-more-end">All matches loaded</p>`}
      </section>
    `;
  }

  champStatsTemplate(data) {
    const matches = data.matches || [];
    if (!matches.length) {
      return `<section class="card"><h1 class="heading">Champion Stats</h1><p class="status">No match data loaded. Go to Dashboard and search for a player first.</p></section>`;
    }

    // Aggregate per-champion stats
    const champMap = {};
    for (const m of matches) {
      if (!m.champion) continue;
      if (!champMap[m.champion]) {
        champMap[m.champion] = { games: 0, wins: 0, kills: 0, deaths: 0, assists: 0, cs: 0, damage: 0, gold: 0 };
      }
      const c = champMap[m.champion];
      c.games++;
      if (m.player_won) c.wins++;
      c.kills   += m.kills   || 0;
      c.deaths  += m.deaths  || 0;
      c.assists += m.assists || 0;
      c.cs      += m.cs      || 0;
      c.damage  += m.damage  || 0;
      c.gold    += m.gold    || 0;
    }

    const sorted = Object.entries(champMap).sort((a, b) => b[1].games - a[1].games);

    const rows = sorted.map(([champ, s]) => {
      const wr = Math.round((s.wins / s.games) * 100);
      const wrClass = wr >= 50 ? 'cst-wr-good' : 'cst-wr-bad';
      const avgK = (s.kills   / s.games).toFixed(1);
      const avgD = (s.deaths  / s.games).toFixed(1);
      const avgA = (s.assists / s.games).toFixed(1);
      const avgCS  = Math.round(s.cs     / s.games);
      const avgDmg = (s.damage / s.games / 1000).toFixed(1);
      const avgGold = (s.gold  / s.games / 1000).toFixed(1);
      const imgSrc = `https://ddragon.leagueoflegends.com/cdn/14.10.1/img/champion/${champ}.png`;
      return `
        <tr class="cst-row">
          <td class="cst-champ">
            <img class="cst-icon" src="${imgSrc}" alt="${champ}" onerror="this.style.display='none'">
            <span>${champ}</span>
          </td>
          <td class="cst-center">${s.games}</td>
          <td class="cst-center ${wrClass}">${wr}%</td>
          <td class="cst-center">
            <span class="kda-k">${avgK}</span> /
            <span class="kda-d">${avgD}</span> /
            <span class="kda-a">${avgA}</span>
          </td>
          <td class="cst-center">${avgCS}</td>
          <td class="cst-center">${avgDmg}k</td>
          <td class="cst-center">${avgGold}k</td>
        </tr>`;
    }).join('');

    return `
      <section class="card">
        <h1 class="heading">Champion Stats</h1>
        <p class="stat-sub" style="margin-bottom:1rem">Based on last ${matches.length} games</p>
        <div class="cst-table-wrap">
          <table class="cst-table">
            <thead>
              <tr>
                <th class="cst-th">Champion</th>
                <th class="cst-th cst-center">Games</th>
                <th class="cst-th cst-center">Win Rate</th>
                <th class="cst-th cst-center">Avg KDA</th>
                <th class="cst-th cst-center">Avg CS</th>
                <th class="cst-th cst-center">Avg Dmg</th>
                <th class="cst-th cst-center">Avg Gold</th>
              </tr>
            </thead>
            <tbody>${rows}</tbody>
          </table>
        </div>
      </section>
    `;
  }

  coachTemplate(data) {
    const SUGGESTED = [
      'Why do I keep losing games?',
      'Which champion should I focus on?',
      'What is my biggest weakness right now?',
      'How is my recent form trending?',
    ];

    const renderMsg = (text) => {
      // Escape HTML first to prevent XSS from LLM-generated content
      const escaped = text
        .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
      // Then convert **bold** markdown syntax to <strong> tags
      return escaped.replace(/\*\*(.*?)\*\*/g, '<strong>$1</strong>');
    };

    const messages = data.chatHistory.map((msg, i) => {
      if (msg.role === 'user') {
        return `<div class="coach-msg coach-msg-user"><p>${renderMsg(msg.content)}</p></div>`;
      }
      const rating = data.chatRatings[i];
      const upCls   = rating === 'up'   ? ' rated' : '';
      const downCls = rating === 'down' ? ' rated' : '';
      return `
        <div class="coach-msg coach-msg-assistant">
          <div class="coach-msg-label">AI Coach</div>
          <p>${renderMsg(msg.content)}</p>
          <div class="coach-rating-row">
            <button class="coach-rate-btn${upCls}" data-action="rateChat" data-index="${i}" data-rating="up" title="Helpful">&#128077;</button>
            <button class="coach-rate-btn${downCls}" data-action="rateChat" data-index="${i}" data-rating="down" title="Not helpful">&#128078;</button>
          </div>
        </div>`;
    }).join('');

    const ratedCount   = Object.keys(data.chatRatings).length;
    const helpfulCount = Object.values(data.chatRatings).filter(r => r === 'up').length;
    const evalNote = ratedCount > 0
      ? `<p class="coach-eval-note">${helpfulCount} of ${ratedCount} responses rated helpful</p>`
      : '';

    return `
      <section class="card coach-card">
        <div class="coach-header">
          <h1 class="heading" style="margin:0">AI Coach</h1>
          ${evalNote}
        </div>
        <p class="stat-sub coach-intro">Ask about your performance to get personalized coaching advice.</p>
        ${!data.chatHistory.length ? `
        <div class="coach-suggestions">
          ${SUGGESTED.map(q => `<button class="coach-suggest-btn" data-action="suggestChat" data-q="${q}">${q}</button>`).join('')}
        </div>` : ''}
        <div class="coach-messages" id="coachMessages">
          ${messages}
          ${data.chatLoading ? '<div class="coach-typing"><span></span><span></span><span></span></div>' : ''}
        </div>
        <div class="coach-input-row">
          <input id="coachInput" class="text-input coach-input" type="text"
            placeholder="Ask your coach..." ${data.chatLoading ? 'disabled' : ''} />
          <button class="button coach-send-btn" data-action="sendChat" ${data.chatLoading ? 'disabled' : ''}>
            Send
          </button>
        </div>
      </section>
    `;
  }

}

/* =====================================================================
   CONTROLLER — listens to DOM events via event delegation on the root
   #app element, maps data-action / data-link attributes to model calls,
   and re-renders the view after each state change
   ===================================================================== */
class AppController {
  constructor(model, view) {
    this.model = model;
    this.view = view;
    this.init();
  }

  init() {
    this.view.render();
    this.addEventListeners();
  }

  addEventListeners() {
    // Single delegated click handler for the entire app — avoids re-attaching
    // listeners on every render cycle since innerHTML replaces the DOM tree
    this.view.app.addEventListener('click', async (e) => {
      const link = e.target.closest('[data-link]');
      if (link) {
        const viewName = link.getAttribute('data-link');
        this.model.navigate(viewName);
        this.view.render();
        // Lazy-load matches + predictions when the user switches to those tabs
        if (viewName === 'champions' && this.model.puuid) {
          this.model.loadMatches()
            .then(() => this.view.render())
            .then(() => this.model.loadPredictions())
            .then(() => this.view.render());
        }
        if (viewName === 'champStats' && this.model.puuid) {
          this.model.loadMatches().then(() => this.view.render());
        }
        return;
      }

      const action = e.target.getAttribute('data-action');
      if (action === 'login') {
        const username = document.getElementById('username').value.trim();
        const tagLine  = document.getElementById('password').value.trim();
        const messageEl = document.getElementById('loginMessage');
        messageEl.innerText = 'Searching...';
        messageEl.style.color = '#555';
        const result = await this.model.login(username, tagLine);
        messageEl.innerText = result.message;
        messageEl.style.color = result.success ? '#2d7a3a' : '#b02a37';
        if (result.success) this.view.render();
        return;
      }

      if (action === 'toggleTheme') {
        const dark = document.body.classList.toggle('dark');
        localStorage.setItem('theme', dark ? 'dark' : 'light');  // persist across page reloads
        this.view.render();
        return;
      }

      if (action === 'logout') {
        this.model.logout();
        this.view.render();
      }

      if (action === 'loadMore') {
        await this.model.loadMore();
        this.view.render();
        await this.model.loadPredictions();
        this.view.render();
      }

      if (action === 'refreshAI') {
        await this.model.refreshPredictions();
        this.view.render();
        return;
      }

      if (action === 'sendChat') {
        const input = document.getElementById('coachInput');
        if (!input) return;
        const q = input.value.trim();
        if (!q) return;
        input.value = '';
        this.view.render();  // show the user's message immediately before awaiting LLM
        await this.model.sendChat(q);
        this.view.render();
        this._scrollCoach();
        return;
      }

      if (action === 'suggestChat') {
        // Suggested question buttons pre-fill and submit in one click
        const q = e.target.getAttribute('data-q');
        if (!q) return;
        this.view.render();
        await this.model.sendChat(q);
        this.view.render();
        this._scrollCoach();
        return;
      }

      if (action === 'rateChat') {
        // Store the rating keyed by message index; re-render updates button appearance
        const idx    = parseInt(e.target.getAttribute('data-index'), 10);
        const rating = e.target.getAttribute('data-rating');
        this.model.data.chatRatings[idx] = rating;
        this.view.render();
        return;
      }
    });

    // Allow Enter key to submit a chat message without clicking the Send button
    this.view.app.addEventListener('keydown', async (e) => {
      if (e.key === 'Enter' && e.target.id === 'coachInput') {
        const input = e.target;
        const q = input.value.trim();
        if (!q || this.model.data.chatLoading) return;
        input.value = '';
        this.view.render();
        await this.model.sendChat(q);
        this.view.render();
        this._scrollCoach();
      }
    });
  }

  _scrollCoach() {
    // After a new message is rendered, scroll the chat window to the bottom
    const el = document.getElementById('coachMessages');
    if (el) el.scrollTop = el.scrollHeight;
  }
}

const appModel = new AppModel();
const appView = new AppView(appModel);
new AppController(appModel, appView);

// Apply persisted theme before first render so there's no flash of light mode
if (localStorage.getItem('theme') === 'dark') {
  document.body.classList.add('dark');
  appView.render();
}
