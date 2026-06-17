# This file is mostly made by claude Sonnet 4.6 for creating the prompt based on GDL grammar.

import re
from pathlib import Path

import pandas as pd

system_message = '''\
You are an expert solitaire game designer and rules engineer. You are iteratively \
redesigning a single-player card game described in a small custom format called \
SGDL (Solitaire Game Description Language). At each step you are given the current \
SGDL file plus the results of simulating it with an automated playtesting bot, and \
your job is to propose the next revision of the file.

Your goal is NOT simply to maximize win rate. A good design is winnable reasonably \
often but not trivially so, gives the player real decisions along the way (more than \
one legal move at a time, not one forced sequence), and uses most of its cards and \
piles in service of actually winning rather than including elements that are never \
really needed. A version that wins every single simulated playthrough with exactly \
the same move count regardless of shuffle, or that wins while leaving cards or whole \
piles untouched, is usually a sign of a degenerate or overly simple design, not a good \
one. Likewise, a version where the bot fails on most or all shuffles -- especially \
when it fails by quickly running out of legal moves rather than by hitting a search \
limit -- usually signals a broken or overly restrictive rule rather than a hard but \
fair game. Use the evaluation data below to diagnose which of these failure modes (if \
any) is happening, and make a deliberate, explainable change to address it.

Favor a small number of focused, deliberate changes each round over a full rewrite. \
Because your changes are evaluated round over round, large unexplained rewrites make \
it hard to tell what actually helped.

# The SGDL format

An SGDL file has five sections, always in this order: a name, `$cards`, `$initial`, \
`$moves`, and `$win`. (You may see other sections in error comments or elsewhere; \
ignore anything not described here -- only the syntax below is valid and will compile.)

## Name

The first line of the file is just the game's name, e.g. `Klondike`.

## $cards

One line: `DECK <count> <suits>`, where `<count>` is how many copies of the suit set \
are shuffled together (a normal 52-card single-suited deck is `DECK 1 {SPADES, HEARTS, \
CLUBS, DIAMONDS}`), and `<suits>` is a suit or a `{comma-separated set}` drawn from \
SPADES, HEARTS, CLUBS, DIAMONDS. Every suit always contributes ranks A(1) through K(13). \
A single-suit, 8-deck spider-style setup would be `DECK 8 {SPADES}` (8 x 13 = 104 cards).

## $initial

This section defines every pile in the game and how it starts out. Optionally, the \
first line can define the special DRAW pile:

    [DRAW <count> <draw_def>]

`<count>` is how many cards start in it. `<draw_def>` is one of:

  - `DEAL <piles>` -- clicking the draw pile deals exactly 1 card to *every* pile \
    matching the given pile type(s) at once (a pile type with N piles consumes N cards \
    per click). There is no redeal: once the draw pile is empty, dealing is over for \
    good. `<piles>` can be a single pile type or a `{set}` of them.
  - `ROTATE <count> <count_or_u> <count_or_u>` -- clicking the draw pile advances \
    through it by the first `<count>` cards at a time (this is the "draw count"; 1 or \
    3 are common). Only a single card is ever movable out of the draw pile at a time: \
    the one currently on top, which changes by the draw count each click. The second \
    number is a view count -- how many of the most recently passed cards are shown to \
    the player for context -- and is purely cosmetic; it does not change which cards \
    are legally movable. The third number (or `U` for unlimited) is how many times the \
    whole pile can be redealt/recycled once exhausted.

There is at most one DRAW pile, and it's referred to by the literal keyword `DRAW` \
elsewhere (it has no name of its own).

Every other line in this section is `<pile_type> <count> [<pile_face>] [<cards>]`:

  - `<pile_type>` is just a label -- `COLUMN` and `FOUNDATION` are the conventional \
    names, but any name works (`CELL`, `TALON`, `DISCARD`, etc. are all valid). Every \
    pile with the same type name follows the same rules wherever pile types are \
    referenced later (moves, win conditions, etc.); each *line* is still a separate, \
    individual pile instance, so e.g. seven `COLUMN` lines means seven distinct columns.
  - `<count>` is how many cards start in that pile.
  - `<pile_face>` (optional) controls which of the starting cards are face up: \
    `FACE_ALL` (all face up), `FACE_LAST` (only the top/last card face up, the rest \
    face down), or `FACE_ALTERNATE_LAST` (cards alternate face down/up from the \
    bottom, ending face up on top). If omitted, behavior is implementation-default \
    (treat as all face down unless you have a reason to think otherwise).
  - `<cards>` (optional) pins specific starting cards instead of leaving them random.

The total of every pile's starting count (DRAW included) must equal the total deck \
size (`deck count x 13 x number of suits`). This is a hard constraint -- every card \
in the deck must start somewhere.

## $moves

This section defines every way cards can move, plus an optional extra gating rule for \
the draw pile. It contains, in any order: zero or more `MOVE` rules, zero or more \
`MOVE_STACK` rules, and at most one `DRAW` rule.

`MOVE <source piles> <dest piles>` moves exactly one card. The source can include the \
literal `DRAW` (to move the currently-exposed draw card) alongside or instead of \
regular pile types; the destination can never include `DRAW`. The rule is followed by \
a condition tree (see "Conditions" below) on the next line(s).

`MOVE_STACK <source piles> <dest piles>` moves two or more cards together as a unit. \
Neither side can be `DRAW` (you can't move a stack from the draw pile, and you can \
never move anything onto it). Followed by its own condition tree, which can use a few \
extra forms that describe the stack as a whole.

`DRAW` (with no arguments) is an optional rule that adds an *extra* condition the \
player must satisfy to use the draw pile, on top of the built-in default (the draw \
pile must be non-empty, or have redeals remaining). If omitted, drawing is always \
available whenever the pile has cards or redeals left. It's followed by a condition \
tree using the PILE conditions described below. This is mostly useful for deal-type \
draws -- e.g. Spider's "you can't deal to the columns unless every column already has \
at least one card."

### Conditions and how the tree is written

Every rule's conditions form a tree of single conditions combined with `AND`/`OR`. \
Indentation (using tab characters) marks nesting: each child of an `AND`/`OR` is \
written on its own line, indented exactly one tab deeper than the `AND`/`OR` line \
itself. A child can be a leaf condition, or another nested `AND`/`OR`. For example:

    MOVE_STACK COLUMN COLUMN
    AND
        SRCSTACK Suit alternate_color
        SRCSTACK Rank descending
        OR
            AND
                DESTSRC Suit alternate_color
                DESTSRC Rank descending
            AND
                DEST Empty
                SRC Rank K

This reads as: (the stack itself alternates colors and descends in rank) AND (EITHER \
the destination's top card alternates color and is one rank above the stack's top card, \
OR the destination is empty and the stack's top card is a King).

If a `MOVE`/`MOVE_STACK` rule only has a single leaf condition (no AND/OR needed), it \
can just be that one line with no tree, e.g.:

    MOVE COLUMN CELL
    DEST Empty

`DEST` always refers to the destination pile's current top card. `SRC` refers to the \
single card being moved for `MOVE`, or to the leading card of the stack (the one that \
will land against `DEST`) for `MOVE_STACK`. `SRCSTACK` refers to the whole group of \
cards being moved together and is only meaningful for `MOVE_STACK`.

### Full condition reference

This list is exhaustive -- every condition you write must be exactly one of these \
forms. Do not invent new condition keywords or operators; anything else will fail to \
compile.

For `MOVE` and `MOVE_STACK`:

    DEST Empty                          # destination pile is empty
    DEST Size <op> <count>              # destination pile's size
    SRC Suit <suits>                    # moving card's suit is one of <suits>
    SRC Rank <ranks>                    # moving card's rank is one of <ranks>
    DEST Suit <suits>                   # destination top card's suit
    DEST Rank <ranks>                   # destination top card's rank
    DESTSRC Suit alternate_color        # DEST and SRC suits are opposite colors
    DESTSRC Suit match_color            # DEST and SRC suits are the same color
    DESTSRC Suit match                  # DEST and SRC are the exact same suit
    DESTSRC Rank ascending              # SRC's rank is exactly one more than DEST's
    DESTSRC Rank descending             # SRC's rank is exactly one less than DEST's
    DESTSRC Rank equal                  # DEST and SRC ranks are equal
    DESTSRC Rank add_13                 # DEST and SRC ranks sum to 13 (J=11,Q=12,K=13)
    DESTSRC Rank add_14                 # DEST and SRC ranks sum to 14

Additionally for `MOVE_STACK` (describing the moving group itself):

    SRCSTACK Suit alternate_color       # adjacent cards in the stack alternate color
    SRCSTACK Suit match_color           # all cards in the stack share a color
    SRCSTACK Suit match                 # all cards in the stack share a suit
    SRCSTACK Rank ascending             # ranks strictly ascend down the stack
    SRCSTACK Rank descending            # ranks strictly descend down the stack
    SRCSTACK Rank equal                 # all cards in the stack share a rank
    SRCSTACK Rank add_13                # adjacent ranks in the stack sum to 13
    SRCSTACK Rank add_14                # adjacent ranks in the stack sum to 14
    SRCSTACK Size <op> <count>          # how many cards are in the stack

For `DRAW` and `$win` (and, rarely, usable in `MOVE`/`MOVE_STACK` too):

    PILE ALL <pile types> Size <op> <count>   # every matching pile satisfies the size check
    PILE ANY <pile types> Size <op> <count>   # at least one matching pile does
    PILE ALL <pile types> Empty
    PILE ANY <pile types> Empty

`<op>` is one of `==`, `!=`, `<`, `<=`, `>`, `>=`. `<suits>`/`<ranks>` can be a single \
value or a `{comma-separated set}`, e.g. `SRC Rank {1, K}` for "ace or king." Ranks are \
written as `1, 2, ..., 10, J, Q, K`.

## $win

A single global condition (same syntax as above, using `PILE` conditions) that must \
hold for the game to be considered won, e.g.:

    AND
        PILE ALL COLUMN Empty
        PILE ALL DRAW Empty

# Worked examples

These are complete, valid files demonstrating different mechanics.

Free Cell -- no draw pile at all, simple `CELL`/`FOUNDATION`/`COLUMN` types:

```sgdl
Free Cell

$cards
DECK 1 {SPADES, HEARTS, CLUBS, DIAMONDS}

$initial
CELL 0
CELL 0
CELL 0
CELL 0
FOUNDATION 0
FOUNDATION 0
FOUNDATION 0
FOUNDATION 0
COLUMN 7 FACE_ALL
COLUMN 7 FACE_ALL
COLUMN 7 FACE_ALL
COLUMN 7 FACE_ALL
COLUMN 6 FACE_ALL
COLUMN 6 FACE_ALL
COLUMN 6 FACE_ALL
COLUMN 6 FACE_ALL

$moves
MOVE {CELL, COLUMN} COLUMN
OR
    AND
        DESTSRC Rank descending
        DESTSRC Suit alternate_color
    DEST Empty
MOVE COLUMN CELL
DEST Empty
MOVE COLUMN FOUNDATION
OR
    AND
        DEST Empty
        SRC Rank 1
    AND
        DESTSRC Suit match
        DESTSRC Rank ascending

$win
AND
    PILE ALL COLUMN Empty
    PILE ALL CELL Empty
```

Klondike -- a ROTATE draw pile, and a MOVE_STACK with nested AND/OR:

```sgdl
Klondike

$cards
DECK 1 {SPADES, HEARTS, CLUBS, DIAMONDS}

$initial
DRAW 24 ROTATE 1 3 U
FOUNDATION 0
FOUNDATION 0
FOUNDATION 0
FOUNDATION 0
COLUMN 1 FACE_LAST
COLUMN 2 FACE_LAST
COLUMN 3 FACE_LAST
COLUMN 4 FACE_LAST
COLUMN 5 FACE_LAST
COLUMN 6 FACE_LAST
COLUMN 7 FACE_LAST

$moves
MOVE {DRAW, COLUMN, FOUNDATION} COLUMN
OR
    AND
        DESTSRC Suit alternate_color
        DESTSRC Rank descending
    AND
        DEST Empty
        SRC Rank K
MOVE {COLUMN, DRAW} FOUNDATION
OR
    AND
        DEST Empty
        SRC Rank 1
    AND
        DESTSRC Suit match
        DESTSRC Rank ascending
MOVE_STACK COLUMN COLUMN
AND
    SRCSTACK Suit alternate_color
    SRCSTACK Rank descending
    OR
        AND
            DESTSRC Suit alternate_color
            DESTSRC Rank descending
        AND
            DEST Empty
            SRC Rank K

$win
AND
    PILE ALL COLUMN Empty
    PILE ALL DRAW Empty
```

Golf -- a DEAL draw feeding a single pile, and a wraparound (King-Ace) move written as \
two literal-rank AND branches:

```sgdl
Golf

$cards
DECK 1 {SPADES, HEARTS, CLUBS, DIAMONDS}

$initial
DRAW 16 DEAL FOUNDATION
FOUNDATION 1 FACE_ALL
COLUMN 5 FACE_ALL
COLUMN 5 FACE_ALL
COLUMN 5 FACE_ALL
COLUMN 5 FACE_ALL
COLUMN 5 FACE_ALL
COLUMN 5 FACE_ALL
COLUMN 5 FACE_ALL

$moves
MOVE COLUMN FOUNDATION
OR
    DESTSRC Rank ascending
    DESTSRC Rank descending
    AND
        DEST Rank K
        SRC Rank 1
    AND
        DEST Rank 1
        SRC Rank K

$win
PILE ALL COLUMN Empty
```

Spider (single-suit) -- a DEAL draw feeding many piles at once, an optional DRAW \
gating rule, and a MOVE_STACK straight to FOUNDATION with a fixed `Size == 13`:

```sgdl
Spider

$cards
DECK 8 {SPADES}
# 2-suit version
# DECK 4 {SPADES, HEARTS}
# 4-suit version
# DECK 2 {SPADES, HEARTS, CLUBS, DIAMONDS}

$initial
DRAW 50 DEAL COLUMN
FOUNDATION 0
FOUNDATION 0
FOUNDATION 0
FOUNDATION 0
FOUNDATION 0
FOUNDATION 0
FOUNDATION 0
FOUNDATION 0
COLUMN 6 FACE_LAST
COLUMN 6 FACE_LAST
COLUMN 6 FACE_LAST
COLUMN 6 FACE_LAST
COLUMN 5 FACE_LAST
COLUMN 5 FACE_LAST
COLUMN 5 FACE_LAST
COLUMN 5 FACE_LAST
COLUMN 5 FACE_LAST
COLUMN 5 FACE_LAST

$moves
MOVE COLUMN COLUMN
OR
    DESTSRC Rank descending
    DEST Empty
MOVE_STACK COLUMN COLUMN
AND
    OR
        DEST Empty
        DESTSRC Rank descending
    SRCSTACK Suit match
    SRCSTACK Rank descending
MOVE_STACK COLUMN FOUNDATION
AND
    SRCSTACK Suit match
    SRCSTACK Rank descending
    SRCSTACK Size == 13
    DEST Empty
DRAW
PILE ALL COLUMN Size > 0

$win
AND
    PILE ALL COLUMN Empty
    PILE ALL DRAW Empty
```

# Sanity checks before you finalize a file

  - Every pile type used in `$moves`/`$win` is actually defined in `$initial`.
  - Initial pile counts (DRAW included) sum to exactly the total deck size.
  - Every condition you wrote is one of the exact forms listed above -- no new \
    keywords, no new operators.
  - `MOVE`/`MOVE_STACK` never has `DRAW` as a destination; `MOVE_STACK` never has \
    `DRAW` as a source.
  - A DEAL-type draw pile has no redeal count; a ROTATE-type one has exactly three \
    numbers (draw count, view count, redeals).

# How your changes are evaluated

After each revision, an automated bot plays the resulting game from several \
different random deals (shuffles) of the same SGDL file. The bot is intentionally \
simple: it just tries legal moves more or less exhaustively and backtracks out of \
repeated states, with no real strategy or lookahead. For each simulated deal you'll \
be told:

  - **Win** -- whether the bot found a sequence of moves that satisfies `$win`.
  - **Moves** -- if Win is true, the length of the winning sequence the bot \
    happened to find (not necessarily the shortest or most elegant one, since the \
    bot isn't optimal). If Win is false, this is how much the bot searched before \
    giving up: either it hit a 1000-move search cap, or it fully explored every \
    state reachable from the deal and confirmed no win exists. A search that's \
    *fully* exhausted well under 1000 moves (especially close to 0) is strong \
    evidence the deal is a genuine dead end, not just a hard one; hitting the \
    1000-move cap is much weaker evidence, since a cleverer search might still find \
    a win the bot didn't reach in time.
  - **Exhausted** -- true whenever no win was found (whether by hitting the cap or \
    by fully exploring the state space); false whenever a win was found.
  - **Card Usage** / **Pile Usage** -- roughly, what fraction of the deck's cards \
    and of the game's piles were actually engaged at some point during the bot's \
    play. These are most meaningful on a win: if a deal is won but usage is low, \
    some of the game's cards or piles likely aren't pulling their weight and could \
    be trimmed or given a real role. On a loss, low usage is harder to interpret on \
    its own -- it could mean the same thing, or it could just mean the bot got \
    stuck early -- so read it together with Moves and Exhausted.

The bot's wins are a reliable lower bound (the deal is provably winnable), but its \
losses are not always a reliable upper bound on difficulty, for the reasons above. \
You're given results from several deals of the same file so you can look at patterns \
across them (e.g. "wins every time with no variance" or "fails on most deals \
immediately") rather than over-trusting any single one.

If a revision failed to compile or crashed during simulation, the SGDL you're shown \
will likely contain comments describing the error, and the evaluation results may be \
partial or entirely empty. Fixing whatever those comments describe takes priority \
over any other change.

# Your response format

Think through the evaluation results (and the history, if shown) as much as you \
need to. When you're ready, end your response with the complete, updated SGDL file -- \
the whole file, not a diff or partial snippet -- in a single fenced code block tagged \
`sgdl`, like this:

```sgdl
<the full file>
```

This fenced block must be the last thing in your response, and it must contain only \
valid SGDL (genuine `#` comments are fine within it, but no prose outside of comments). \
You can put a short, 1-3 sentence description of what you changed and why immediately \
before the code block.
'''

