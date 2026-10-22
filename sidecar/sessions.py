"""Earlier Claude Code conversations in a project folder, so work picks up an old chat instead of starting from zero.

Claude Code keeps every session as ~/.claude/projects/<folder with / → ->/<session id>.jsonl: the ones cv started and
the user's own from a terminal. A quick scan (no model involved) reads each file's title (the user's /rename, cv's
--name, or Claude Code's own ai-title), its first and latest prompts and how long it ran, and ranks them against what
the user just said. Files can be several MB, so only lines that carry those fields are parsed, and results are cached
by modification time.
"""

import json
import os
import re
import time

PROJECTS = os.path.expanduser("~/.claude/projects")
KEEP_DAYS = 60  # older conversations are left out
LONG = 150_000  # tokens: worth offering a fresh start (agents.CONTEXT_NUDGES)
BUSY_S = 600  # modified this recently: probably still open somewhere, so resuming it forks a copy
STOP = {"the", "and", "for", "with", "that", "this", "what", "was", "were", "about", "again", "back", "pick", "up",
        "continue", "resume", "where", "left", "off", "earlier", "last", "time", "chat", "session", "work", "can",
        "you", "your", "me", "my", "let", "lets", "let's", "on", "we", "our", "project", "it", "into", "from", "go"}
_cache = {}  # path → (mtime, info)


def slug(path):
    """A path as a folder name: its real location with every non-alphanumeric character turned into -."""
    return re.sub(r"[^A-Za-z0-9]", "-", os.path.realpath(os.path.expanduser(path)))


def folder(path):
    """The folder Claude Code stores a project's sessions in."""
    return os.path.join(PROJECTS, slug(path))


def context_tokens(u):
    """How much context a usage dict says the conversation carries."""
    return u.get("input_tokens", 0) + u.get("cache_read_input_tokens", 0) + u.get("cache_creation_input_tokens", 0)


def _scan(path):
    titles, ai_titles, prompts, last = [], [], [], ""
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            if '"type":"custom-title"' in line:
                titles.append(json.loads(line).get("customTitle", ""))
            elif '"type":"ai-title"' in line:
                ai_titles.append(json.loads(line).get("aiTitle", ""))
            elif '"type":"last-prompt"' in line:
                p = json.loads(line).get("lastPrompt", "")
                if p and not p.startswith("<") and (not prompts or prompts[-1] != p):
                    prompts.append(p)
            elif '"type":"assistant"' in line and '"isSidechain":true' not in line:
                last = line  # parsed once at the end: its usage is how much context the conversation carries
    title = (titles or ai_titles or [""])[-1].strip()
    u = json.loads(last).get("message", {}).get("usage", {}) if last else {}
    context = context_tokens(u)
    return {"title": title, "first": prompts[0] if prompts else "", "latest": prompts[-1] if prompts else "",
            "turns": len(prompts), "context": context, "words": _words(f"{title} {title} " + " ".join(p[:300] for p in prompts))}


def _words(text):
    return {w for w in re.findall(r"[a-z0-9][a-z0-9+#.\-]{2,}", text.lower()) if w not in STOP}


def sessions(project_dir, query="", limit=6, exclude=()):
    """→ [{"id", "title", "first", "latest", "turns", "context", "modified", "busy", "score"}], best match first, then newest.
    Conversations with no real prompt (a single command, an empty start) are left out."""
    d = folder(project_dir)
    try:
        entries = [e for e in os.scandir(d) if e.name.endswith(".jsonl")]
    except OSError:
        return []
    cutoff, now = time.time() - KEEP_DAYS * 86400, time.time()
    q = _words(query)
    out = []
    for e in entries:
        sid = e.name[:-6]
        mtime = e.stat().st_mtime
        if sid in exclude or mtime < cutoff:
            continue
        cached = _cache.get(e.path)
        if not cached or cached[0] != mtime:
            try:
                cached = _cache[e.path] = (mtime, _scan(e.path))
            except (OSError, ValueError):
                continue
        info = cached[1]
        if not info["turns"]:
            continue
        out.append({"id": sid, **{k: v for k, v in info.items() if k != "words"}, "modified": mtime,
                    "busy": now - mtime < BUSY_S, "score": len(q & info["words"])})
    out.sort(key=lambda s: (-s["score"], -s["modified"]))
    return out[:limit]


def describe(s):
    """One line for Haiku: what it was about and when."""
    when = time.strftime("%b %d %H:%M", time.localtime(s["modified"]))
    about = s["title"] or s["first"][:80]
    latest = f"; last asked: {s['latest'][:100]!r}" if s["latest"] and s["latest"] != s["first"] else ""
    busy = " (active in the last few minutes, maybe open elsewhere)" if s["busy"] else ""
    size = f", {s['context'] // 1000}k tokens" + (" (long)" if s["context"] >= LONG else "") if s["context"] else ""
    return f"- session {s['id']}: {about!r}, {when}, {s['turns']} prompts{size}{latest}{busy}"
