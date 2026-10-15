"""
CLI for building fictional "character" Showdown cards (ex: Disney characters) based on a real
MLB player-year's stats, with manual dial overrides to push the card better/worse.

How it works:
  1. Generate a normal card for the real base player/year (reuses the existing scrape + fit
     pipeline, so the chart/points start out fully realistic).
  2. Swap in the character's name/image and re-run the SAME fitting pipeline with any requested
     stat overrides (ex: more strikeouts, fewer walks) via `ShowdownPlayerCard.rebuilt_from_card_data`.
     This keeps the chart (and therefore the points) internally consistent instead of hand-editing
     chart slots directly.
  3. Independently, defense/speed/innings-pitched dials are NOT part of the chart fit, so they're
     applied as a direct field override + `recalculate_points()` afterward.

Note on --defense: the base player's real position(s)/role carry over automatically (ex: a real
starter already comes out as STARTER). Only pass --defense when you want to change the role/rating
itself (ex: basing a character on a reliever but making them a starter, or bumping a fielding
rating).

EDIT MODE: every successful run writes a small JSON sidecar (`<character>.json`, next to the
image) recording every input used. Re-running with just `--character-name` (and no `--base-name`/
`--base-year`/`--image`) loads that sidecar and treats it as the starting point - any flags you DO
pass this time are layered on top of it:
  - `--stat` / `--defense` MERGE into the saved dict (new keys override old ones, old keys you
    don't mention are kept).
  - Everything else (`--command`/`--outs`, `--speed`, `--ip`, `--team-override`, `--image`, the
    `--*-logo`/`--*-year` flags, `--base-name`/`--base-year`/`--set`) OVERRIDES the saved value if
    passed, otherwise the saved value is kept.
Use `--list` to see which characters already have a saved sidecar.

Examples:
  # Spider-Man, starting pitcher, based on Pedro Martinez's 1997 season
  python -m mlb_showdown_bot.scripts.build_character_card \\
      --base-name "Pedro Martinez" --base-year 1997 \\
      --character-name "Spider-Man" \\
      --image ~/disney_images/spiderman.png \\
      --command 5 --outs 13 \\
      --stat HR=12 --stat BB=45 \\
      --show

  # Come back later and nudge an existing character - no need to re-pass base player/image
  python -m mlb_showdown_bot.scripts.build_character_card \\
      --character-name "Spider-Man" --stat SO=300 --ip 180
"""
import argparse
import copy
import json
import re
import sys
from pathlib import Path

from ..core.card.card_generation import generate_card
from ..core.card.chart import ChartCategory
from ..core.card.images import ImageParallel
from ..core.card.showdown_player_card import ShowdownPlayerCard
from ..core.shared.player_position import Position
from ..core.shared.speed import Speed, SpeedLetter

DEFAULT_OUTPUT_FOLDER = str(Path(__file__).parent / "output" / "character_cards")
DEFAULT_SET = "2002"

# Chart categories whose slot count is driven by a single raw counting stat (vs. PU/GB/FB, which
# are filled as a percentage of total outs and aren't independently targetable this way).
CHART_SLOT_STAT_KEY = {"SO": "SO", "BB": "BB", "1B": "1B", "2B": "2B", "3B": "3B", "HR": "HR"}
CHART_SLOT_SEARCH_MAX = 300


def slugify(character_name: str, output_filename: str | None) -> str:
    return output_filename or re.sub(r"[^A-Za-z0-9_-]+", "_", character_name).strip("_")


def state_path(output_folder: str, slug: str) -> Path:
    return Path(output_folder) / f"{slug}.json"


def load_state(output_folder: str, slug: str) -> dict | None:
    path = state_path(output_folder, slug)
    if not path.exists():
        return None
    with open(path) as f:
        return json.load(f)


def save_state(output_folder: str, slug: str, state: dict) -> None:
    with open(state_path(output_folder, slug), "w") as f:
        json.dump(state, f, indent=2, sort_keys=True)


def list_characters(output_folder: str) -> list[str]:
    folder = Path(output_folder)
    if not folder.exists():
        return []
    return sorted(p.stem for p in folder.glob("*.json"))


