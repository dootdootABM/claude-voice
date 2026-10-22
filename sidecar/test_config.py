"""config.yaml: loading, merge order, validation messages, prompts reaching the front end's command line, voices.
No models, audio or Claude needed. Run: pytest test_config.py"""

import os
import subprocess
import sys
import textwrap

import pytest

import agents
import config
import engine
import platform_util as pu

VOICES = ["af_bella", "af_heart", "am_adam", "bf_emma", "bm_george", "jf_alpha"]


@pytest.fixture
def cfg_file(tmp_path, monkeypatch):
    """Point CLAUDE_VOICE_CONFIG at a fresh path and load nothing from the cache."""
    path = tmp_path / "cv" / "config.yaml"
    monkeypatch.setenv("CLAUDE_VOICE_CONFIG", str(path))
    monkeypatch.setattr(config, "_current", None)
    yield path
    monkeypatch.setattr(config, "_current", None)


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(text), encoding="utf-8")


def parse(text, voices=VOICES):
    return config.parse(textwrap.dedent(text), label="config.yaml", voices=voices)


# ---- location -----------------------------------------------------------------------------------------------
def test_config_path_override_and_platform_defaults(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_VOICE_CONFIG", str(tmp_path / "x.yaml"))
    assert pu.config_path() == str(tmp_path / "x.yaml")
    monkeypatch.setenv("CLAUDE_VOICE_CONFIG", str(tmp_path))  # a folder: config.yaml inside it
    assert pu.config_path() == str(tmp_path / "config.yaml")
    monkeypatch.delenv("CLAUDE_VOICE_CONFIG")
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    home = os.path.expanduser("~")
    monkeypatch.setattr(pu, "is_windows", lambda: False)
    monkeypatch.setattr(sys, "platform", "linux")
    assert pu.config_dir() == os.path.join(home, ".config", "claude-voice")
    monkeypatch.setattr(sys, "platform", "darwin")
    assert pu.config_dir() == os.path.join(home, "Library", "Application Support", "claude-voice")
    monkeypatch.setattr(pu, "is_windows", lambda: True)
    monkeypatch.setenv("APPDATA", str(tmp_path / "roaming"))
    assert pu.config_dir() == os.path.join(str(tmp_path / "roaming"), "claude-voice")


# ---- first run, defaults, merge -------------------------------------------------------------------------------
def test_first_run_copies_the_example_once(cfg_file, capsys):
    assert not cfg_file.exists()
    cfg = config.get()
    assert cfg.first_run and cfg_file.exists()
    assert cfg_file.read_text(encoding="utf-8") == config.EXAMPLE.read_text(encoding="utf-8")
    assert "first run" in capsys.readouterr().err
    config.set_current(None)
    again = config.load()
    assert not again.first_run  # said once


def test_example_matches_the_built_in_defaults():
    assert config.EXAMPLE.read_text(encoding="utf-8") == config.example_text(), \
        "run `python config.py --write-example` in sidecar/"
    cfg = config.parse(config.EXAMPLE.read_text(encoding="utf-8"), voices=VOICES)
    assert cfg.data == config.DEFAULTS and cfg.warnings == []
    for key in config.PROMPTS:  # every prompt, byte for byte (blank lines and trailing spaces included)
        assert cfg["prompts"][key] == config.PROMPTS[key]


def test_example_has_no_personal_details():
    """The example's user section is a neutral sample; test_release.py checks the repo against your real config."""
    text = config.EXAMPLE.read_text(encoding="utf-8") + "".join(config.PROMPTS.values())
    assert "/home/" not in text and config.DEFAULTS["user"]["name"] == "Alex"


def test_partial_file_keeps_defaults_and_prompt_replaces_whole(cfg_file):
    write(cfg_file, """
        voice:
          speed: 1.2
        prompts:
          go_note: "Just do it."
        """)
    cfg = config.load()
    assert cfg["voice"]["speed"] == 1.2 and cfg["voice"]["name"] == "af_bella"
    assert cfg["prompts"]["go_note"] == "Just do it."  # replaced, not merged
    assert cfg["prompts"]["show_note"] == config.PROMPTS["show_note"]


def test_null_prompt_and_empty_sections_keep_defaults():
    cfg = parse("prompts:\n  go_note:\nvoice:\n")
    assert cfg["prompts"]["go_note"] == config.PROMPTS["go_note"] and cfg["voice"]["name"] == "af_bella"
    assert parse("").data == config.DEFAULTS


def test_merge_order_defaults_file_flags(cfg_file):
    write(cfg_file, "behaviour:\n  verbosity: chatty\n  half_duplex: true\nmodels:\n  front: sonnet\nstt:\n"
                    "  device: cpu\n  model: tiny.en\n  fast_model: false\n")
    config.set_current(None)
    o = engine.make_parser().parse_args([])
    assert (o.verbosity, o.front_model, o.device, o.stt_model, o.fast_stt, o.half_duplex) == \
        ("chatty", "sonnet", "cpu", "tiny.en", False, True)
    o = engine.make_parser().parse_args(["--verbosity", "quiet", "--front-model", "haiku", "--device", "cuda",
                                         "--stt-model", "small.en", "--fast-stt"])
    assert (o.verbosity, o.front_model, o.device, o.stt_model, o.fast_stt) == ("quiet", "haiku", "cuda", "small.en", True)
    write(cfg_file, "")  # no settings in the file: the built-in defaults
    config.set_current(None)
    o = engine.make_parser().parse_args([])
    assert (o.verbosity, o.front_model, o.device, o.stt_model, o.fast_stt, o.half_duplex) == \
        ("normal", "haiku", "auto", None, True, False)


def test_task_model_defaults_come_from_models_section(cfg_file):
    write(cfg_file, "models:\n  backend: opus\n  job: haiku\n")
    config.set_current(None)
    quick = agents.Task("t1", {"title": "a", "prompt": "p", "project_dir": "~"})
    job = agents.Task("t2", {"title": "a", "prompt": "p", "project_dir": "~", "route": "job"})
    assert (quick.model, job.model) == ("opus", "haiku")
    assert agents.Task("t3", {"title": "a", "prompt": "p", "backend_model": "sonnet"}).model == "sonnet"


# ---- errors and warnings --------------------------------------------------------------------------------------
def test_broken_yaml_is_a_readable_error_not_a_traceback(cfg_file):
    write(cfg_file, "voice:\n  name: [af_bella\n  speed: 1\n")
    with pytest.raises(config.ConfigError) as e:
        config.load()
    msg = str(e.value)
    assert "not valid YAML" in msg and "line" in msg and str(cfg_file) in msg


def test_cli_prints_config_error_and_exits_2(cfg_file, capsys):
    write(cfg_file, "voice: {name: [\n")
    with pytest.raises(SystemExit) as e:
        engine.parse_args([])
    assert e.value.code == 2
    err = capsys.readouterr().err
    assert "config error" in err and "Traceback" not in err


def test_bad_value_names_key_and_line():
    with pytest.raises(config.ConfigError) as e:
        parse("""
            behaviour:
              verbosity: loud
            voice:
              speed: 3
            """)
    msg = str(e.value)
    assert "line 3: behaviour.verbosity: 'loud' is not one of quiet | normal | chatty" in msg
    assert "line 5: voice.speed: 3 is outside 0.5 to 2.0" in msg


def test_wrong_types_are_reported():
    with pytest.raises(config.ConfigError) as e:
        parse("user:\n  load_global_claude_md: maybe\n  project_dirs: 5\nvoice:\n  blend: 3\nmodels:\n  front: ''\n")
    msg = str(e.value)
    for key in ("user.load_global_claude_md", "user.project_dirs", "voice.blend", "models.front"):
        assert key in msg
    with pytest.raises(config.ConfigError, match="mapping"):
        parse("- a\n- b\n")
    with pytest.raises(config.ConfigError, match="line 1: voice"):
        parse("voice: loud\n")


def test_unknown_keys_warn_but_load():
    cfg = parse("""
        voice:
          nmae: af_bella
        colour: blue
        prompts:
          frontagent: hi
        """)
    text = "\n".join(cfg.warnings)
    assert "voice.nmae: unknown key" in text and "colour: unknown section" in text and "prompts.frontagent" in text
    assert "line 3" in text and cfg["voice"]["name"] == "af_bella"


def test_missing_pyyaml_falls_back_to_defaults(cfg_file, monkeypatch):
    write(cfg_file, "voice:\n  speed: 1.3\n")
    monkeypatch.setattr(config, "_yaml", lambda: None)
    cfg = config.load()
    assert cfg["voice"]["speed"] == 1.05 and "PyYAML is not installed" in cfg.warnings[0]


# ---- placeholders ---------------------------------------------------------------------------------------------
def test_required_placeholders_are_checked_at_load():
    with pytest.raises(config.ConfigError) as e:
        parse("prompts:\n  handover_start: 'Carry on.'\n  handover_job: 'Carry on {note}'\n")
    assert "line 2: prompts.handover_start: must contain the placeholder {project}" in str(e.value)
    assert "handover_job" not in str(e.value)  # that one has {note}
    with pytest.raises(config.ConfigError, match="prompts.handover_job"):
        parse("prompts:\n  handover_job: 'no note here'\n")
    with pytest.raises(config.ConfigError, match="whole prompt"):
        parse("prompts:\n  narration: ''\n")


def test_unknown_placeholder_warns():
    cfg = parse("prompts:\n  go_note: 'Hi {nmae}'\n")
    assert any("{nmae}" in w for w in cfg.warnings)


def test_placeholders_filled_from_user_section_and_braces_left_alone():
    cfg = parse("""
        user:
          name: Sam
          system: Mac mini
          project_dirs: ["~/code", "~/work"]
          notes_dir: ~/notes
          preferences: Terse.
        prompts:
          front_context: "{name}|{system}|{project_dirs}|{new_project_dir}|{notes_line}|{preferences}"
          step_check: 'Reply {"allow": true} for {name}'
          handover_start: "In {project}: {note}"
        """)
    assert cfg.prompt("front_context") == "Sam|Mac mini|~/code and ~/work|~/code| Notes are in ~/notes.|Terse."
    assert cfg.prompt("step_check") == 'Reply {"allow": true} for Sam'
    assert cfg.prompt("handover_start", project="/p", note="{name}") == "In /p: {name}"  # not filled twice
    assert config.fill("a {x} {y}", {"x": 1}) == "a 1 {y}"
    assert parse("user:\n  notes_dir: ''\n").values()["notes_line"] == ""


# ---- prompts reach the front end's command line ---------------------------------------------------------------
def front_cmdline(cfg_file, text):
    write(cfg_file, text)
    config.set_current(None)
    app = type("StubApp", (), {"ui": None, "tts": None})()
    front = agents.Front.__new__(agents.Front)
    front.app = app
    args = agents.front_args("haiku")
    return args, args[args.index("--system-prompt-file") + 1]


def test_replaced_prompts_reach_the_front_command_line(cfg_file):
    args, prompt_file = front_cmdline(cfg_file, """
        user:
          name: Sam
          load_global_claude_md: false
        prompts:
          front_agent: |-
            You are a pirate. Say arr.
          front_context: "Captain {name} sails the {system}."
        """)
    with open(prompt_file, encoding="utf-8") as f:
        assert f.read().strip() == "You are a pirate. Say arr."
    assert args[args.index("--append-system-prompt") + 1].startswith("Captain Sam sails the ")
    assert "global instructions" not in args[args.index("--append-system-prompt") + 1]
    assert args[args.index("--model") + 1] == "haiku"


def test_front_class_uses_front_args(cfg_file, monkeypatch):
    write(cfg_file, "prompts:\n  front_agent: Custom front agent text.\n")
    config.set_current(None)
    seen = {}

    class Proc:
        def __init__(self, args, **kw):
            seen["args"] = args

    monkeypatch.setattr(agents, "ClaudeProc", Proc)

    class App:
        pass

    agents.Front(App(), "sonnet")
    with open(seen["args"][seen["args"].index("--system-prompt-file") + 1], encoding="utf-8") as f:
        assert "Custom front agent text." in f.read()
    assert seen["args"][1] == "sonnet"


def test_global_claude_md_is_loaded_by_default(cfg_file, monkeypatch, tmp_path):
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "CLAUDE.md").write_text("MY GLOBAL RULES", encoding="utf-8")
    monkeypatch.setattr(agents.pathlib.Path, "home", classmethod(lambda cls: tmp_path))
    write(cfg_file, "")
    config.set_current(None)
    assert "MY GLOBAL RULES" in agents.front_context()
    write(cfg_file, "user:\n  load_global_claude_md: false\n")
    config.set_current(None)
    assert "MY GLOBAL RULES" not in agents.front_context()


