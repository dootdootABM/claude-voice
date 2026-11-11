"""LaTeX maths for the output and live windows.

Two ways to draw a formula:
- to_unicode(tex): plain text with Unicode maths (x² + y², √(a+b), ∑ᵢ, ℝ, α → β). Inline maths ($…$) always uses
  this, so it flows with the sentence; display maths uses it while the image is being made, and when no image can be.
- render(tex, rgb, cell_h, scale): a typeset image (an RGBA PIL image) of display maths ($$…$$, \\[…\\], ```math), for terminals with
  a graphics protocol (kitty, or sixel). It uses the system LaTeX (latex + dvipng: full amsmath, matrices, align, cases)
  when installed, else matplotlib's mathtext when that is installed (a subset), else nothing (None → the Unicode text).

Rendered images are cached on disk by formula, colour and size, so reopening an answer is instant.
"""

import hashlib
import io
import os
import pathlib
import re
import shutil
import subprocess
import tempfile

# ---- TeX → Unicode ----------------------------------------------------------------------------------------------
GREEK = {
    "alpha": "α", "beta": "β", "gamma": "γ", "delta": "δ", "epsilon": "ϵ", "varepsilon": "ε", "zeta": "ζ", "eta": "η",
    "theta": "θ", "vartheta": "ϑ", "iota": "ι", "kappa": "κ", "lambda": "λ", "mu": "μ", "nu": "ν", "xi": "ξ",
    "pi": "π", "varpi": "ϖ", "rho": "ρ", "varrho": "ϱ", "sigma": "σ", "varsigma": "ς", "tau": "τ", "upsilon": "υ",
    "phi": "ϕ", "varphi": "φ", "chi": "χ", "psi": "ψ", "omega": "ω",
    "Gamma": "Γ", "Delta": "Δ", "Theta": "Θ", "Lambda": "Λ", "Xi": "Ξ", "Pi": "Π", "Sigma": "Σ", "Upsilon": "Υ",
    "Phi": "Φ", "Psi": "Ψ", "Omega": "Ω",
}
SYMBOLS = {
    "cdot": "·", "times": "×", "div": "÷", "pm": "±", "mp": "∓", "ast": "∗", "star": "⋆", "circ": "∘", "bullet": "•",
    "le": "≤", "leq": "≤", "ge": "≥", "geq": "≥", "ne": "≠", "neq": "≠", "approx": "≈", "equiv": "≡", "sim": "∼",
    "simeq": "≃", "cong": "≅", "propto": "∝", "ll": "≪", "gg": "≫", "prec": "≺", "succ": "≻", "doteq": "≐",
    "infty": "∞", "partial": "∂", "nabla": "∇", "sum": "∑", "prod": "∏", "coprod": "∐", "int": "∫", "iint": "∬",
    "iiint": "∭", "oint": "∮", "forall": "∀", "exists": "∃", "nexists": "∄", "in": "∈", "notin": "∉", "ni": "∋",
    "subset": "⊂", "subseteq": "⊆", "supset": "⊃", "supseteq": "⊇", "cup": "∪", "cap": "∩", "bigcup": "⋃",
    "bigcap": "⋂", "setminus": "∖", "emptyset": "∅", "varnothing": "∅",
    "to": "→", "rightarrow": "→", "leftarrow": "←", "gets": "←", "leftrightarrow": "↔", "Rightarrow": "⇒",
    "Leftarrow": "⇐", "Leftrightarrow": "⇔", "iff": "⟺", "implies": "⟹", "impliedby": "⟸", "mapsto": "↦",
    "longrightarrow": "⟶", "longleftarrow": "⟵", "uparrow": "↑", "downarrow": "↓", "nearrow": "↗", "searrow": "↘",
    "ldots": "…", "dots": "…", "cdots": "⋯", "vdots": "⋮", "ddots": "⋱",
    "angle": "∠", "perp": "⊥", "parallel": "∥", "mid": " ∣ ", "nmid": "∤", "neg": "¬", "lnot": "¬", "land": "∧",
    "lor": "∨", "wedge": "∧", "vee": "∨", "oplus": "⊕", "ominus": "⊖", "otimes": "⊗", "odot": "⊙", "dagger": "†",
    "langle": "⟨", "rangle": "⟩", "lceil": "⌈", "rceil": "⌉", "lfloor": "⌊", "rfloor": "⌋", "|": "‖", "Vert": "‖",
    "vert": "|", "lvert": "|", "rvert": "|", "lVert": "‖", "rVert": "‖", "backslash": "∖",
    "hbar": "ℏ", "ell": "ℓ", "Re": "ℜ", "Im": "ℑ", "aleph": "ℵ", "wp": "℘", "prime": "′", "degree": "°",
    "top": "⊤", "bot": "⊥", "vdash": "⊢", "models": "⊨", "therefore": "∴", "because": "∵", "square": "□",
    "triangle": "△", "checkmark": "✓",
    "{": "{", "}": "}", "%": "%", "$": "$", "&": "&", "#": "#", "_": "_",
    ",": " ", ";": " ", ":": " ", ">": " ", "!": "", " ": " ", "quad": "  ", "qquad": "    ", "enspace": " ",
    "thinspace": " ", "medspace": " ", "thickspace": " ", "negthinspace": "",
}
FUNCS = ("sin", "cos", "tan", "cot", "sec", "csc", "arcsin", "arccos", "arctan", "sinh", "cosh", "tanh", "log", "ln",
         "lg", "exp", "lim", "liminf", "limsup", "sup", "inf", "max", "min", "arg", "argmax", "argmin", "det", "dim",
         "ker", "gcd", "deg", "hom", "Pr", "tr", "rank", "sgn", "mod", "bmod")
