"""Straighten the box diagrams a model draws in ```diagram blocks before they're shown.

A model lays a diagram out by counting characters, and sometimes the count is off: a row one cell too long or short,
or text wider than its box, which pushes the │ out of line. Everything here works in terminal cells (a wide character
fills two, a combining accent none), so it's what the screen shows that gets lined up.

fix(src): for every box (a ┌──┐ top, │ sides, a └──┘ bottom), each row's right edge is moved onto the column of the
top's ┐, trading spaces between the inside and the outside of the edge so the rest of the row stays put. When the text
doesn't fit, the box is widened instead, taking the extra cells from the gap after its right edge where there is one,
so boxes further along the row stay where they are. Rows that already line up are never touched.
"""

from rich.cells import cell_len

TOP_LEFT, TOP_RIGHT = "┌╭╔┏", "┐╮╗┓"
BOTTOM_LEFT, BOTTOM_RIGHT = "└╰╚┗", "┘╯╝┛"
LEFT_SIDES = "│┃║├┠╟╠┣"
RIGHT_SIDES = "│┃║┤┨╢╣┫"
HORIZONTAL = "─━═┬┴┼╤╧"
REACH = 8  # how far (cells) from where it belongs a row's right edge is still taken for this box's
MAX_PASSES = 60


def cells(line):
    """A line as one string per terminal cell: a wide character fills two (the second is ""), a combining accent
    stays with the character before it."""
    out = []
    for ch in line:
        w = cell_len(ch)
        if w == 0 and out:
            out[-1] += ch
        elif w == 2:
            out += [ch, ""]
        else:
            out.append(ch)
    return out


def _is(c, chars):
    """c is one of chars (an empty cell, past the end or behind a wide character, never is)."""
    return bool(c) and c in chars


def _at(rows, j, x):
    return rows[j][x] if 0 <= j < len(rows) and 0 <= x < len(rows[j]) else ""


def _tops(rows):
    """Every box top: (row, left column, right column)."""
    found = []
    for j, row in enumerate(rows):
        x = 0
        while x < len(row):
            if _is(row[x], TOP_LEFT):
                k = x + 1
                while k < len(row) and _is(row[k], HORIZONTAL):
                    k += 1
                if k < len(row) and _is(row[k], TOP_RIGHT) and k > x + 1:
                    found.append((j, x, k))
                    x = k
            x += 1
    return found


def _edge(row, x0, x1, chars):
    """The column of this row's right edge: the edge character nearest to x1 (within REACH), right of x0."""
    near = [k for k in range(max(x0 + 1, x1 - REACH), min(len(row), x1 + REACH + 1)) if _is(row[k], chars)]
    return min(near, key=lambda k: (abs(k - x1), k)) if near else None


def _spare(row, e):
    """How many cells of the run of spaces (or ─) starting at e can go, keeping its last one; None if nothing follows
    (then there's nothing to keep in place)."""
    if e >= len(row):
        return None
    n = 0
    while e + n + 1 < len(row) and row[e + n] in (" ", "─") and row[e + n + 1] == row[e + n]:
        n += 1
    return n


def _take_after(row, e, n):
    """Remove up to n cells of the run of spaces (or ─) starting at e, keeping its last one."""
    del row[e:e + min(n, _spare(row, e) or 0)]


def _shift_edge(row, k, x1, fill):
    """Move the edge at k to x1, trading cells across it so the rest of the row stays where it is. → the overflow
    (cells the edge is still right of x1 because the inside is full), or 0."""
    if k > x1:  # too far right: drop spaces (or ─) just inside the edge
        free = 0
        while free < k - x1 and k - 1 - free > 0 and row[k - 1 - free] == fill and (
                fill != " " or k - 2 - free >= 0 and row[k - 2 - free] == " "):
            free += 1
        if free:
            del row[k - free:k]
            k -= free
            after = "─" if _is(_at([row], 0, k + 1), HORIZONTAL) else " "
            if k + 1 < len(row):  # something follows the edge: keep it where it was
                row[k + 1:k + 1] = [after] * free
        return k - x1
    if k < x1:  # too far left: pad inside, take the same back from a gap right after the edge
        n = x1 - k
        row[k:k] = [fill] * n
        _take_after(row, x1 + 1, n)
    return 0


def _fix_box(rows, top, x0, x1):
    """Line up one box's rows with its top. → True if anything changed."""
    sides, bottom = [], None
    j = top + 1
    while j < len(rows):
        c = _at(rows, j, x0)
        if _is(c, LEFT_SIDES):
            sides.append(j)
        elif _is(c, BOTTOM_LEFT):
            bottom = j
            break
        else:
            break
        j += 1
    if bottom is None or not sides:
        return False
    before = [list(r) for r in rows]
    over = {}
    for j in sides + [bottom]:
        row = rows[j]
        chars = BOTTOM_RIGHT if j == bottom else RIGHT_SIDES
        if _is(_at(rows, j, x1), chars):
            continue
        k = _edge(row, x0, x1, chars)
        if k is None:
            if j != bottom and len(row) <= x1 and "".join(row[x0 + 1:]).strip() == "".join(row[x0 + 1:]).rstrip():
                row += [" "] * (x1 - len(row)) + ["│"]  # the right edge is missing: put it back
            continue
        fill = "─" if j == bottom or _is(_at(rows, j, k - 1), HORIZONTAL) else " "
        if o := _shift_edge(row, k, x1, fill):
            over[j] = o
    if over:  # the text doesn't fit: widen the whole box
        # What follows each row's edge moves on by the same amount, so whatever stands to the right stays one piece:
        # w less what every row can spare from the gap after its edge (a full row was already pushed out by its
        # overflow, so it has that much less to give back).
        w = max(over.values())
        box = [top] + sides + [bottom]
        spares = [s for j in box if (s := _spare(rows[j], x1 + over.get(j, 0) + 1)) is not None]
        give = min([w] + spares)
        for j in box:
            row = rows[j]
            edge = x1 + over.get(j, 0)
            fill = "─" if j in (top, bottom) or _is(_at(rows, j, edge - 1), HORIZONTAL) else " "
            n = w - over.get(j, 0)
            row[edge:edge] = [fill] * n
            _take_after(row, edge + n + 1, give)
    return rows != before


def fix(src):
    """The diagram with its widths evened out and its boxes squared up (see the module doc)."""
    rows = [cells(line) for line in src.split("\n")]
    for _ in range(MAX_PASSES):
        if not any(_fix_box(rows, *t) for t in _tops(rows)):
            break
    return "\n".join("".join(r) for r in rows)


def misaligned(src):
    """Rows whose right edge isn't under their box's top corner: [(row, expected column, found column)] (for tests)."""
    rows = [cells(line) for line in src.split("\n")]
    out = []
    for top, x0, x1 in _tops(rows):
        j = top + 1
        while j < len(rows) and _is(_at(rows, j, x0), LEFT_SIDES + BOTTOM_LEFT):
            chars = BOTTOM_RIGHT if _is(_at(rows, j, x0), BOTTOM_LEFT) else RIGHT_SIDES
            if not _is(_at(rows, j, x1), chars):
                out.append((j, x1, _edge(rows[j], x0, x1, chars)))
            if _is(_at(rows, j, x0), BOTTOM_LEFT):
                break
            j += 1
    return out