def test_other_prompts_are_taken_from_config(cfg_file):
    write(cfg_file, """
        prompts:
          narration: "Narrate in {app_dir}."
          go_note: "GO"
          show_note: "SHOW"
          short_ack: "ACK"
          handover_start: "H {project} {note}"
        """)
    config.set_current(None)
    assert agents.prompt_note("go_note") == "\n\nGO" and agents.prompt_note("show_note") == "\n\nSHOW"
    assert agents.Backend.system_prompt.fget(None).startswith("Narrate in ") and "{app_dir}" not in agents.Backend.system_prompt.fget(None)
    import orchestrator
    assert orchestrator.JobBackend.system_prompt.fget(None) == config.PROMPTS["orchestrator"]
    assert config.prompt("handover_start", project="P", note="N") == "H P N"


# ---- voices ---------------------------------------------------------------------------------------------------
def test_did_you_mean_voice():
    with pytest.raises(config.ConfigError) as e:
        parse("voice:\n  name: af_bela\n")
    assert "line 2: voice.name: 'af_bela' is not a Kokoro voice; did you mean 'af_bella'?" in str(e.value)
    with pytest.raises(config.ConfigError, match=r"cv --list-voices"):
        parse("voice:\n  name: zzzz\n")
    parse("voice:\n  name: af_bela\n", voices=None)  # no voices file (CI): nothing to check against


