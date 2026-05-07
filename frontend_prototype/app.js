// MVC Prototype for WinRate AI

/* Model */
class AppModel {
  constructor() {
    this.user = null; // logged in user
    this.view = 'login';
    this.puuid = null;
    this.data = {
      stats: { wins: 0, losses: 0, total: 0, winRate: '0%' },
      matches: [],
      predictions: {},
      loading: false,
      error: null,
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
      await fetch('http://localhost:5000/api/matches/fetch', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ puuid: this.puuid, count: 10 }),
      });
      const statsResp = await fetch(
        `http://localhost:5000/api/player/${encodeURIComponent(username)}/${encodeURIComponent(tagLine)}`
      );
      const stats = statsResp.ok ? await statsResp.json() : playerData;
      const wins = stats.wins || 0;
      const losses = stats.losses || 0;
      this.data.stats = { wins, losses, total: wins + losses, winRate: stats.win_rate || '0%' };
      const matchResp = await fetch(`http://localhost:5000/api/matches/${encodeURIComponent(this.puuid)}`);
      if (matchResp.ok) {
        const matchData = await matchResp.json();
        this.data.matches = matchData.matches || [];
      }
      this.data.loading = false;
      this.data.predictions = {};
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
      const resp = await fetch(`http://localhost:5000/api/matches/${encodeURIComponent(this.puuid)}`);
      if (resp.ok) {
        const data = await resp.json();
        this.data.matches = data.matches || [];
      }
    } catch (e) {
      this.data.error = 'Could not load match history.';
    } finally {
      this.data.loading = false;
    }
  }

  async loadPredictions() {
    if (!this.data.matches.length) return;
    const missing = this.data.matches.filter(m => !(m.match_id in this.data.predictions));
    if (!missing.length) return;
    const results = await Promise.all(
      missing.map(async m => {
        try {
          const r = await fetch(`http://localhost:5000/api/predict/match/${m.match_id}`);
          return [m.match_id, r.ok ? await r.json() : null];
        } catch (e) {
          return [m.match_id, null];
        }
      })
    );
    for (const [id, pred] of results) {
      this.data.predictions[id] = pred;
    }
  }

  logout() {
    this.user = null;
    this.puuid = null;
    this.data.matches = [];
    this.data.predictions = {};
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

/* View */
class AppView {
  constructor(model) {
    this.model = model;
    this.app = document.getElementById('app');
  }

  render() {
    const { user, view, data } = this.model;

    const nav = `
      <header class="navbar">
        <div class="brand">WinRate AI</div>
        <nav class="nav-links">
          <a class="nav-link ${view === 'home' ? 'active' : ''}" data-link="home">Dashboard</a>
          <a class="nav-link ${view === 'profile' ? 'active' : ''}" data-link="profile">Profile</a>
          <a class="nav-link ${view === 'champions' ? 'active' : ''}" data-link="champions">Match History</a>
        </nav>
        <div class="auth-buttons">
          ${user ? `<button class="button secondary" data-action="logout">New Search</button>` : ''}
        </div>
      </header>
    `;

    let mainContent;
    if (!user && view === 'login') {
      mainContent = this.loginTemplate();
    } else if (user && view === 'home') {
      mainContent = this.homeTemplate(data, user);
    } else if (user && view === 'profile') {
      mainContent = this.profileTemplate(user);
    } else if (user && view === 'champions') {
      mainContent = this.championsTemplate(data);
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
    return `
      <section class="card">
        <h1 class="heading">${user.name}'s Dashboard</h1>
        <p class="status">Viewing WinRate stats for ${user.name}.</p>
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
    `;
  }

  profileTemplate(user) {
    return `
      <section class="card">
        <h1 class="heading">${user.name}'s Profile</h1>
        <p><strong>Summoner:</strong> ${user.name}</p>
      </section>
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
        const playerTeam = m.player_won ? m.winning_team : (m.winning_team === '100' ? '200' : '100');
        const modelWin = (pred.predicted_winner === 'Team 1' && playerTeam === '100') ||
                         (pred.predicted_winner === 'Team 2' && playerTeam === '200');
        const correct = (modelWin === (m.player_won === true));
        const predClass = modelWin ? 'win' : 'loss';
        const wrongClass = correct ? '' : ' wrong';
        predHtml = `<span class="pred-badge ${predClass}${wrongClass}">AI ${modelWin ? 'WIN' : 'LOSS'}</span>`;
      }

      const role = ROLE_LABEL[m.position || ''] || '';
      const champHtml = m.champion
        ? `<span class="stat-champion">${m.champion}${role ? ` <span class="stat-role">${role}</span>` : ''}</span>`
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
            <span class="match-date">${date}</span>
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
    let accuracyNote = '';
    if (predCount >= data.matches.length && data.matches.length > 0) {
      const correctCount = data.matches.filter(m => {
        const pred = preds[m.match_id];
        if (!pred) return false;
        const playerTeam = m.player_won ? m.winning_team : (m.winning_team === '100' ? '200' : '100');
        const modelWin = (pred.predicted_winner === 'Team 1' && playerTeam === '100') ||
                         (pred.predicted_winner === 'Team 2' && playerTeam === '200');
        return modelWin === (m.player_won === true);
      }).length;
      accuracyNote = `<p class="ai-accuracy">AI correctly predicted ${correctCount} of ${data.matches.length} games in this sample</p>`;
    }

    return `
      <section class="card">
        <h1 class="heading">Match History</h1>
        ${accuracyNote}
        <ul class="match-list">${rows}</ul>
      </section>
    `;
  }

}

/* Controller */
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
    this.view.app.addEventListener('click', async (e) => {
      const link = e.target.closest('[data-link]');
      if (link) {
        const viewName = link.getAttribute('data-link');
        this.model.navigate(viewName);
        this.view.render();
        if (viewName === 'champions' && this.model.puuid) {
          this.model.loadMatches()
            .then(() => this.view.render())
            .then(() => this.model.loadPredictions())
            .then(() => this.view.render());
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

      if (action === 'logout') {
        this.model.logout();
        this.view.render();
      }
    });
  }
}

const appModel = new AppModel();
const appView = new AppView(appModel);
new AppController(appModel, appView);
