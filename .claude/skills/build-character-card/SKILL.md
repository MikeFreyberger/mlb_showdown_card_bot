---
name: build-character-card
description: Interactively build a fictional "character" Showdown card (ex: a Disney character, a kid's chosen persona) based on a real MLB player-year, using mlb_showdown_bot/scripts/build_character_card.py. Use when asked to make/create/design a custom card for a non-MLB character, or to adjust/tweak/iterate on one that's already been started.
---

# Build Character Card

Wraps `mlb_showdown_bot/scripts/build_character_card.py`, which takes a real player's season,
renames/re-images it as a fictional character, and re-fits the chart from any stat overrides you
give it (so points always stay internally consistent with the chart, not hand-typed). Full
mechanism is documented in that script's module docstring — read it once at the start of a
session if you haven't already (`mlb_showdown_bot/scripts/build_character_card.py`).

## 1. Check whether this character already exists

Every successful run saves a JSON sidecar (`<character>.json`, next to the image) recording all
inputs used. Before asking the user anything, check what's already there:

```bash
.venv/bin/python -m mlb_showdown_bot.scripts.build_character_card --list
```

If the character the user is asking about is in that list, **this is an edit, not a new build** —
skip straight to "5. Editing an existing character" below. You don't need to re-ask for the base
player/year/photo; they're already saved. (You can read the sidecar JSON directly,
`mlb_showdown_bot/scripts/output/character_cards/<character>.json`, if you want to see the current
state/overrides before making a change.)

## 2. For a brand-new character, collect the four required inputs

Don't guess any of these — confirm with the user first:

1. **Character name** (what's printed on the card, ex: "Spider-Man") — this is also the key used
   to save/load state, so get it right the first time.
2. **Base player + year** (whose real stats drive the card, ex: "Pedro Martinez, 1997")
3. **Position/role** — for pitchers: starter/reliever/closer; for hitters: their fielding
   position(s). This only needs an explicit `--defense` override if it should differ from the
   base player's real role (see script docstring note on `--defense`); otherwise it's inherited
   automatically and you can skip the flag.
4. **Photo** — a local file path. If the user hasn't given one, ask where it lives (check an
   existing `photos/` folder in the repo root first) rather than running without one.

If any of the four is missing or ambiguous, ask - don't default a character's identity or source
player on your own.

## 3. Generate the first version

Run with no stat overrides so the first look is the base player's real season, just re-skinned.
Use the repo's `.venv` (see Environment note below) rather than a bare `python`:

```bash
.venv/bin/python -m mlb_showdown_bot.scripts.build_character_card \
  --base-name "<base player>" --base-year <year> \
  --character-name "<character name>" \
  --image <photo path>
```

(Set defaults to `2002`, and the team logo / bot logo / year badge are already hidden by default
- no flags needed for that.)

The script prints the full breakdown to stdout (points, chart, projected-vs-real stats table) -
**read it, don't just glance at the image.** Then view the saved image with the Read tool (path is
printed at the end, and also always `mlb_showdown_bot/scripts/output/character_cards/<character
name>.jpg` unless `--output-filename` was overridden) and show it in your response.

If the points total is pushing past ~500 (most Showdown cards land in the 300-450 range), mention
it once in a short clause, not a repeated mini-lecture - ex: "710 PTS, which is elite/broken
territory if you want to dial it back." Don't restate the 300-450 benchmark or suggest specific
nerfs unless asked; the user is iterating deliberately and will ask if they want ideas.

**Above 800 PTS is a real issue, not just "elite"** - flag it every time it happens (not just once
per character), since it usually means a dial got pushed somewhere degenerate rather than just
"very good." Say so plainly and suggest pulling back whichever override pushed it there.

## 4. Iterate from user feedback (same session)

