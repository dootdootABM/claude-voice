"""LaTeX maths in the output windows: Unicode text, the safety filter, the Markdown parser and the image renderer."""
import shutil

import pytest

import mathtex
import show_output


@pytest.mark.parametrize("tex, want", [
    (r"E=mc^2", "E=mc²"),
    (r"x_i^2 + y_{ij}", "xᵢ² + yᵢⱼ"),
    (r"\frac{a+b}{2}", "(a+b)/2"),
    (r"\frac{1}{n}\sum_{i=1}^{n} x_i", "1/n ∑ᵢ₌₁ⁿ xᵢ"),
    (r"\sqrt{x^2+y^2}", "√(x²+y²)"),
    (r"\int_0^\infty e^{-x^2}\,dx = \frac{\sqrt{\pi}}{2}", "∫₀^∞ e^(-x²) dx = √π/2"),
    (r"\mathbb{R}^n \to \mathbb{R}", "ℝⁿ → ℝ"),
    (r"\sqrt[3]{8}=2", "∛8=2"),
    (r"\nabla \cdot \vec{E} = \frac{\rho}{\varepsilon_0}", "∇ · E⃗ = ρ/ε₀"),
    (r"\begin{pmatrix} a & b \\ c & d \end{pmatrix}", "(a b; c d)"),
    (r"\lim_{n\to\infty} \left(1+\frac{1}{n}\right)^n = e", "lim_(n→∞) (1+1/n)ⁿ = e"),
    (r"\text{if } x \ge 0", "if x ≥ 0"),
    (r"\unknownmacro{x}", "unknownmacrox"),
])
def test_to_unicode(tex, want):
    assert mathtex.to_unicode(tex) == want


def test_to_unicode_never_raises_on_broken_tex():
    for tex in (r"\frac{", "}", r"x^", r"\begin{matrix} a &", "\\"):
        assert isinstance(mathtex.to_unicode(tex), str)


@pytest.mark.parametrize("tex", [r"\input{/etc/passwd}", r"^^5cinput{/etc/passwd}", r"\catcode`\|=0",
                                 r"\csname input\endcsname{x}", r"\def\x{y}", r"\write18{ls}", r"\immediate",
                                 r"\usepackage{foo}", r"\makeatletter\@input", r"\end{document}"])
def test_unsafe_tex_never_reaches_latex(tex, monkeypatch):
    assert mathtex.UNSAFE.search(tex)
    monkeypatch.setattr(mathtex.subprocess, "run", lambda *a, **k: pytest.fail("latex ran"))
    assert mathtex._latex_png(tex, 100) is None


@pytest.mark.parametrize("tex", [r"\frac{a}{b}", r"\int_0^1 f(x)\,dx", r"\operatorname{softmax}(z)_i",
                                 r"\mathbf{x}\cdot\mathbf{y}", r"\text{bold}\ x", r"\begin{aligned}a&=b\end{aligned}"])
def test_ordinary_maths_is_not_flagged(tex):
    assert not mathtex.UNSAFE.search(tex)


def _inline_math(src):
    toks = show_output.markdown().parse(src)
    return [c.content for t in toks for c in (t.children or []) if c.type in show_output.MATH_INLINE]


def test_parser_finds_inline_maths_but_not_prices():
    assert _inline_math(r"Energy $E=mc^2$ and \(a+b\).") == ["E=mc^2", "a+b"]
    assert _inline_math("It costs $5 and $10 a month.") == []
    assert _inline_math("from $ 5 to $ 10") == []


def test_display_maths_nodes():
    src = "$$\n\\int_0^1 x\\,dx\n$$\n\n\\[ \\frac{a}{b} \\]\n\n```math\nx^2\n```\n\n```latex\n\\section{Hi}\n```\n"
    nodes = show_output.tree(show_output.markdown().parse(src))
    assert [show_output.math_source(n) for n in nodes] == [r"\int_0^1 x\,dx", r"\frac{a}{b}", "x^2", None]


def test_block_widget_for_display_maths_is_a_mathblock():
    pal = show_output.Palette(show_output.Theme())
    r = show_output.Renderer(pal)
    nodes = show_output.tree(show_output.markdown().parse("$$\nx^2\n$$\n\n```latex\nx\n```\n"))
    maths, code = (show_output.block_widget(n, r, pal) for n in nodes)
    assert isinstance(maths, show_output.MathBlock) and maths.tex == "x^2" and maths.text.plain == "x²"
    assert isinstance(code, show_output.CodeBlock)  # a ```latex fence is LaTeX source to read, not a formula


