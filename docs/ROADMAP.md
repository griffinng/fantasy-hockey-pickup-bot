# Roadmap

Ideas not yet built, roughly in the order they'd pay off. The guiding
principle stays the same throughout: **find value before the box score shows
it.** Every item below is a different way of being early.

---

## 1. Injury-return radar

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

## 2. Treat the IR slot as a free roster spot

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

**Depends on #1**, since the value of a stash is entirely the value of the
returning player.

---

## 3. Next-week lookahead

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
  acquisition-budget model from #2.

**Design note.** This should be a *blend*, weighted by how much of the current
week is left, not a hard switch. On Monday next week is nearly irrelevant; by
Saturday it dominates. Something like
`weight_next_week = 1 - (days_remaining / 7)` is the obvious starting point,
and the backtest can tune the curve.

---

## 4. Smaller things worth doing

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