Translate plain-language requests into flags, then re-run with the **same** command plus the new
overrides. Don't reset the overrides you already agreed on between iterations - keep accumulating
them in the command you run (the sidecar also merges `--stat`/`--defense` automatically, so
dropping an old flag from the command doesn't lose it — but keep passing the full set in-session so
what you print and what's saved stay obviously in sync).

| User says | Flag |
|---|---|
| "more/fewer strikeouts" | `--stat SO=<count>` |
| "more/fewer walks" / "better control" | `--stat BB=<count>` and/or `--command <n> --outs <n>` |
| "give up fewer/more home runs" | `--stat HR=<count>` |
| "pitch deeper into games" / "more innings" | `--ip <rating>` |
| "faster on the bases" (hitters) | `--speed <rating>` `[--speed-letter A\|B\|C]` |
| "better/worse defense at X" (hitters) | `--defense "X=<rating>"` |
| "make him a starter/reliever/closer instead" | `--defense "SP=0"` / `"RP=0"` / `"CL=0"` |
| "hit for more power" (hitters) | `--stat HR=<count>`, `--stat 2B=<count>` |
| "give him exactly N doubles/triples/etc on the chart" | `--chart-slots "2B=<N>"` (see below - exact slot count, not a stat guess) |
| general "better/worse" with no number | Ask what dial they mean and roughly how much - don't
  pick a number for them. You can suggest a starting nudge (ex: "~20 more strikeouts") if it helps,
  but confirm before running. |

The raw counting stats (`--stat KEY=VALUE`, repeatable) accept any `NormalizedPlayerStats` field:
`PA, AB, G, GS, IP, H, 1B, 2B, 3B, HR, BB, SO, ER`, etc. These are season totals for the player's
real `PA`/`IP`, not per-game rates - scale them relative to the real stat line already printed in
the `REAL` row of the projected-stats table, not in the abstract.

**If the user wants an exact number of chart results** for a category (ex: "give him exactly 2
doubles and 4 triples on the chart") instead of a vague nudge, use `--chart-slots` instead of
guessing a `--stat` value:

```bash
--chart-slots "2B=2,3B=4"
```

This only works for `SO, BB, 1B, 2B, 3B, HR` (the categories driven by a single raw stat) - PU/GB/FB
are filled as a percentage of total outs and can't be targeted this way. It searches for the
`--stat` value that produces exactly that many die-roll slots and reports what it found (ex:
`2B: 2B=39 -> 2 slots (exact match)`), rather than you trial-and-erroring a raw stat count by hand.
On the first run for a character, it locks the auto-fit command/outs in place before searching (so
the slot budget doesn't shift between tries) - pass `--command`/`--outs` yourself first if you want
a specific control number, otherwise whatever the base player naturally fits to is used and then
held fixed going forward (saved in the sidecar like everything else).

After each run, show the new points total vs. the previous one and the updated image so the user
can see the effect of their change before asking for the next one. Don't re-flag the ~500
elite/broken threshold on every iteration - mention it once per character (first version, or the
first time it crosses ~500), not again on every subsequent tweak. The 800+ threshold is the
exception - flag that one every time (see above).

## 5. Editing an existing character (new session, or returning later)

When the user names a character that already has a saved sidecar (found via `--list` in step 1),
just run the script with `--character-name` plus whatever they want changed — `--base-name`,
`--base-year`, and `--image` are only required the *first* time a character is created, and are
otherwise picked up from the sidecar automatically:

```bash
.venv/bin/python -m mlb_showdown_bot.scripts.build_character_card \
  --character-name "Spider-Man" --stat SO=300 --ip 180
```

This still prints `EDITING existing character` (vs. `CREATING new character`) so you can confirm
it found the right one. Everything from step 4's translation table and image/output behavior
applies the same way here — show the new breakdown and image, and keep going until the user's
happy. Only pass `--base-name`/`--base-year`/`--image` again if the user explicitly wants to rebase
the character onto a different player/year or swap the photo.

## 6. Finishing up

The script already overwrites the same file each run (named after the character unless
`--output-filename` was set), so there's no separate "save" step - once the user says they're
happy, display the final image again with the Read tool (even if it looks the same as the last
iteration you showed) alongside the final stat line/points total, and confirm the file path. If
they want a different save location, pass `--output-folder`.

## Environment note

This repo keeps a dedicated virtualenv at `.venv` (gitignored, not committed) specifically so this
script has somewhere to run without touching the system Python - the OS Python here is
"externally managed" (PEP 668) and refuses direct `pip install`. Always invoke the script via
`.venv/bin/python`, never a bare `python`/`python3`.

If `.venv` doesn't exist yet (fresh clone, or it got cleaned up), create it once:

```bash
python3 -m venv .venv
.venv/bin/pip install -e .
```

Then proceed with `.venv/bin/python -m mlb_showdown_bot.scripts.build_character_card ...` as
normal. If `.venv/bin/python ...` fails with `ModuleNotFoundError` even though `.venv` exists, the
install is stale - re-run the `pip install -e .` line above.