IGNORED = {"tiny", "scriptsize", "footnotesize", "small", "normalsize", "large", "Large", "LARGE", "huge", "Huge",
           "left", "right", "big", "Big", "bigg", "Bigg", "bigl", "bigr", "Bigl", "Bigr", "biggl", "biggr", "displaystyle",
           "textstyle", "scriptstyle", "limits", "nolimits", "nonumber", "notag", "label", "tag", "middle"}
TEXTLIKE = {"text", "textrm", "textit", "textbf", "mathrm", "mathit", "mathsf", "mathtt", "operatorname", "mbox",
            "textnormal", "boldsymbol", "bm", "mathbf", "pmb", "emph"}
ACCENTS = {"hat": "\u0302", "widehat": "\u0302", "bar": "\u0304", "overline": "\u0305", "vec": "\u20d7",
           "dot": "\u0307", "ddot": "\u0308", "tilde": "\u0303", "widetilde": "\u0303", "check": "\u030c",
           "breve": "\u0306", "acute": "\u0301", "grave": "\u0300", "underline": "\u0332"}

SUP = dict(zip("0123456789+-=()abcdefghijklmnoprstuvwxyzABDEGHIJKLMNOPRTUVW",
               "⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻⁼⁽⁾ᵃᵇᶜᵈᵉᶠᵍʰⁱʲᵏˡᵐⁿᵒᵖʳˢᵗᵘᵛʷˣʸᶻᴬᴮᴰᴱᴳᴴᴵᴶᴷᴸᴹᴺᴼᴾᴿᵀᵁⱽᵂ"))
SUP.update({"′": "′", "*": "*", "∗": "*", "·": "·", " ": " ", "α": "ᵅ", "β": "ᵝ", "γ": "ᵞ", "δ": "ᵟ", "θ": "ᶿ",
            "ϕ": "ᵠ", "φ": "ᵠ", "χ": "ᵡ"})
SUB = dict(zip("0123456789+-=()aehijklmnoprstuvx", "₀₁₂₃₄₅₆₇₈₉₊₋₌₍₎ₐₑₕᵢⱼₖₗₘₙₒₚᵣₛₜᵤᵥₓ"))
SUB.update({" ": " ", "β": "ᵦ", "γ": "ᵧ", "ρ": "ᵨ", "φ": "ᵩ", "ϕ": "ᵩ", "χ": "ᵪ", ",": ","})


def _alphabet(upper_start, lower_start=None, digits_start=None, exceptions=None):
    out = {}
    for i in range(26):
        out[chr(65 + i)] = chr(upper_start + i)
        if lower_start:
            out[chr(97 + i)] = chr(lower_start + i)
    if digits_start:
        for i in range(10):
            out[chr(48 + i)] = chr(digits_start + i)
    out.update(exceptions or {})
    return out