def parse_key_value_list(pairs: list[str] | None) -> dict:
    """Parse a list of 'KEY=VALUE' strings into a dict, casting values to int/float where possible."""
    result = {}
    for pair in pairs or []:
        if "=" not in pair:
            raise ValueError(f"Expected KEY=VALUE, got '{pair}'")
        key, raw_value = pair.split("=", 1)
        key = key.strip()
        raw_value = raw_value.strip()
        try:
            value = int(raw_value)
        except ValueError:
            try:
                value = float(raw_value)
            except ValueError:
                value = raw_value
        result[key] = value
    return result


def position_from_str(pos_str: str) -> Position:
    pos_str = pos_str.strip().upper()
    try:
        return Position[pos_str]  # EX: 'SP' -> Position.SP (value "STARTER")
    except KeyError:
        return Position(pos_str)  # EX: '1B' -> Position._1B (value "1B")


def parse_defense_overrides(raw: str | None) -> dict[str, int]:
    """Parse 'POS=RATING,POS2=RATING2' into {Position.name: int} (ex: {'SP': 0, 'SS': 15})."""
    if not raw:
        return {}
    overrides = {}
    for pair in raw.split(","):
        pos_str, rating_str = pair.split("=", 1)
        overrides[position_from_str(pos_str).name] = int(rating_str.strip())
    return overrides


def parallel_from_str(raw: str) -> str:
    """Accept either the enum name (ex: 'COMIC_BOOK_HERO') or short code (ex: 'CB'); returns the
    canonical short-code value ShowdownImage expects."""
    raw = raw.strip().upper().replace(" ", "_").replace("-", "_")
    try:
        return ImageParallel[raw].value
    except KeyError:
        return ImageParallel(raw).value  # raises ValueError if neither matches


def parse_chart_slot_targets(raw: str | None) -> dict[str, int]:
    """Parse 'CAT=COUNT,CAT2=COUNT2' into {category: target slot count} (ex: {'2B': 2, '3B': 4})."""
    if not raw:
        return {}
    targets = {}
    for pair in raw.split(","):
        cat, count = pair.split("=", 1)
        cat = cat.strip().upper()
        if cat not in CHART_SLOT_STAT_KEY:
            raise ValueError(f"--chart-slots only supports {sorted(CHART_SLOT_STAT_KEY)} (PU/GB/FB are filled as a % of total outs, not independently settable)")
        targets[cat] = int(count.strip())
    return targets


def merge_args_into_state(args: argparse.Namespace, prev: dict | None) -> dict:
    """Combine this run's CLI args with any previously saved state for this character.

    `--stat`/`--defense` merge key-by-key (new values win, old keys not mentioned are kept).
    Everything else overrides the saved value if passed this run, otherwise keeps it.
    """
    prev = prev or {}

    base_name = args.base_name or prev.get("base_name")
    base_year = args.base_year or prev.get("base_year")
    image_path = args.image or prev.get("image_path")
    if not base_name or not base_year or not image_path:
        missing = [
            n for n, v in [("--base-name", base_name), ("--base-year", base_year), ("--image", image_path)]
            if not v
        ]
        raise ValueError(
            f"No saved character named '{args.character_name}' found, so {', '.join(missing)} "
            f"{'are' if len(missing) > 1 else 'is'} required the first time."
        )

    set_ = args.set or prev.get("set") or DEFAULT_SET

    stats = dict(prev.get("stats") or {})
    stats.update(parse_key_value_list(args.stats))

    defense = dict(prev.get("defense") or {})
    defense.update(parse_defense_overrides(args.defense))

    if (args.command is None) != (args.outs is None):
        raise ValueError("--command and --outs must be provided together")
    command = args.command if args.command is not None else prev.get("command")
    outs = args.outs if args.outs is not None else prev.get("outs")

    parallel = parallel_from_str(args.parallel) if args.parallel else prev.get("parallel")

    return {
        "character_name": args.character_name,
        "base_name": base_name,
        "base_year": base_year,
        "set": set_,
        "image_path": image_path,
        "stats": stats,
        "defense": defense,
        "command": command,
        "outs": outs,
        "speed": args.speed if args.speed is not None else prev.get("speed"),
        "speed_letter": args.speed_letter or prev.get("speed_letter"),
        "ip": args.ip if args.ip is not None else prev.get("ip"),
        "team_override": args.team_override or prev.get("team_override"),
        "parallel": parallel,
        "dark_mode": args.dark_mode if args.dark_mode is not None else prev.get("dark_mode", False),
        "hide_team_logo": args.hide_team_logo if args.hide_team_logo is not None else prev.get("hide_team_logo", True),
        "hide_bot_logo": args.hide_bot_logo if args.hide_bot_logo is not None else prev.get("hide_bot_logo", True),
        "hide_set_year_text": args.hide_set_year_text if args.hide_set_year_text is not None else prev.get("hide_set_year_text", True),
        "set_number": args.set_number or prev.get("set_number"),
        "output_filename": args.output_filename or prev.get("output_filename"),
    }


