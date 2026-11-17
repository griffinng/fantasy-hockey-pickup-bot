# Roadmap

Ideas not yet built, roughly in the order they'd pay off. The guiding
principle stays the same throughout: **find value before the box score shows
it.** Every item below is a different way of being early.

---

## 1. Daily lineup solver — and marginal pickup value

Two features, one piece of machinery. They belong together because they are
the same calculation asked in two directions.

### The machinery

A **daily lineup solver**: given a roster, the slot structure (currently 2C,
2LW, 2RW, 4D, 2G), and per-day projections, find the best legal lineup for
each day and sum the week. `PlayerProjection.games` already holds expected
points per individual game, so the inputs exist.

### Use one: is my lineup right today?

Run the solver on the current roster, compare against what is actually set,
and flag the gaps. The valuable catch is not a close call between two
projections — it is **a starter whose team is idle while a bench player's is
not**, which is a guaranteed zero against a real number.

### Use two: what is a pickup actually worth?

This is the part the current rankings get wrong, and it is not a small error.

**Week Pts is an upper bound.** It assumes a player appears in every game his
team plays and that you start him every time. It knows nothing about your
roster's shape. If you already have six forwards playing on Thursday into six
forward slots, a seventh forward who plays Thursday is worth **zero** to you
that day, however good he is.

The honest number is marginal: *(best week I can field with him) minus (best
week I can field without him)*. Same solver, run twice.

**A worked example from the real roster, week of 17 Nov 2026.** Daily skater
coverage against 2C/2LW/2RW/4D:

    day        C  LW  RW   D    NHL games    state
    Wed 19     0   0   0   2        3        8 of 10 slots empty
    Thu 20     1   3   4   4       12        3 players benched
    Fri 21     0   0   0   0        0        nothing to fix
    Sat 22     1   2   4   5       14        3 players benched

Against that shape, ranking by Week Pts points at the wrong players:

    player              week pts   rank    pts on Wed 19
    Frank Nazar            26.6      #1        0.0
    Zach Benson            23.9      #2        0.0
    Sam Rinzel             23.8      #3        0.0
    Mason McTavish         23.5      #4        0.0
    Patrick Kane           22.1      #5        0.0
    ...
    Elias Lindholm         18.6     #14        5.7
    Zachary Bolduc         14.5     #42        5.2

Every one of the top five contributes **nothing** on the day with open slots,
while the players who would actually fill it sit at #14 and #42. Meanwhile
Thursday and Saturday additions mostly displace nobody, because those slots
are already full and three owned players are being benched as it is.

### The nuance that stops this overcorrecting

Scarcity has to be measured against the **slate**, not just the roster. Friday
21 Nov shows ten empty slots, which looks like a gaping hole and is in fact
nothing at all: there are no NHL games that day. A naive "fill your empty
days" rule would chase an opportunity that does not exist. The solver handles
this correctly by construction — it can only ever assign players who actually
play — but any heuristic shortcut around it would not.

### What is still missing

- **Yahoo position eligibility.** Yahoo grants multi-position eligibility
  (`C,RW`); the NHL data has one position per player. Without it we cannot know
  a bench winger may legally fill the open centre slot. It is on the roster
  page beside each name, so this is parsing, not modelling.
- **The solver itself.** With eligibility known it is a small assignment
  problem — about 14 skaters across 10 slots per day. Brute force over
  sensible assignments is fine; a transparent answer beats an optimal-but-
  opaque one.
- **Replacing Week Pts in the rankings** with marginal value, or showing both.
  Both is probably right: Week Pts answers "how good is he", marginal value
  answers "how much does he help *me*", and they are different questions.

### Output shape

Actions, not numbers, with the free wins separated from the judgement calls:

```
START  Brock Boeser (VAN plays TOR tonight)
SIT    Erik Karlsson (PIT idle)              +6.4 pts, free
ADD    Elias Lindholm — fills your empty Wed slot   +5.7 marginal
       (Nazar projects 26.6 but adds 0.0 on Wed; you are already full
        Thu and Sat)
```

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
