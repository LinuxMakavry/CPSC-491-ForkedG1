"""
WinRateAI Coach — Evaluation Script
Runs:
  1. 10 standardized coaching questions (human rating evaluation)
  2. Context size comparison: same question at 10 / 30 / 50 game limits
Results saved to: evaluation_results.txt
"""

import os, sys
sys.path.insert(0, '.')
from dotenv import load_dotenv; load_dotenv()
from database_setup.db_manager import get_matches_for_player, get_player_stats
from openai import OpenAI

PUUID = 'ku2nmJCByoPYnwXXJHZQD7SGZ_ikkyg5YLtEBtIpuV3wQHO4hmMh9dldDXRICXmV1pD9yn0eyXE0fg'
client = OpenAI(api_key=os.getenv('NRP_LLM_API_KEY'), base_url='https://ellm.nrp-nautilus.io/v1')

EVAL_QUESTIONS = [
    'Why do I keep losing games?',
    'Which champion should I focus on?',
    'What is my biggest weakness right now?',
    'How is my recent form trending?',
    'How does my KDA affect my win rate?',
    'Which role do I perform best in?',
    'What should I improve about my vision score?',
    'Am I better in short or long games?',
    'What are my strongest and weakest champions?',
    'Give me a summary of my overall performance.',
]

COMPARISON_QUESTION = 'What is my biggest weakness right now?'


def build_context(puuid, limit):
    stats = get_player_stats(puuid) or {}
    matches = get_matches_for_player(puuid, limit=limit)
    valid = [m for m in matches if m.get('kills') is not None]
    if not valid:
        return 'No match data available.', 0
    name = stats.get('summoner_name', 'Unknown')
    wins = stats.get('wins', 0) or 0
    losses = stats.get('losses', 0) or 0
    total = wins + losses
    wr = round(wins / total * 100) if total else 0
    n = len(valid)
    avg_k   = round(sum(m['kills']   for m in valid) / n, 1)
    avg_d   = round(sum(m['deaths']  for m in valid) / n, 1)
    avg_a   = round(sum(m['assists'] for m in valid) / n, 1)
    avg_cs  = round(sum(m['cs']      for m in valid) / n, 1)
    avg_vis = round(sum(m['vision']  for m in valid) / n, 1)
    avg_dmg = round(sum(m['damage']  for m in valid) / n)
    avg_gld = round(sum(m['gold']    for m in valid) / n)

    champ_map = {}
    for m in valid:
        c = m.get('champion') or 'Unknown'
        if c not in champ_map:
            champ_map[c] = {'games': 0, 'wins': 0, 'kills': 0, 'deaths': 0, 'assists': 0, 'cs': 0}
        d = champ_map[c]
        d['games'] += 1
        if m.get('player_won'):
            d['wins'] += 1
        d['kills']   += m['kills']
        d['deaths']  += m['deaths']
        d['assists'] += m['assists']
        d['cs']      += m['cs']

    champ_list = sorted(champ_map.items(), key=lambda x: x[1]['games'], reverse=True)

    role_map = {}
    for m in valid:
        r = m.get('position') or 'UNKNOWN'
        if r not in role_map:
            role_map[r] = {'games': 0, 'wins': 0}
        role_map[r]['games'] += 1
        if m.get('player_won'):
            role_map[r]['wins'] += 1

    last5 = valid[:5]
    prior = valid[5:]
    last5_wr = round(sum(1 for m in last5 if m.get('player_won')) / len(last5) * 100) if last5 else None
    prior_wr = round(sum(1 for m in prior if m.get('player_won')) / len(prior) * 100) if prior else None

    lines = [
        f'=== PLAYER: {name} (context: {n} games) ===',
        f'Record: {wins}W/{losses}L ({wr}% WR)',
        f'Avg KDA: {avg_k}/{avg_d}/{avg_a} | CS: {avg_cs} | Vision: {avg_vis} | Dmg: {avg_dmg:,} | Gold: {avg_gld:,}',
        '', '=== CHAMPIONS ===',
    ]
    for champ, d in champ_list[:10]:
        g = d['games']
        cwr = round(d['wins'] / g * 100) if g else 0
        ck  = round(d['kills']   / g, 1)
        cd  = round(d['deaths']  / g, 1)
        ca  = round(d['assists'] / g, 1)
        ccs = round(d['cs']      / g, 1)
        lines.append(f'  {champ}: {g}g {cwr}%WR {ck}/{cd}/{ca} KDA {ccs} CS')

    lines += ['', '=== ROLES ===']
    for role, d in sorted(role_map.items(), key=lambda x: x[1]['games'], reverse=True):
        g = d['games']
        rwr = round(d['wins'] / g * 100) if g else 0
        lines.append(f'  {role}: {g}g {rwr}%WR')

    if last5_wr is not None:
        trend = ''
        if prior_wr is not None:
            trend = ' (improving)' if last5_wr > prior_wr else ' (declining)' if last5_wr < prior_wr else ' (stable)'
        lines += ['', '=== RECENT FORM ===', f'  Last 5 games: {last5_wr}%WR{trend}']
        if prior_wr is not None:
            lines.append(f'  Prior games: {prior_wr}%WR')

    return '\n'.join(lines), n