def fetch_baseline_card_data(base_name: str, base_year: str, set_: str) -> dict:
    """One-time network fetch of the real base player's card (stats, stats_period, etc)."""
    baseline = generate_card(
        name=base_name,
        year=base_year,
        set=set_,
        print_to_cli=False,
        show_image=False,
        store_in_logs=False,
    )
    if baseline.get("error"):
        raise RuntimeError(f"Failed to generate base card for {base_name} ({base_year}): {baseline['error_for_user'] or baseline['error']}")
    return baseline["card"]


def construct_card(card_data: dict, state: dict) -> ShowdownPlayerCard:
    """Build the fictional character's ShowdownPlayerCard from already-fetched baseline data + state.
    Pure/no network - safe to call many times in a loop (ex: the --chart-slots search)."""

    # KEEP ONLY THE INPUT FIELDS (stats, stats_period, set, image, overrides, ...), DROP DERIVED ONES
    # (chart, points, speed, ip, defense, ...) SO THEY GET FRESHLY RE-FIT BELOW.
    inputs = {k: v for k, v in card_data.items() if k not in ShowdownPlayerCard.REBUILT_FIELDS}

    # CHARACTER IDENTITY
    character_name = state["character_name"]
    inputs["name"] = character_name
    inputs["stats"]["name"] = character_name

    # STAT DIAL OVERRIDES (anything not recognized is still passed through, in case it's a
    # valid NormalizedPlayerStats field not in CHART_DRIVING_STAT_KEYS, ex: ER, SV, W, L)
    for key, value in state["stats"].items():
        inputs["stats"][key] = value

    # CONTROL / ONBASE DIAL
    if state["command"] is not None and state["outs"] is not None:
        inputs["command_out_override"] = f"{state['command']}-{state['outs']}"

    # IMAGE
    image = dict(inputs.get("image") or {})
    image["source"] = {"path": str(Path(state["image_path"]).expanduser())}
    # DON'T SET output_folder_path YET - THAT WOULD AUTO-RENDER THE IMAGE DURING __init__, BEFORE
    # THE POST-HOC DEFENSE/SPEED/IP OVERRIDES BELOW ARE APPLIED. RENDER ONCE, EXPLICITLY, AT THE END.
    image["hide_team_logo"] = state["hide_team_logo"]
    image["hide_bot_logo"] = state["hide_bot_logo"]
    image["hide_set_year_text"] = state["hide_set_year_text"]
    if state.get("set_number"):
        image["set_number"] = state["set_number"]
    if state.get("parallel"):
        image["parallel"] = state["parallel"]
    image["is_dark_mode"] = state.get("dark_mode", False)
    inputs["image"] = image

    if state["team_override"]:
        inputs["team_override"] = state["team_override"]

    inputs["print_to_cli"] = False
    inputs["show_image"] = False  # show manually below, after any post-hoc overrides

    card = ShowdownPlayerCard(**inputs)

    # DEFENSE / SPEED / IP ARE NOT PART OF THE CHART FIT -> APPLY DIRECTLY + RECALCULATE POINTS
    defense_overrides = {position_from_str(k): v for k, v in state["defense"].items()}
    needs_recalc = False
    if defense_overrides:
        card.positions_and_defense.update(defense_overrides)
        card.positions_and_defense_for_visuals = card.calc_positions_and_defense_for_visuals()
        card.positions_and_defense_string = card.positions_and_defense_as_string(is_horizontal=True)
        needs_recalc = True
    if state["speed"] is not None:
        card.speed = Speed(speed=state["speed"], letter=SpeedLetter(state["speed_letter"] or "B"))
        needs_recalc = True
    if state["ip"] is not None:
        card.ip = state["ip"]
        needs_recalc = True

    if needs_recalc:
        card.recalculate_points()

    return card