def test_blend_validation_and_spec():
    cfg = parse("voice:\n  name: am_adam\n  blend: {af_bella: 0.6, af_heart: 0.4}\n")
    assert cfg.voice_spec() == (("af_bella", 0.6), ("af_heart", 0.4))  # replaces name
    assert cfg.warm_voice() == "af_bella"
    assert parse("voice:\n  name: am_adam\n").voice_spec() == "am_adam"
    assert parse("").voice_spec() == "af_bella"
    for bad in ("{af_bella: 0}", "{af_bella: x}", "{af_bela: 1}"):
        with pytest.raises(config.ConfigError, match="voice.blend"):
            parse(f"voice:\n  blend: {bad}\n")


def test_tts_blend_is_the_weighted_mean_of_style_vectors():
    import numpy as np
    import audio

    class K:
        def get_voice_style(self, n):
            return {"a": np.full((4, 1, 3), 1.0), "b": np.full((4, 1, 3), 3.0)}[n]

    tts = audio.Tts.__new__(audio.Tts)
    tts.kokoro, tts._blends = K(), {}
    assert tts.style("a") == "a"
    out = tts.style((("a", 1.0), ("b", 3.0)))
    assert out.dtype == np.float32 and out.shape == (4, 1, 3) and np.allclose(out, 2.5)
    assert tts.style((("a", 1.0), ("b", 3.0))) is out  # cached


