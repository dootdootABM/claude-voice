"""Stricter routing in Front._continue, without models: big work never goes to a quick task.

Run: pytest test_routing.py
"""

import asyncio
import types

import agents

INCIDENT = ("Look in ~/src for the Claude Voice changes markdown file, read the prompt in it, and carry out "
            "those changes")


def front(kind):
    task = types.SimpleNamespace(id="t7", live_key="t7", kind=kind, open=True, state="idle", active=False, context=0, backend=None,
                                 last_asked="", digest=[], budget=10.0, budget_warned=False)
    sent = []

    async def send(msg):
        sent.append(msg)

    task.backend = types.SimpleNamespace(proc=types.SimpleNamespace(send=send), running_workers=lambda: [])
    f = object.__new__(agents.Front)
    f.last_user_text, f.user_turns = "please do it", 1
    f.app = types.SimpleNamespace(tasks={"t7": task}, decisions=[],
                                  ui=types.SimpleNamespace(status=lambda m: None, dim=lambda m: None))
    return f, sent


def run(f, **args):
    base = {"task_id": "t7", "same_topic": True, "size": "small", "user_quote": "please do it"}
    return asyncio.run(f._continue({**base, **args}))


def test_schema_requires_size():
    assert "size" in agents.CONTINUE_SCHEMA["required"]
    assert agents.CONTINUE_SCHEMA["properties"]["size"]["enum"] == ["small", "large"]


def test_large_request_to_quick_task_is_refused():
    f, sent = front("quick")
    out = run(f, message="add three features across the app", size="large")
    assert out == agents.BIG_WORK_REFUSAL.format(id="t7") and out.startswith("Not sent: this is new multi-part work")
    assert "propose_task with route job" in out and "task t7's last result" in out
    assert not sent


def test_small_follow_up_goes_through():
    f, sent = front("quick")
    assert run(f, message="why 75 though?").startswith("Continuing that work")
    assert len(sent) == 1


def test_execute_file_message_to_quick_task_is_refused():
    f, sent = front("quick")
    assert run(f, message=INCIDENT).startswith("Not sent: this is new multi-part work")
    assert not sent


def test_same_message_to_a_job_is_allowed():
    f, sent = front("job")
    assert run(f, message=INCIDENT, size="large").startswith("Continuing that work")
    assert len(sent) == 1


def test_reading_a_file_to_answer_passes():
    f, sent = front("quick")
    assert run(f, message="just read the file notes.md and tell me what it says").startswith("Continuing")
    assert len(sent) == 1


def test_different_topic_still_refused():
    f, sent = front("quick")
    assert "different topic" in run(f, message="now explain the orb", same_topic=False)
    assert not sent


def test_small_work_that_mentions_a_file_or_brief_passes():
    for msg in ["please apply the fix, then update the README.md", "execute the tests in test.txt",
                "follow up on the brief", "I read the brief, now implement it",
                "read notes.txt and tell me what it says", "update the README.md"]:
        assert not agents.asks_to_carry_out_file(msg), msg
        f, sent = front("quick")
        assert run(f, message=msg).startswith("Continuing"), msg
        assert len(sent) == 1


def test_carry_out_file_messages_are_refused():
    for msg in [INCIDENT, "read changes.md and carry out those changes", "open brief.md and implement everything in it"]:
        assert agents.asks_to_carry_out_file(msg), msg
        f, sent = front("quick")
        assert run(f, message=msg).startswith("Not sent: this is new multi-part work"), msg
        assert not sent


def test_resume_of_quick_task_is_not_refused():
    f, sent = front("quick")
    f.app.tasks["t7"].session_id = "s1"
    out = asyncio.run(f._resume({"session_id": "s1", "user_quote": "please do it", "message": "follow the plan in notes.md",
                                 "size": "large"}))
    assert out.startswith("Continuing"), out
    assert len(sent) == 1


def test_new_project_gets_its_folder(tmp_path, monkeypatch):
    import config
    projects = tmp_path / "src"  # missing too, as ~/src often is on a fresh machine
    monkeypatch.setattr(config, "_current", {"user": {"project_dirs": [str(projects), str(tmp_path)]}})
    assert agents.make_new_project(str(projects / "textstats"))
    assert (projects / "textstats").is_dir()
    assert not agents.make_new_project(str(projects / "textstats"))  # already there: nothing made
    assert not agents.make_new_project(str(projects / "a" / "b"))  # only a folder right inside the projects folder
    assert not agents.make_new_project(str(tmp_path / "elsewhere"))  # a mistyped path still fails
    assert not (projects / "a").exists() and not (tmp_path / "elsewhere").exists()