def render_card(card: ShowdownPlayerCard, character_name: str, output_filename: str | None, output_folder: str, show: bool) -> None:
    Path(output_folder).mkdir(parents=True, exist_ok=True)
    card.image.output_folder_path = output_folder

    # STABLE FILENAME (NO TIMESTAMP) SO RE-RUNNING WITH NEW OVERRIDES OVERWRITES THE SAME FILE -
    # MAKES AN ITERATE-UNTIL-HAPPY WORKFLOW EASY TO TRACK.
    slug = slugify(character_name, output_filename)
    card.image.output_file_name = f"{slug}.{card.image.file_format.extension}"

    card.generate_card_image(show=show)


def resolve_chart_slot_targets(card_data: dict, state: dict, targets: dict[str, int]) -> dict:
    """For each requested {category: target slot count}, search for the raw stat value that
    produces exactly that many chart slots (reusing the real fit algorithm as a black box, rather
    than hand-laying-out the chart). Returns an updated copy of `state` with the found stat values
    merged in. Resolves one category at a time, in the order given, since earlier choices affect
    how much "room" is left for later ones.
    """
    state = copy.deepcopy(state)

    # LOCK COMMAND/OUTS BEFORE SEARCHING - OTHERWISE EACH TRIAL RE-AUTO-FITS ITS OWN COMMAND/OUTS
    # COMBO, WHICH SHIFTS THE AVAILABLE SLOT BUDGET UNDER US AND MAKES THE SEARCH INCONSISTENT WITH
    # WHAT THE FINAL CARD (BUILT WITH ONE FIXED COMBO) ACTUALLY SHOWS.
    if state["command"] is None or state["outs"] is None:
        baseline_chart = card_data.get("chart") or {}
        state["command"] = baseline_chart.get("command")
        state["outs"] = baseline_chart.get("outs_full")

    for cat, target in targets.items():
        stat_key = CHART_SLOT_STAT_KEY[cat]
        category = ChartCategory(cat)
        best_value, best_count, best_diff = None, None, None
        for candidate in range(0, CHART_SLOT_SEARCH_MAX + 1):
            trial_state = copy.deepcopy(state)
            trial_state["stats"][stat_key] = candidate
            try:
                trial_card = construct_card(card_data, trial_state)
            except Exception:
                continue
            # COUNT ACTUAL RENDERED DIE-ROLL SLOTS (chart.results), NOT chart.values - THE LATTER IS
            # AN INTERNAL POINT-WEIGHTED UNIT, NOT A SLOT COUNT, AND CAN BE NONZERO EVEN WHEN A
            # CATEGORY RENDERS 0 SLOTS (ROUNDS DOWN) ON THE ACTUAL CARD.
            actual = list(trial_card.chart.results.values()).count(category)
            diff = abs(actual - target)
            if best_diff is None or diff < best_diff:
                best_value, best_count, best_diff = candidate, actual, diff
            if diff == 0:
                break
        if best_value is None:
            print(f"Warning: couldn't find any {stat_key} value producing a valid chart for {cat} - leaving it unchanged")
            continue
        state["stats"][stat_key] = best_value
        if best_count == target:
            print(f"{cat}: {stat_key}={best_value} -> {best_count} slots (exact match)")
        else:
            print(f"{cat}: couldn't hit exactly {target} slots - closest is {best_count} slots at {stat_key}={best_value}")
    return state


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--list", action="store_true", help="List characters with a saved sidecar in the output folder, then exit")
    parser.add_argument("--character-name", default=None, help="Name to display on the card, ex: 'Spider-Man'. Also the key used to save/load edit state.")
    parser.add_argument("--base-name", default=None, help="Real player name (or bref id / MLB id) to source stats from. Required the first time a character is created; optional on edits.")
    parser.add_argument("--base-year", default=None, help="Real player's year, ex: 1997. Required the first time; optional on edits.")
    parser.add_argument("--set", default=None, help=f"Showdown set to use (default: {DEFAULT_SET})")
    parser.add_argument("--image", default=None, help="Path to a local character image file. Required the first time; optional on edits.")
    parser.add_argument("--stat", action="append", dest="stats", default=[], help="Raw stat override as KEY=VALUE (ex: --stat HR=12 --stat SO=300). Repeatable. Merges into any saved overrides.")
    parser.add_argument("--command", type=int, default=None, help="Control/onbase number override (use with --outs)")
    parser.add_argument("--outs", type=int, default=None, help="Outs override (use with --command)")
    parser.add_argument("--defense", default=None, help="Defense/role override as 'POS=RATING,POS2=RATING2' (ex: 'SP=0' for a starting pitcher role, 'SS=15' for a hitter). Merges into any saved overrides.")
    parser.add_argument("--chart-slots", default=None, help=f"Target an EXACT number of chart die-roll slots for a category, as 'CAT=COUNT,CAT2=COUNT2' (ex: '2B=2,3B=4'). Only supports {sorted(CHART_SLOT_STAT_KEY)} - searches for the --stat value that produces exactly that many slots, rather than you guessing one. Not merged with --stat for the same category in the same run.")
    parser.add_argument("--speed", type=int, default=None, help="Hitter speed rating override")
    parser.add_argument("--speed-letter", choices=["A", "B", "C"], default=None, help="Hitter speed letter override (default B if --speed set)")
    parser.add_argument("--ip", type=int, default=None, help="Pitcher innings-pitched rating override")
    parser.add_argument("--team-override", default=None, help="Use a specific team's colors/logo for the card background")
    parser.add_argument("--parallel", default=None, help=f"Visual theme/parallel for the card. Name or short code, ex: 'COMIC_BOOK_HERO' or 'CB'. Options: {sorted(p.name for p in ImageParallel)}")
    parser.add_argument("--dark-mode", action=argparse.BooleanOptionalAction, default=None, help="Toggle dark mode (2022+ showdown-bot sets only)")
    # TEAM LOGO / BOT LOGO / YEAR BADGE ARE HIDDEN BY DEFAULT THE FIRST TIME A CHARACTER IS CREATED.
    # LEFT UNSET (None) ON A GIVEN RUN, THEY INHERIT WHATEVER WAS SAVED LAST TIME.
    parser.add_argument("--hide-team-logo", action=argparse.BooleanOptionalAction, default=None, help="Hide the real team's logo/branding (default: hidden, on first creation)")
    parser.add_argument("--hide-bot-logo", action=argparse.BooleanOptionalAction, default=None, help="Hide the Showdown Bot logo badge (default: hidden, on first creation)")
    parser.add_argument("--hide-set-year-text", action=argparse.BooleanOptionalAction, default=None, help="Hide the real player's year badge (default: hidden, on first creation)")
    parser.add_argument("--set-number", default=None, help="Card number to print in place of the real year badge (ex: '1/98' for checklist #1 of a themed 98-card set). Leaving this unset renders the badge empty.")
    parser.add_argument("--output-folder", default=None, help=f"Where to save the image + edit state (default: {DEFAULT_OUTPUT_FOLDER})")
    parser.add_argument("--output-filename", default=None, help="Filename (no extension) to save as. Defaults to the character name. Re-running with the same name overwrites the file.")
    parser.add_argument("--show", action="store_true", help="Open the generated image when done")

    args = parser.parse_args()

    output_folder = args.output_folder or DEFAULT_OUTPUT_FOLDER

    if args.list:
        characters = list_characters(output_folder)
        if not characters:
            print(f"No saved characters in {output_folder}")
        else:
            print("\n".join(characters))
        return

    if not args.character_name:
        print("Error: --character-name is required")
        sys.exit(1)

    slug = slugify(args.character_name, args.output_filename)
    prev_state = load_state(output_folder, slug)

    try:
        state = merge_args_into_state(args, prev_state)
        chart_slot_targets = parse_chart_slot_targets(args.chart_slots)
    except ValueError as e:
        print(f"Error: {e}")
        sys.exit(1)

    print("EDITING existing character" if prev_state else "CREATING new character")

    try:
        card_data = fetch_baseline_card_data(state["base_name"], state["base_year"], state["set"])
        if chart_slot_targets:
            state = resolve_chart_slot_targets(card_data, state, chart_slot_targets)
        card = construct_card(card_data, state)
        render_card(card, state["character_name"], state["output_filename"], output_folder, args.show)
    except Exception as e:
        print(f"Error building character card: {e}")
        sys.exit(1)

    save_state(output_folder, slug, state)

    card.print_player()


if __name__ == "__main__":
    main()