STYLES = {  # the Unicode maths alphabets; the gaps in each block are the letters Unicode had already
    "mathbb": _alphabet(0x1D538, 0x1D552, 0x1D7D8, {"C": "ℂ", "H": "ℍ", "N": "ℕ", "P": "ℙ", "Q": "ℚ", "R": "ℝ",
                                                     "Z": "ℤ"}),
    "mathcal": _alphabet(0x1D4D0),  # bold script: every capital exists, and it reads as "calligraphic"
    "mathscr": _alphabet(0x1D4D0),
    "mathfrak": _alphabet(0x1D504, 0x1D51E, None, {"C": "ℭ", "H": "ℌ", "I": "ℑ", "R": "ℜ", "Z": "ℨ"}),
}
STYLES["Bbb"] = STYLES["mathbb"]


class _Parser:
    def __init__(self, src):
        self.s, self.i = src, 0

    def peek(self):
        return self.s[self.i] if self.i < len(self.s) else ""

    def command(self):
        """At a backslash: → the command name (letters, or the one symbol after it)."""
        self.i += 1
        m = re.match(r"[A-Za-z]+", self.s[self.i:])
        if m:
            self.i += m.end()
            return m.group()
        ch = self.peek()
        self.i += 1
        return ch

    def skip_space(self):
        while self.peek().isspace():
            self.i += 1

    def group(self, close="}"):
        """After a "{": the converted text up to the matching close."""
        out = self.seq(close)
        if self.peek() == close:
            self.i += 1
        return out

    def arg(self):
        """One argument: a {group}, a \\command, or one character."""
        self.skip_space()
        ch = self.peek()
        if ch == "{":
            self.i += 1
            return self.group()
        if ch == "\\":
            return self.atom(self.command())
        self.i += 1
        return ch

    def raw_arg(self):
        """An argument's source text, unconverted (for \\text and \\begin)."""
        self.skip_space()
        if self.peek() != "{":
            ch = self.peek()
            self.i += 1
            return ch
        depth, start = 0, self.i
        while self.i < len(self.s):
            c = self.s[self.i]
            depth += (c == "{") - (c == "}")
            self.i += 1
            if depth == 0:
                break
        return self.s[start + 1:self.i - 1]

    def opt(self):
        """An optional [argument] (as in \\sqrt[3]{x}), converted; "" when there isn't one."""
        self.skip_space()
        if self.peek() != "[":
            return ""
        self.i += 1
        return self.group("]")

    def atom(self, name):
        if name in GREEK:
            return GREEK[name]
        if name in SYMBOLS:
            return SYMBOLS[name]
        if name in FUNCS:
            return name + ("" if self.peek() in "_^({[" else " ")
        if name in IGNORED:
            if name in ("label", "tag"):
                self.raw_arg()
            return ""
        if name in TEXTLIKE:
            inner = self.raw_arg()
            return inner if name.startswith("text") or name == "mbox" else to_unicode(inner)
        if name in STYLES:
            table = STYLES[name]
            return "".join(table.get(c, c) for c in self.arg())
        if name in ACCENTS:
            inner = self.arg()
            return inner + ACCENTS[name] if len(inner) == 1 else f"{inner}{ACCENTS[name]}"
        if name in ("frac", "dfrac", "tfrac", "cfrac"):
            num, den = self.arg(), self.arg()
            return f"{_wrap(num)}/{_wrap(den)} "
        if name == "binom":
            n, k = self.arg(), self.arg()
            return f"C({n}, {k})"
        if name == "sqrt":
            index, body = self.opt(), self.arg()
            root = {"": "√", "2": "√", "3": "∛", "4": "∜"}.get(index) or (_script(index, SUP, "^") + "√")
            return root + _wrap(body)
        if name in ("overset", "stackrel"):
            top, base = self.arg(), self.arg()
            return base + _script(top, SUP, "^")
        if name == "underset":
            bottom, base = self.arg(), self.arg()
            return base + _script(bottom, SUB, "_")
        if name in ("mathop", "mathbin", "mathrel", "mathord", "ensuremath", "boxed", "phantom", "hphantom"):
            inner = self.arg()
            return "" if "phantom" in name else inner
        if name in ("color", "textcolor"):
            self.raw_arg()
            return self.arg() if name == "textcolor" else ""
        if name == "begin":
            return self.environment(self.raw_arg())
        if name == "end":
            self.raw_arg()
            return ""
        if name == "\\":
            return "\n"
        if name in ("hline", "midrule", "toprule", "bottomrule"):
            return ""
        return name  # unknown: its name reads better than nothing

    def environment(self, env):
        """\\begin{env} … \\end{env}: rows split at \\\\, cells at &. Matrices in brackets, cases with a brace."""
        end = re.compile(r"\\end\s*\{" + re.escape(env) + r"\}")
        m = end.search(self.s, self.i)
        body = self.s[self.i:m.start()] if m else self.s[self.i:]
        self.i = m.end() if m else len(self.s)
        if env in ("array", "tabular"):
            body = re.sub(r"^\s*\{[^}]*\}", "", body)  # the column spec
        rows = [r for r in re.split(r"\\\\(?:\[[^\]]*\])?", body) if r.strip()]
        cells = [[to_unicode(c).strip() for c in r.split("&")] for r in rows]
        if env.rstrip("*") in ("pmatrix", "bmatrix", "Bmatrix", "vmatrix", "Vmatrix", "matrix", "smallmatrix",
                               "array"):
            l, r = {"pmatrix": "()", "bmatrix": "[]", "Bmatrix": "{}", "vmatrix": "||", "Vmatrix": "‖‖"}.get(
                env.rstrip("*"), "[]")
            return l + "; ".join("  ".join(c) for c in cells) + r
        if env.rstrip("*") in ("cases", "dcases"):
            return "{ " + "; ".join(" if ".join(x for x in (c[0], c[1].removeprefix("if ").strip()) if x)
                                     if len(c) > 1 else c[0] for c in cells) + " }"
        return "\n".join(" ".join(c) for c in cells)  # align, gather, equation, split, …

    def seq(self, close=None):
        out = []
        while self.i < len(self.s):
            ch = self.peek()
            if close and ch == close:
                break
            if ch == "\\":
                out.append(self.atom(self.command()))
            elif ch == "{":
                self.i += 1
                out.append(self.group())
            elif ch in "^_":
                self.i += 1
                body = self.arg()
                out.append(_script(body, SUP if ch == "^" else SUB, ch))
            elif ch == "~":
                self.i += 1
                out.append(" ")
            elif ch == "&":
                self.i += 1
                out.append(" ")
            elif ch == "'":
                self.i += 1
                out.append("′")
            elif ch.isspace():
                self.i += 1
                if out and not out[-1].endswith(" "):
                    out.append(" ")
            else:
                self.i += 1
                out.append(ch)
        return "".join(out)


