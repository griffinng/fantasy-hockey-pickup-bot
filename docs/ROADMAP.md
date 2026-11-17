# Roadmap

Ideas not yet built, roughly in the order they'd pay off. The guiding
principle stays the same throughout: **find value before the box score shows
it.** Every item below is a different way of being early.

---

## 1. Daily lineup check

**The idea.** Every morning, compare the lineup as it actually stands against
what the projections say it should be, and flag anything worth changing before
puck drop.

**Why this is first.** Everything else on this list fires occasionally; this
fires *every day*. And the mistake it catches is the cheapest kind to fix and
the most annoying to discover afterwards: **a starter whose team isn't playing
tonight while a bench player's is.** That is not a close call between two
projections, it is a guaranteed zero against a real number, and over a season
it quietly costs more than any single waiver pickup gains. In a daily-lineup
league, games started is the single biggest lever you control.

**Most of the work is already done.** The pieces exist:

- The Yahoo roster page carries the slot for every player — `C`, `LW`, `RW`,
  `D`, `G`, `BN`, `IR+` — in the same table we already read for the roster, so
  who is starting is scrapeable without anything new.
- Per-game projections already exist: `PlayerProjection.games` holds a
  `GameProjection` per remaining game with its own expected points, and
  `matchups.Game.state` now tells us whether a game is still to come.
- `remaining_games()` already excludes finished games, which is exactly what a
  "is he playing tonight" check needs.

**What's missing.**

- **Yahoo position eligibility**, which is not the same as NHL position. Yahoo
  grants multi-position eligibility (a player listed `C,RW`) and the NHL data
  has only one position per player. Without it we cannot know a bench winger
  can legally fill the empty centre slot. The roster page shows each player's
  eligibility next to his name, so this is a parsing job rather than a
  modelling one.
- **A slot-filling solver.** With eligibility known it is a small assignment
  problem: maximise total projected points across the day's slots. The roster
  is ~14 skaters and a handful of slots, so brute force over sensible
  assignments is fine — no need for anything clever, and a transparent result
  is worth more than an optimal-but-opaque one.

**Output shape.** Not a list of numbers but a short list of actions, ranked by
how much they gain, with the ones that are free at the top:

```
START  Brock Boeser (VAN plays TOR tonight)
SIT    Erik Karlsson (PIT idle)            +6.4 pts, free
START  Gabriel Vilardi over Victor Olofsson in the RW slot
                                            +1.2 pts, judgement call
```

The distinction matters: the first is arithmetic, the second is a projection
difference small enough that you may disagree. The bot should separate them
rather than presenting both with the same confidence.

**Timing.** The daily job already runs at 11:00 for the Daily Faceoff lines,
which is the right time for this too — after morning skate, before most puck
drops. The existing `scripts/daily_update.py` is the natural home.

---

## 2. Injury-return radar

**The idea.** Track players currently out who are approaching a return. A
genuine first-line talent coming back in a week is often sitting unowned,
because everyone drops injured players and nobody watches for the return. By
the time he plays two games and scores, he's gone.

**Why it needs separate handling.** The model is blind to these players by
construction. It projects from trailing ice time, and a player who hasn't
played has none. Worse, a star who got hurt in October may have *zero* points
this season, so every points-based ranking in the league buries him. The
signal has to come from outside the stat line entirely.

**Shape of the work.**
- Daily Faceoff already gives us `injuryStatus` per player and an `ir` group,
  so we know *who* is out at no extra cost. We are currently throwing this
  away for anyone not in a line slot.
- What's missing is *when he returns* and *how good he was*. Return timing
  lives in news text, so this needs a news source — Daily Faceoff player news
  (`latestNews.details` is already in the payload we parse, and often reads
  "expected to play Wednesday"), or the NHL's own injury feed.
- "How good was he" should come from **prior-season** per-60 rates and his
  role when healthy, not this season's counting stats.
- Output: a separate "Coming back" panel ranked by *talent*, not by projected
  points for this week, with an estimated return date and a confidence level.

**Honest caveat.** Return dates reported in news are routinely wrong, and
"week to week" means nothing. This panel should surface candidates for you to
judge, and must never feed an automatic add suggestion.

---

## 3. Treat the IR slot as a free roster spot

**The idea.** An IR/IR+ slot holds a player without costing active roster
space. If a stashable injured player is worth more than the marginal
free agent, parking him there is a free option on his return.

**The constraint that makes it non-trivial.** It is only free if you aren't
add-constrained. Each stash costs an acquisition, and you get 4 a week. So the
rule is roughly:

```
stash worthwhile  if  adds_remaining > adds_needed_for_this_week's lineup
                 and  player is IR-eligible (Yahoo status O/IR, not DTD)
                 and  his healthy talent clears the best available FA
```

Already half-built: `RosterEntry.on_ir` exists and IR players are excluded
from drop suggestions. What's missing is the *acquisition-budget* reasoning —
the bot needs to know how many adds this week's lineup actually requires
before it can call the rest spare.

**Depends on #2**, since the value of a stash is entirely the value of the
returning player.

---

## 4. Next-week lookahead

**The idea.** Late in a week, adds should be evaluated against *next* week too,
not just the remaining days. Two refinements:

- **Tiebreak on next week's schedule.** When two candidates project within
  noise of each other this week, prefer the one whose team plays more games
  next week, or faces softer opponents. Nearly free to implement: the schedule
  and opponent profiles are already loaded, and `matchups.team_games()` takes
  arbitrary date bounds.
- **Spend leftover adds early.** If it's Saturday, you have adds remaining, and
  they don't roll over, a player who does nothing this week but has four games
  next week is worth more than an unused acquisition. Needs the
  acquisition-budget model from #3.

**Design note.** This should be a *blend*, weighted by how much of the current
week is left, not a hard switch. On Monday next week is nearly irrelevant; by
Saturday it dominates. Something like
`weight_next_week = 1 - (days_remaining / 7)` is the obvious starting point,
and the backtest can tune the curve.

---

## 5. Smaller things worth doing

- **Goalie model refinements.** Confirmed starters from Daily Faceoff's
  starting-goalies page would beat inferring from the depth chart. Also:
  opponent-specific shot volume rather than the team-level average currently
  used.
- **Backtest the promotion signal.** The headline 4.8% edge excludes it,
  because historical line listings aren't available. Now that we fetch Daily
  Faceoff daily, snapshotting each day's lines builds the history needed to
  measure it properly in a few weeks.
- **Cache `bust(namespace)` is broken.** Keys are sha256 hashes, so the
  `LIKE %namespace%` filter never matches. Only the full `bust()` works today.
- **Yahoo API.** If the access application is approved, roster and free-agent
  lists come from the league automatically and the manual files become a
  fallback. `data/my_team.yahoo_api_available()` already detects this.