def test_list_voices_groups_by_language_and_gender(capsys, monkeypatch):
    monkeypatch.setattr(config, "available_voices", lambda path=None: VOICES)
    with pytest.raises(SystemExit) as e:
        engine.parse_args(["--list-voices"])
    assert e.value.code == 0
    out = capsys.readouterr().out
    assert "American English (en-us), female:\n  af_bella*  af_heart" in out
    assert "American English (en-us), male:\n  am_adam" in out
    assert "British English (en-gb), female:\n  bf_emma" in out and "Japanese (ja), female:" in out
    assert out.index("American") < out.index("British") < out.index("Japanese")


def test_list_voices_without_voices_file_exits_1(capsys, monkeypatch):
    monkeypatch.setattr(config, "available_voices", lambda path=None: None)
    with pytest.raises(SystemExit) as e:
        engine.parse_args(["--list-voices"])
    assert e.value.code == 1 and "voices file not found" in capsys.readouterr().err


def test_available_voices_reads_the_npz(tmp_path):
    import numpy as np
    path = tmp_path / "v.bin"
    with open(path, "wb") as f:
        np.savez(f, af_x=np.zeros(2), bm_y=np.zeros(2))
    assert config.available_voices(path) == ["af_x", "bm_y"]
    assert config.available_voices(tmp_path / "nope.bin") is None


def test_tts_warmup_and_speech_use_configured_voice_speed_lang():
    import asyncio
    import audio

    calls = []

    class K:
        def create(self, text, voice, speed, lang):
            calls.append((voice, speed, lang))
            return [0.0], 24000

    async def run():
        tts = audio.Tts(type("P", (), {"play": lambda s, a: None, "busy": False})(), (K(), "cpu"), 1.2, "en-gb")
        tts.say("Hello there.", "bf_emma")
        await asyncio.sleep(0.3)

    asyncio.run(run())
    assert calls == [("bf_emma", 1.2, "en-gb")]


def test_startup_is_quiet_in_a_fresh_process(tmp_path):
    """python -c import of the whole chain with a fresh config path: first-run message, file created, exit 0."""
    env = {**os.environ, "CLAUDE_VOICE_CONFIG": str(tmp_path / "c" / "config.yaml"),
           "CLAUDE_VOICE_STATE_DIR": str(tmp_path / "s"), "PYTHONUTF8": "0", "LC_ALL": "C"}
    r = subprocess.run([sys.executable, "-c", "import engine; engine.make_parser()"], cwd=os.path.dirname(__file__),
                       env=env, capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert r.returncode == 0, r.stderr
    assert "first run" in r.stderr and (tmp_path / "c" / "config.yaml").exists()
