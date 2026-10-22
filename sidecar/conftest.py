"""pytest setup shared by all tests: nothing here may touch the real saved state, the sound card or the clipboard."""

import os
import sys
import tempfile
from pathlib import Path

# Before any module that derives paths from it (agents.RESTART_FILE, tui logs, saved tasks) is imported.
os.environ["CLAUDE_VOICE_STATE_DIR"] = tempfile.mkdtemp(prefix="cv-test-state-")
# Never read (or create) the real config file: the config tests set what they need.
os.environ["CLAUDE_VOICE_CONFIG"] = os.path.join(tempfile.mkdtemp(prefix="cv-test-config-"), "config.yaml")
sys.path.insert(0, str(Path(__file__).resolve().parent))


def pytest_collection_modifyitems(config, items):
    import pytest

    if sys.platform.startswith("linux"):
        return
    skip = pytest.mark.skip(reason="Linux only")
    for item in items:
        if "linux_only" in item.keywords:
            item.add_marker(skip)