def _wrap(s):
    """Brackets around a fraction's part or a root's body unless it's one simple term."""
    s = s.strip()
    simple = r"[√∛∜]?[\w.′]+(?:\([^()]*\))?|[√∛∜]?[^\s+\-−=/<>]"
    return s if re.fullmatch(simple, s) or (s[:1] + s[-1:] in ("()", "[]", "{}", "||")) else f"({s})"


def _script(body, table, mark):
    """A super/subscript: Unicode script letters when all of them have one, else ^(…) / _(…)."""
    body = body.strip()
    if body and all(c in table for c in body):
        return "".join(table[c] for c in body)
    if mark == "^" and body in ("′", "′′", "*", "†", "∘", "°"):
        return body
    return f"{mark}{body}" if len(body) == 1 else f"{mark}({body})"


def to_unicode(tex):
    """LaTeX maths → readable Unicode text. Never fails: what it doesn't know comes out as written."""
    try:
        text = _Parser(tex).seq()
    except Exception:  # noqa: BLE001 - a fallback must not break the window
        return tex.strip()
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" ?\n ?", "\n", text)
    text = re.sub(r" ([,;.)\]])", r"\1", text)
    text = re.sub(r"([(\[]) ", r"\1", text)
    return text.strip()


# ---- TeX → image ------------------------------------------------------------------------------------------------
def _cache_dir():
    import platform_util
    d = pathlib.Path(platform_util.state_dir()) / "cache" / "math"
    d.mkdir(parents=True, exist_ok=True)
    return d


def engine():
    """Which renderer is available: "latex", "mathtext" or None."""
    if shutil.which("latex") and shutil.which("dvipng"):
        return "latex"
    try:
        import matplotlib  # noqa: F401
        return "mathtext"
    except ImportError:
        return None


DOC = r"""\documentclass[12pt]{article}
\usepackage{amsmath,amssymb}
\pagestyle{empty}
\begin{document}
\begin{align*}
%s
\end{align*}
\end{document}
"""
ENVS = re.compile(r"\\begin\s*\{(align|gather|equation|multline|alignat|flalign)\*?\}")  # top-level displays