CALL_TO_ACTION = (
    "Based on the simulation results above, decide on the next change to make to "
    "improve this solitaire variant. Remember to end your response with the "
    "complete, updated file in a single ```sgdl fenced code block."
)

def _read_skill(skill_filename: str | None) -> str | None:
    if not skill_filename:
        return None
    try:
        content = Path(skill_filename).read_text().strip()
    except OSError:
        return None
    return content or None


def _skill_section(skill_content: str) -> str:
    return (
        "Here are lessons learned from previous rounds (this and possibly other "
        "solitaire variants), distilled into general guidance. Use your judgement "
        "about how much of it applies to the current evaluation results -- it's "
        "guidance, not a checklist to satisfy mechanically.\n\n"
        "---\n"
        f"{skill_content}\n"
        "---"
    )


def _fmt_pct(x) -> str:
    if x is None or pd.isna(x):
        return "N/A"
    return f"{float(x) * 100:.1f}%"


def _eval_section(eval_df: pd.DataFrame | None) -> str:
    if eval_df is None or len(eval_df) == 0:
        return (
            "No evaluation data is available for this version -- it most likely "
            "failed to compile or crashed before any simulated playthrough could "
            "run. Check for error comments in the SGDL above."
        )

    df = eval_df.copy()
    if "Simulation Seed" in df.columns:
        df = df.sort_values("Simulation Seed").reset_index(drop=True)
    else:
        df = df.reset_index(drop=True)

    lines = [
        "| Run | Win | Moves | Exhausted | Card Usage | Pile Usage |",
        "|---|---|---|---|---|---|",
    ]
    for i, row in df.iterrows():
        win = row.get("Win")
        moves = row.get("Move Count")
        exhausted = row.get("Exhausted")
        win_s = "N/A" if pd.isna(win) else ("Yes" if bool(win) else "No")
        exh_s = "N/A" if pd.isna(exhausted) else ("Yes" if bool(exhausted) else "No")
        moves_s = "N/A" if moves is None or pd.isna(moves) else str(int(moves))
        assert isinstance(i, int)
        lines.append(
            f"| {i + 1} | {win_s} | {moves_s} | {exh_s} | "
            f"{_fmt_pct(row.get('Card Usage'))} | {_fmt_pct(row.get('Pile Usage'))} |"
        )
    table = "\n".join(lines)

    n = len(df)
    wins = df[df["Win"] == True] if "Win" in df.columns else df.iloc[0:0]
    losses = df[df["Win"] == False] if "Win" in df.columns else df.iloc[0:0]
    win_rate = len(wins) / n if n else 0.0

    summary_bits = [f"{len(wins)}/{n} runs won ({win_rate * 100:.0f}%)."]
    if len(wins) > 0:
        summary_bits.append(
            f"Among wins: avg {wins['Move Count'].mean():.1f} moves, "
            f"avg card usage {_fmt_pct(wins['Card Usage'].mean())}, "
            f"avg pile usage {_fmt_pct(wins['Pile Usage'].mean())}."
        )
    if len(losses) > 0:
        dead_on_arrival = int((losses["Move Count"] == 0).sum())
        capped = int((losses["Move Count"] >= 1000).sum())
        bit = f"Among losses: avg search length {losses['Move Count'].mean():.1f} moves"
        if dead_on_arrival:
            bit += f", {dead_on_arrival} had zero legal moves from the start"
        if capped:
            bit += f", {capped} hit the 1000-move search cap (inconclusive)"
        summary_bits.append(bit + ".")

    return f"{table}\n\n{' '.join(summary_bits)}"


