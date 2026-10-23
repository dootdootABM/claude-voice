"""art_fix: box diagrams squared up before they're drawn. Run: pytest test_art_fix.py"""

import random

import pytest

import art_fix

ALIGNED = [
    # side by side, with accents (a combining macron takes no cell) and arrows
    "╭──────────────────╮   ╭──────────────────╮\n"
    "│  weights θ       │──▶│  weights θ̄       │\n"
    "╰──────────────────╯   ╰──────────────────╯",
    # nested, with a divider and a junction in the bottom
    "┌────────────────────┐\n"
    "│ outer              │\n"
    "│  ┌──────┐          │\n"
    "│  │ in   │          │\n"
    "│  └──────┘          │\n"
    "├────────────────────┤\n"
    "│ more               │\n"
    "└─────────┬──────────┘\n"
    "          ▼",
    "no boxes here → just text",
]


@pytest.mark.parametrize("src", ALIGNED)
def test_what_lines_up_is_left_alone(src):
    assert art_fix.misaligned(src) == [] and art_fix.fix(src) == src


def test_a_row_one_cell_long_moves_its_edge_and_keeps_the_rest_in_place():
    src = ("╭───────────╮\n"
           "│ text       │  note\n"
           "╰───────────╯")
    out = art_fix.fix(src)
    assert out.split("\n")[1] == "│ text      │   note"
    assert art_fix.misaligned(out) == []


def test_a_short_row_is_padded():
    src = ("┌──────────┐\n"
           "│ short  │\n"
           "└──────────┘")
    assert art_fix.fix(src).split("\n")[1] == "│ short    │"


def test_text_wider_than_its_box_widens_the_box_and_its_neighbour_stays_in_one_piece():
    src = ("╭──────────╮      ╭─────╮\n"
           "│ much too wide│──▶   │ b   │\n"
           "│ ok       │      │     │\n"
           "╰──────────╯      ╰─────╯")
    out = art_fix.fix(src)
    assert art_fix.misaligned(out) == []
    rows = out.split("\n")
    assert rows[1].startswith("│ much too wide│")
    edge = rows[0].index("╮")
    second = {r.index(c, edge + 1) for r, c in zip(rows, "╭││╰")}
    assert len(second) == 1  # the second box's left side is one straight column


def test_a_crooked_bottom_is_squared():
    src = ("┌─────┐\n"
           "│ a   │\n"
           "└──────┘")
    out = art_fix.fix(src)
    assert out.split("\n")[2] == "└─────┘" and art_fix.misaligned(out) == []


REAL = [  # two diagrams cv drew that were off by a cell (from its saved outputs)
    "                               ╭───────────────────────────────────────────╮\n"
    "                               │ LOSS = − Σ_i log p_θ( t_{i+1} | t_1..t_i ) │  cross-entropy\n"
    "                               ╰───────────────────────────────────────────╯",
    " ╭────────────────╮   ╭──────────────────╮   ╭───────────────────╮\n"
    " │ run transformer│──►│ take LAST position│──►│ p(next word)      │\n"
    " │ on all tokens  │   │ h_last → softmax │   │ over V            │\n"
    " ╰────────────────╯   ╰──────────────────╯   ╰─────────┬─────────╯\n"
    "                                                       ▼",
]


@pytest.mark.parametrize("src", REAL)
def test_real_diagrams_come_out_square(src):
    assert art_fix.misaligned(src)
    out = art_fix.fix(src)
    assert art_fix.misaligned(out) == []
    assert art_fix.fix(out) == out  # and stay that way


def test_the_third_box_and_its_arrow_stay_put():
    rows = art_fix.fix(REAL[1]).split("\n")
    assert rows[3].index("┬") == rows[4].index("▼") == REAL[1].split("\n")[3].index("┬")


def test_junk_never_breaks_it():
    rng = random.Random(7)
    alphabet = "┌┐└┘╭╮╰╯│─├┤┬┴┼ ab θ̄▶🔥\n"
    for _ in range(300):
        src = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 120)))
        art_fix.fix(src)
