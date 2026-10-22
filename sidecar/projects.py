"""Find project folders by name, so the voice agent never has to ask for a path ("resume rmpc", "the luffy project").

A quick, bounded directory walk (no model involved): folders whose name matches the query, ranked by how well the
name matches and whether the folder looks like a real project (git, pyproject, package.json, CLAUDE.md, …).
"""

import os
import re
import time

import platform_util

HOME = os.path.expanduser("~")
# (root, max depth below it)
ROOTS = [(os.path.join(HOME, "src"), 2), (HOME, 3), (os.path.join(HOME, ".config"), 1)]
if platform_util.is_windows():
    ROOTS = ROOTS[:2]  # no ~/.config
SKIP = {"node_modules", ".venv", "venv", "__pycache__", ".git", ".cache", ".local", "target", "build", "dist",
        "Games", "Music", "Pictures", "Videos", ".steam", ".mozilla", ".npm", ".cargo", ".rustup", "go", "snap",
        ".nv", ".var", "site-packages"}
if platform_util.is_windows():
    SKIP |= {"AppData", "OneDrive"}  # app data and synced cloud folders: huge, never projects
MARKERS = {".git": 3, "CLAUDE.md": 2, "pyproject.toml": 2, "package.json": 2, "Cargo.toml": 2, "go.mod": 2,
           "Makefile": 1, "CMakeLists.txt": 2, "README.md": 1, "setup.py": 2, ".obsidian": 2}
TIME_LIMIT = 1.5  # seconds


def _norm(s):
    return re.sub(r"[^a-z0-9]", "", s.lower())


def _match(name, query):
    n, q = _norm(name), _norm(query)
    if not q:
        return 0
    if n == q:
        return 10
    if n.startswith(q) or q.startswith(n) and len(n) >= 3:
        return 6
    if q in n:
        return 4
    words = [_norm(w) for w in re.split(r"[\s_\-./]+", query) if len(w) >= 3]
    if words and all(w in n for w in words):
        return 3
    return 0


def find_projects(query, limit=6):
    """→ [{"path", "markers", "modified"}], best first."""
    found, seen = {}, set()
    deadline = time.monotonic() + TIME_LIMIT
    for root, max_depth in ROOTS:
        stack = [(root, 0)]
        while stack and time.monotonic() < deadline:
            path, depth = stack.pop()
            try:
                entries = list(os.scandir(path))
            except OSError:
                continue
            for e in entries:
                if not e.is_dir(follow_symlinks=False) or e.name in SKIP:
                    continue
                if e.name.startswith(".") and not (path == HOME and e.name == ".config"):
                    continue
                real = os.path.realpath(e.path)
                if real in seen:
                    continue
                seen.add(real)
                score = _match(e.name, query)
                if score:
                    try:
                        names = set(os.listdir(e.path))
                    except OSError:
                        names = set()
                    markers = [m for m in MARKERS if m in names]
                    found[real] = (score + sum(MARKERS[m] for m in markers) - depth * 0.5, e.path, markers)
                if depth < max_depth:
                    stack.append((e.path, depth + 1))
    best = sorted(found.values(), key=lambda x: -x[0])[:limit]
    out = []
    for _, path, markers in best:
        try:
            modified = time.strftime("%Y-%m-%d", time.localtime(os.path.getmtime(path)))
        except OSError:
            modified = "?"
        out.append({"path": path.replace(HOME, "~", 1), "markers": markers, "modified": modified})
    return out


REAL = {".git", "CLAUDE.md", "pyproject.toml", "package.json", "Cargo.toml", "go.mod", "CMakeLists.txt", "setup.py"}
STOP = {"the", "and", "for", "with", "that", "this", "project", "projects", "folder", "repo", "work", "want", "make",
        "task", "add", "fix", "can", "you", "please", "my", "into", "from", "short", "note", "new", "file", "files"}


def named_project(text):
    """A real project (git or project files) whose folder name the user said exactly, e.g. "my luffy project" →
    "~/luffy". Checks single words and two-word names ("claude voice" → ~/src/claude-voice). None if there isn't one."""
    words = [w for w in re.findall(r"[A-Za-z][\w.\-]{2,}", text) if w.lower() not in STOP]
    candidates = [f"{a} {b}" for a, b in zip(words, words[1:])] + sorted(set(words), key=len, reverse=True)
    for c in candidates[:12]:
        for hit in find_projects(c, limit=2):
            name = os.path.basename(hit["path"])
            if _norm(name) == _norm(c) and REAL & set(hit["markers"]):
                return hit["path"]
    return None
