# t3 · How cv hands work to the back end

*~/src/claude-voice · 19:30*

cv splits every request into a **voice** layer and a **work** layer. Haiku talks; Claude Code does the work, and the full answer lands here. See [the Textual docs](https://textual.textualize.io/) for the widget model, and `agents.py` for the routing.

## The flow

```diagram
  ╭──────────╮   speech    ╭───────────╮  propose_task  ╭──────────────────╮   briefs   ╭──────────────────────────╮
  │   mic    │ ──────────▶ │   Haiku   │ ─────────────▶ │   orchestrator   │ ─────────▶ │  scout · builder · review │
  │ Whisper  │             │ front end │ ◀───────────── │   (per project)  │ ◀───────── │  one builder at a time    │
  ╰──────────╯             ╰─────┬─────╯     result     ╰────────┬─────────╯   reports  ╰──────────────────────────╯
                                 │ gist                          │ full answer
                                 ▼                               ▼
                           ╭───────────╮                 ╭───────────────╮
                           │  Kokoro   │                 │ output window │
                           ╰───────────╯                 ╰───────────────╯
```

## Where the time goes

```diagram
  hearing you    ▇▇▇▇▇▇               2.3 s
  Haiku reply    ▇▇▇▇▇▇▇▇             3.1 s
  first audio    ▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇  7.1 s
```

### The pieces

| piece | model | runs on | latency |
|:--|:--|:-:|--:|
| speech → text | distil-large-v3 | GPU | 2.3 s |
| front end | Haiku | API | 3.1 s |
| voice | Kokoro `af_bella` | CPU | 0.4 s |

1. Haiku routes the request: *quick* or *job*.
2. A job's plan comes back for approval.
    - each piece is built, then reviewed
    - every step is checked first
        - against the plan
        - against the done list
3. The report opens here.

- [x] output window
- [x] session resume
- [ ] voice settings screen

> [!NOTE]
> Tasks stay open after each round, so follow-ups keep their context.

> [!WARNING]
> Builders never run in parallel: two edits to one file would conflict.

> A plain quote, for contrast: the orb breathes even when nothing happens.

The restart check is one line in `scripts/cv`:

```bash
while true; do
    run tui.py "$@"
    code=$?
    [ "$code" -eq 75 ] || exit "$code"
    run restart_job.py || echo "  (restart job failed; starting Claude Voice anyway)"
done
```

```python
def gradient(text, a, b, style=""):
    """Text whose colour runs from a to b across its characters."""
    out = Text()
    for i, ch in enumerate(text):
        out.append(ch, mix(a, b, i / max(len(text) - 1, 1)))
    return out
```

---

That's the whole path, ~~roughly~~ exactly as built today.