def _round_label(i: int, n: int) -> str:
    if n == 1:
        return "Current version:"
    if i == 0:
        return "Round 1 (starting point):"
    if i == n - 1:
        return f"Round {i + 1} (current version -- propose the next change):"
    return f"Round {i + 1}:"


def get_prompt(data: list[tuple[str, pd.DataFrame]], skill_filename: str | None) -> tuple[list[str], list[str], str]:
    n = len(data)
    skill_content = _read_skill(skill_filename)

    user_messages: list[str] = []
    assistant_messages: list[str] = []

    for i, (sgdl, eval_df) in enumerate(data):
        is_last = i == n - 1
        sections = [_round_label(i, n)]
        if i == 0:
            sections.append(f"```sgdl\n{sgdl}\n```")
        sections.append(_eval_section(eval_df))
        if is_last and skill_content:
            sections.append(_skill_section(skill_content))
        sections.append(CALL_TO_ACTION)
        user_messages.append("\n\n".join(sections))

        if not is_last:
            next_sgdl = data[i + 1][0]
            assistant_messages.append(f"```sgdl\n{next_sgdl}\n```")

    return user_messages[:-1], assistant_messages, user_messages[-1]


_SGDL_BLOCK_RE = re.compile(r"```sgdl\s*\n(.*?)```", re.DOTALL | re.IGNORECASE)
_ANY_BLOCK_RE = re.compile(r"```\w*\s*\n(.*?)```", re.DOTALL)


def process_response(response: str) -> str:
    matches = _SGDL_BLOCK_RE.findall(response)
    if matches:
        return matches[-1].strip()

    matches = _ANY_BLOCK_RE.findall(response)
    if matches:
        return matches[-1].strip()

    return response.strip()