def ask(system_prompt, question):
    resp = client.chat.completions.create(
        model='gpt-oss',
        messages=[
            {'role': 'system', 'content': system_prompt},
            {'role': 'user',   'content': question},
        ],
        max_tokens=1000,
        temperature=0.7,
    )
    return resp.choices[0].message.content


output = []

# ── PART 1: 10-question evaluation ────────────────────────────────────────────
output.append('=' * 70)
output.append('PART 1 — 10-QUESTION EVALUATION (30-game context)')
output.append('Player: Dedgurs')
output.append('=' * 70)
output.append('')

ctx, actual = build_context(PUUID, 30)
sys_prompt = (
    "You are WinRateAI Coach, a League of Legends coaching assistant.\n"
    "Always reference the player's actual data. Be concise and actionable.\n\n"
    + ctx
)

history = []
for i, q in enumerate(EVAL_QUESTIONS, 1):
    messages = [{'role': 'system', 'content': sys_prompt}] + history + [{'role': 'user', 'content': q}]
    resp = client.chat.completions.create(
        model='gpt-oss',
        messages=messages,
        max_tokens=1000,
        temperature=0.7,
    )
    reply = resp.choices[0].message.content
    output.append(f'--- Question {i}: {q}')
    output.append(reply)
    output.append('')
    history.append({'role': 'user',      'content': q})
    history.append({'role': 'assistant', 'content': reply})
    print(f'Q{i} done.')

# ── PART 2: Context size comparison ───────────────────────────────────────────
output.append('')
output.append('=' * 70)
output.append('PART 2 — CONTEXT SIZE COMPARISON')
output.append(f'Question: "{COMPARISON_QUESTION}"')
output.append('=' * 70)
output.append('')

for limit in [10, 30, 50]:
    ctx, actual = build_context(PUUID, limit)
    sys_prompt = (
        "You are WinRateAI Coach, a League of Legends coaching assistant.\n"
        "Always reference the player's actual data. Be concise and actionable.\n\n"
        + ctx
    )
    reply = ask(sys_prompt, COMPARISON_QUESTION)
    output.append(f'--- Context limit: {limit} games (actual stored: {actual}) ---')
    output.append(reply)
    output.append('')
    print(f'Context {limit} done.')

# ── Save to file ───────────────────────────────────────────────────────────────
out_path = os.path.join(os.path.dirname(__file__), 'evaluation_results.txt')
with open(out_path, 'w', encoding='utf-8') as f:
    f.write('\n'.join(output))

print(f'\nSaved to: {out_path}')