# Formulas come from model output, which can quote web pages. TeX can read files (\input{/etc/passwd} works even with
# openin_any=p), write them, and redefine itself into doing so (\catcode, \csname, ^^5c for a backslash). None of
# that is maths, so a formula that uses any of it never reaches latex: it is shown as Unicode text instead.
UNSAFE = re.compile(r"\^\^|@|\\(?:input|include|includeonly|openin|openout|read|readline|write|immediate|closein|closeout|"
                    r"newread|newwrite|catcode|lccode|uccode|def|edef|gdef|xdef|let|futurelet|csname|endcsname|"
                    r"expandafter|afterassignment|aftergroup|special|usepackage|RequirePackage|documentclass|"
                    r"newcommand|renewcommand|providecommand|newenvironment|renewenvironment|DeclareRobustCommand|"
                    r"makeatletter|lowercase|uppercase|scantokens|detokenize|string|jobname|endinput|everyjob|"
                    r"everypar|everymath|everydisplay|output|shipout|directlua|luaexec|pdf\w+|write18|shellescape|"
                    r"verbatiminput|InputIfFileExists|IfFileExists|openany|filename|font|document|end\s*\{\s*document)"
                    r"(?![A-Za-z])")


SUPERSAMPLE = 4  # typeset this many times larger, then scale down: smooth edges with the full 256 levels
SIZES = {"tiny": 0.6, "scriptsize": 0.7, "footnotesize": 0.8, "small": 0.9, "normalsize": 1.0, "large": 1.2,
         "Large": 1.44, "LARGE": 1.73, "huge": 2.07, "Huge": 2.49}  # LaTeX's own steps
SIZE_CMD = re.compile(r"^\s*\\(" + "|".join(SIZES) + r")(?![A-Za-z])\s*")


def size_of(tex):
    """A formula may start with a LaTeX size command (\large, \Huge, …): → (its factor, the formula without it)."""
    m = SIZE_CMD.match(tex)
    return (SIZES[m.group(1)], tex[m.end():]) if m else (1.0, tex)


def _latex_png(tex, dpi):
    """latex + dvipng in a scratch folder, no shell escape and paranoid file access; → black-on-white PNG bytes or
    None."""
    body = tex.strip()
    if UNSAFE.search(body):
        return None
    if ENVS.match(body):  # it's its own display environment: don't nest it in align*
        doc = DOC.replace("\\begin{align*}\n%s\n\\end{align*}", "%s") % body
    else:
        doc = DOC % (body if "&" in body or "\\\\" in body else "&" + body)
    env = {**os.environ, "openin_any": "p", "openout_any": "p", "shell_escape": "f"}
    with tempfile.TemporaryDirectory(prefix="cv-math-") as d:
        pathlib.Path(d, "m.tex").write_text(doc, encoding="utf-8")
        run = lambda *cmd: subprocess.run(cmd, cwd=d, env=env, capture_output=True, timeout=15)  # noqa: E731
        if run("latex", "-no-shell-escape", "-interaction=nonstopmode", "-halt-on-error", "m.tex").returncode:
            return None
        if run("dvipng", "-q", "-T", "tight", "-D", str(dpi), "-bg", "rgb 1 1 1", "-fg", "rgb 0 0 0", "--truecolor",
               "-o", "m.png", "m.dvi").returncode:
            return None
        p = pathlib.Path(d, "m.png")
        return p.read_bytes() if p.exists() else None


def _mathtext_png(tex, dpi):
    """matplotlib's mathtext (a subset of LaTeX, no environments); → black-on-white PNG bytes or None."""
    try:
        from matplotlib.figure import Figure
        from matplotlib.backends.backend_agg import FigureCanvasAgg
    except ImportError:
        return None
    lines = [ln.replace("&", "") for ln in re.split(r"\\\\", tex) if ln.strip()]
    try:
        fig = Figure(facecolor="white")
        FigureCanvasAgg(fig)
        n = len(lines)
        for k, ln in enumerate(lines):
            fig.text(0, 1 - (k + 0.5) / n, f"${ln.strip()}$", fontsize=12, color="black", va="center")
        fig.set_size_inches(0.01, 0.01)
        buf = io.BytesIO()
        fig.savefig(buf, dpi=dpi, facecolor="white", bbox_inches="tight", pad_inches=0.02, format="png")
        return buf.getvalue()
    except Exception:  # noqa: BLE001 - mathtext rejects what it doesn't support
        return None


