# Fantasy Hockey Pickup Bot

Ranks available players by **projected fantasy points for the rest of the current
week**, and tells you who to drop for them.

```bash
pip install -r requirements.txt
streamlit run app.py
```

## What it actually does

Most waiver tools rank on recent points per game. That conflates two different
things — how good a player is, and how much he's playing — and it always reacts
*after* the points show up, by which time everyone else in your league has seen
them too.

This one separates the terms:

```
points = rate per minute  x  minutes he'll get  x  games left  x  matchup
```

Each is estimated from the data that actually informs it:

| Term | Source | Why |
|---|---|---|
| **Rate** | NHL API, per 60 min, split power-play vs rest, shrunk toward a positional prior | PP minutes are worth ~3x even-strength minutes, so they can't share one rate. The prior stops a 4th-liner with two lucky goals topping the list. |
| **Minutes** | Daily Faceoff's current line + PP unit, pulled toward measured trailing TOI | What he'll play next game, not what he averaged last month |
| **Games left** | NHL schedule, Mon–Sun fantasy week | The biggest and most underrated lever in weekly fantasy |
| **Matchup** | Opponent GA/60, shots against, PK%, home/away, back-to-backs | Real but small — the multipliers are deliberately gentle |

### The edge: promotion detection

When Daily Faceoff lists a player on PP1 but his trailing 14-day power-play
share is still low, he was **recently promoted** and his box score hasn't caught
up. The rest of your league is reading his points; this reads his minutes. Those
players get flagged 🔥 on the Pickups tab.

The reverse is flagged too — still listed on PP1 but the share has collapsed
means he's on the way out, so don't pay for the label.

## Does it work?

```bash
python -m tests.backtest
```

Replays the last five weeks and compares against the naive
`points-per-game x games-this-week` ranker:

```
Top-20 actual points : model 33.6 vs baseline 32.0  (+1.5, +4.8%)
Rank correlation     : model 0.581 vs baseline 0.556
```

A real but modest edge — and that's a **floor**, because historical line
listings aren't available, so the backtest runs *without* the promotion signal,
which is the part most likely to add value. Weight sensitivity is flat (every
setting tried beat the baseline), so the result isn't a tuning artifact.

### Scoring a single night

```bash
python -m tests.score_predictions            # last night
python -m tests.score_predictions --date 2026-11-17
```

Grades one slate: projected vs actual for every skater who dressed, bias and
calibration by projection bucket, your roster, the waiver pool, and the names
the report actually put in front of you that morning.

Two things to keep in mind when reading it.

**One night is almost no sample.** A single game is mostly variance. The
population bias and calibration lines are worth something across ~400 skaters;
anything about an individual player is an anecdote.

**A projection is `E[points if he plays] x P(he plays)`.** Everyone in the
sample played, so the availability factor shows up as fake pessimism. The
scorecard splits bias by whether that discount was applied, and the
discount-divided-out row is the honest number.

`scripts/daily_update.py` writes `reports/predictions/<date>.json` on every
run, recording each player's per-game projection *and the line and PP unit he
was listed on*. That last part cannot be recovered later — Daily Faceoff only
publishes today's lines — so without the snapshot, scoring a past day silently
grades a weaker model than the one that gave the advice.

## Setup

### Your roster and the free-agent list

Yahoo closed public Fantasy API access in 2026. Our app key now returns
`403 "This application is not authorized to perform this action."` on every
endpoint, so the league can't be read directly.

That blocks exactly two facts: who's on your team, and who's unowned.
Everything else comes from the NHL and Daily Faceoff and is unaffected.

So paste them into the sidebar (or edit the files directly):

- `data/my_team.txt` — your roster
- `data/free_agents.txt` — your league's available players (optional; leave
  blank to rank every skater not on your roster)

Format is forgiving — one player per line, team and position optional:

```
Matvei Michkov
Cale Makar, COL
Elias Pettersson | VAN | c      # needed: Vancouver has two of them
```

### Configuration

Create a `.env` in the project root:

```bash
YAHOO_CONSUMER_KEY=your_client_id
YAHOO_CONSUMER_SECRET=your_client_secret
YAHOO_LEAGUE_ID=123456          # the numeric id from your league URL
YAHOO_GAME_KEY=                 # optional; auto-detected when blank
```

None of this is needed for the projection model itself — only for the Yahoo
roster/free-agent integration, which is currently gated (see above). The NHL
and Daily Faceoff sources require no credentials at all.

### Restoring Yahoo access

Apply at <https://sports.yahoo.com/developer/access/>. The form explicitly
accepts *"personal or single league use"*. Use your existing Client ID from
`.env`, and pick **Small (< 1,000 users)**.

Once approved, `data/my_team.yahoo_api_available()` starts returning `True` and
the roster/free-agent lists can come from the league automatically. Check with:

```bash
python scripts/verify_auth.py
```

## Daily updates

```bash
python scripts/daily_update.py
```

Refetches everything, rebuilds the model, and writes `reports/YYYY-MM-DD.txt`
plus `reports/latest.txt`. Takes about a minute cold.

Run it late morning. Daily Faceoff posts confirmed lines after the morning
skate, so a 6am run mostly gives you yesterday's listing.

## Tabs

- **Pickups** — ranked candidates, with role-change flags up top
- **My Team** — your roster, most droppable first
- **Moves** — add/drop pairs, capped at your 4 acquisitions/week
- **Player detail** — the full arithmetic behind any projection

### Week points vs keep value

The roster view shows both, deliberately. **Week Pts** is what a player gives
you before Sunday; **Keep/Gm** is what he's worth per game with the schedule
stripped out.

Ranking drops purely on weekly points would tell you to cut a star who happens
to have one game left for a replacement-level skater with four. That's right for
seven days and badly wrong for the season — you can't get the star back, and
next week the schedules invert. So drop ranking blends both, and any suggestion
that cuts a genuinely valuable player is flagged rather than quietly made.

## League scoring

Set in `config.py`, confirmed 2026-11-17:

```
skaters  G 6.0   A 4.0   PPP 1.25   SHP 1.5   SOG 0.9   BLK 1.0
goalies  W 5.0   GA -3.0   SV 0.6    SHO 5.0
```

No plus/minus. Shots and blocks matter more than they look — a four-shot night
is 3.6 points, over half a goal, and volume is far more stable week to week than
goals are. That stability is exactly what you want when buying one week.

## Tuning

Every coefficient is in `models/weights.yaml` with comments. Edit, then re-run
the backtest to check you actually improved things.

## Layout

```
data/      nhl_stats_rest.py  NHL stats API (PP time on ice — the key source)
           dailyfaceoff.py    line combos + PP units, parsed from __NEXT_DATA__
           nhl_client.py      schedule + game logs
           names.py           cross-source player name matching
           my_team.py         roster / free-agent files
           cache.py           sqlite TTL cache
features/  roles.py           merges listed deployment with measured ice time
           matchups.py        schedule + opponent difficulty
           projection.py      the model
           advisor.py         add/drop pairing
           scoring.py         league scoring, one place
tests/     backtest.py        model vs baseline, over whole weeks
           score_predictions.py  one night: projected vs actual
scripts/   daily_update.py    daily refresh + report + prediction snapshot
           verify_auth.py     Yahoo access check
```

## Data sources

Stats from the NHL public API. Lines and power-play units from
[Daily Faceoff](https://www.dailyfaceoff.com) (their `robots.txt` permits
`/teams/*/line-combinations`; we cache 6h and make one request per team).
Fantasy data provided by Yahoo Fantasy.