def test_inline_maths_is_drawn_as_unicode():
    pal = show_output.Palette(show_output.Theme())
    node = show_output.tree(show_output.markdown().parse(r"so $\alpha^2$ holds"))[0]
    assert show_output.Renderer(pal).block(node).plain == "so α² holds"


def test_no_images_without_a_graphics_terminal():
    assert mathtex.probe_graphics() is None  # pytest has no tty: display maths stays Unicode text


@pytest.mark.skipif(not (shutil.which("latex") and shutil.which("dvipng")), reason="no LaTeX installed")
def test_render_with_latex_is_cached(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_VOICE_STATE_DIR", str(tmp_path))
    img = mathtex.render(r"\begin{pmatrix}1&2\\3&4\end{pmatrix}", (200, 180, 160), 20)
    assert img is not None and img.width > 20 and img.height > 20
    assert len(list((tmp_path / "cache" / "math").glob("*.png"))) == 1
    monkeypatch.setattr(mathtex.subprocess, "run", lambda *a, **k: pytest.fail("not cached"))
    assert mathtex.render(r"\begin{pmatrix}1&2\\3&4\end{pmatrix}", (200, 180, 160), 20).size == img.size


def test_render_refuses_unsafe_and_broken_tex(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_VOICE_STATE_DIR", str(tmp_path))
    assert mathtex.render(r"\input{/etc/passwd}", (0, 0, 0), 20) is None
    if mathtex.engine() == "latex":
        assert mathtex.render(r"\frac{", (0, 0, 0), 20) is None


def test_size_commands():
    assert mathtex.size_of(r"\Large x^2") == (1.44, "x^2")
    assert mathtex.size_of(r"  \Huge\frac{a}{b}") == (2.49, r"\frac{a}{b}")
    assert mathtex.size_of(r"\largex") == (1.0, r"\largex")  # not a size command
    assert mathtex.to_unicode(r"\Large x^2") == "x²"


def test_fit_cells_pads_to_whole_cells_centred():
    from PIL import Image
    img = Image.new("RGBA", (35, 52), (255, 0, 0, 255))
    out, cells = mathtex.fit_cells(img, 9, 20)
    assert cells == (4, 3) and out.size == (36, 60)
    assert out.getpixel((0, 0))[3] == 0 and out.getpixel((18, 30))[3] == 255  # transparent border, image centred


def test_tint_keeps_the_mask_as_alpha():
    from PIL import Image
    mask = Image.frombytes("L", (64, 1), bytes(range(0, 256, 4)))
    img = mathtex.tint(mask, (10, 20, 30))
    assert img.mode == "RGBA" and img.getpixel((0, 0))[:3] == (10, 20, 30)
    assert len(set(img.getchannel("A").tobytes())) > 50  # smooth, not a 16-level palette


@pytest.mark.skipif(not (shutil.which("latex") and shutil.which("dvipng")), reason="no LaTeX installed")
def test_render_is_smooth_and_scales(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_VOICE_STATE_DIR", str(tmp_path))
    small = mathtex.render(r"\frac{\sqrt{\pi}}{2}", (200, 180, 160), 20)
    alphas = set(small.getchannel("A").tobytes())
    assert small.mode == "RGBA" and len(alphas) > 64  # anti-aliased edges, not posterised
    big = mathtex.render(r"\Large\frac{\sqrt{\pi}}{2}", (200, 180, 160), 20)
    zoomed = mathtex.render(r"\frac{\sqrt{\pi}}{2}", (200, 180, 160), 20, scale=1.44)
    assert big.height > small.height * 1.3 and abs(big.height - zoomed.height) <= 2
    recoloured = mathtex.render(r"\frac{\sqrt{\pi}}{2}", (1, 2, 3), 20)  # from the cache, in a new colour
    assert recoloured.size == small.size and recoloured.getchannel("R").getextrema()[1] == 1


def test_math_scale_config_is_checked():
    import config
    with pytest.raises(config.ConfigError, match="math_scale"):
        config.parse("behaviour:\n  math_scale: 5\n", voices=None)
    assert config.parse("behaviour:\n  math_scale: 1.5\n", voices=None)["behaviour"]["math_scale"] == 1.5