def _mask(data):
    """Black-on-white PNG bytes at SUPERSAMPLE× → the coverage mask (L: 255 = ink) at the final size."""
    from PIL import Image, ImageOps
    big = ImageOps.invert(Image.open(io.BytesIO(data)).convert("L"))
    w, h = max(1, round(big.width / SUPERSAMPLE)), max(1, round(big.height / SUPERSAMPLE))
    small = big.resize((w, h), Image.Resampling.BOX)  # true coverage per pixel, no ringing
    return small.point(INK_CURVE)  # thin strokes keep their weight instead of turning pale grey


INK_GAMMA = 0.6  # below 1 thickens anti-aliased edges a little, like a terminal's own font rendering
INK_CURVE = [round(255 * (v / 255) ** INK_GAMMA) for v in range(256)]


def tint(mask, rgb):
    """The coverage mask → an RGBA image in colour rgb, transparent where there's no ink."""
    from PIL import Image
    img = Image.new("RGBA", mask.size, tuple(rgb) + (0,))
    img.putalpha(mask)
    return img


def fit_cells(img, cell_w, cell_h):
    """Pad the image (transparent) to whole terminal cells, centred, so the terminal draws it pixel for pixel; a
    stretch of a few percent to fill the last cell is what blurred it. → (image, (columns, rows))."""
    from PIL import Image
    cols, rows = -(-img.width // cell_w), -(-img.height // cell_h)
    out = Image.new("RGBA", (cols * cell_w, rows * cell_h), (0, 0, 0, 0))
    out.paste(img, ((out.width - img.width) // 2, (out.height - img.height) // 2))
    return out, (cols, rows)


CACHE_KEEP = 400  # typeset formulas kept on disk


def _prune(d):
    files = sorted(d.glob("*.png"), key=lambda f: f.stat().st_mtime)
    for f in files[:-CACHE_KEEP]:
        f.unlink(missing_ok=True)


def render(tex, rgb, cell_h=18, scale=1.0):
    """Display maths → an RGBA PIL image in colour rgb; None if it can't be made. At scale 1 its type is about as tall as
    the terminal's; scale (the window's zoom × behaviour.math_scale) and a leading \large / \Huge / … make it bigger.
    Safe to call from a thread."""
    which = engine()
    if which is None:
        return None
    factor, tex = size_of(tex)
    em = cell_h * 1.15 * scale * factor  # one em of the formula, in pixels
    dpi = max(30, round(em * 72.27 / 12)) * SUPERSAMPLE  # 12 pt type
    key = hashlib.sha256(f"v4|{which}|{dpi}|{tex.strip()}".encode()).hexdigest()[:24]
    try:
        path = _cache_dir() / f"{key}.png"
    except OSError:
        path = None
    if path and path.exists():
        from PIL import Image
        try:
            path.touch()
            return tint(Image.open(path).convert("L"), rgb)
        except OSError:
            pass
    data = (_latex_png if which == "latex" else _mathtext_png)(tex, dpi)
    if data is None and which == "latex" and not UNSAFE.search(tex):
        data = _mathtext_png(tex, dpi)
    if data is None:
        return None
    mask = _mask(data)
    if path:
        try:
            mask.save(path)
            _prune(path.parent)
        except OSError:
            pass
    return tint(mask, rgb)


# ---- terminal graphics ------------------------------------------------------------------------------------------
_GRAPHICS = None


def probe_graphics():
    """Find out once whether the terminal shows real images (kitty's graphics protocol, or sixel) and how big its cells
    are. It asks the terminal, so it must run before Textual takes over the screen (show_output.main does this).
    Without textual-image, or on a terminal with neither protocol, display maths stays Unicode text."""
    global _GRAPHICS
    if _GRAPHICS is not None:
        return _GRAPHICS or None
    _GRAPHICS = False
    if os.environ.get("CV_MATH_IMAGES", "1") == "0" or engine() is None:
        return None
    try:
        from textual_image import renderable
        from textual_image.widget import Image, get_cell_size
        if renderable.Image not in (renderable.TGPImage, renderable.SixelImage):
            return None
        cell = get_cell_size()
        _GRAPHICS = (Image, cell.width or 9, cell.height or 18)
    except Exception:  # noqa: BLE001 - no package, no tty, an odd terminal: Unicode it is
        return None
    return _GRAPHICS


def graphics():
    """(Image widget class, cell width px, cell height px) after probe_graphics() found a graphics terminal, else
    None."""
    return _GRAPHICS or None
