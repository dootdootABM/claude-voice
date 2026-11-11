"""propose_task's backend_model clean-up, without models: pick_backend_model only.

Run: pytest test_backend_model.py
"""

import agents


def pick(model, route="quick", said="fix the flaky test"):
    args = {"route": route}
    if model is not None:
        args["backend_model"] = model
    note = agents.pick_backend_model(args, said, "sonnet")
    return args.get("backend_model"), note


def test_omitted_stays_omitted():
    assert pick(None) == (None, "")


def test_aliases_pass_through():
    for m in agents.BACKEND_MODELS:
        assert pick(m) == (m, "")


def test_stray_spellings_become_aliases():
    assert pick("Opus 4")[0] == "opus"
    assert pick("claude-sonnet-5-5")[0] == "sonnet"


def test_unknown_model_falls_back_to_default():
    model, note = pick("gpt-5")
    assert model is None and "using the default" in note


def test_job_on_haiku_goes_to_job_default():
    model, note = pick("haiku", route="job")
    assert model == "sonnet" and "unless the user asks" in note


def test_job_on_haiku_when_user_asked():
    assert pick("haiku", route="job", said="run it as a job with Haiku") == ("haiku", "")


def test_schema_and_prompt_explain_the_choice():
    desc = agents.TASK_SCHEMA["properties"]["backend_model"]["description"]
    assert "opus" in desc and "never a job" in desc
    import default_prompts
    assert "Back-end model (backend_model on propose_task)" in default_prompts.PROMPTS["front_agent"]


def test_fable_can_be_chosen():
    assert "fable" in agents.TASK_SCHEMA["properties"]["backend_model"]["enum"]
    assert pick("Fable 5.2") == ("fable", "")
    assert pick("claude-fable-5-2")[0] == "fable"
