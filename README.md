# Claude Voice

A hands-free voice front end for Claude Code. You talk; a small, fast Claude model (Haiku by default) answers out
loud and turns bigger requests into Claude Code tasks that run in the background. Anything risky waits for your
spoken (or typed) approval. It runs in a terminal as a breathing orb, or as a plain text UI.

## How it works

```
 mic ──▶ Silero VAD ──▶ Whisper STT ──▶ front agent (Claude, `claude` CLI)
          (speech?)      (faster-whisper)        │   ▲
                                                 │   └── results, questions, approvals
                                   quick task ◀──┴──▶ job (orchestrator ─▶ scout / builder / reviewer)
                                   (one Claude Code session)
                                                 │
 speakers ◀── ducking player ◀── Kokoro TTS ◀────┘ spoken replies
              (voice quietens when you start speaking, stops if you keep going)
```

- **Front agent.** One long-lived `claude` process (`--front-model`, default `haiku`) is the only voice. It has tools
  to start tasks, find projects and record your decisions. The app only accepts a decision if it quotes words from
  your latest message, so the front agent cannot approve anything on its own.
- **Tasks.** A `quick` task is one Claude Code session in a project folder (default back-end model sonnet). A `job`
  is for multi-part work: an orchestrator session plans (you approve the plan), then hands pieces to `scout`
  (read-only), `builder` (one at a time) and `reviewer` subagents, and a step checker vets their edits and commands
  (read-only commands and test runs skip it). To keep usage down, scouts run on `models.scout` (haiku by default),
  workers report in at most 15 lines, and once a piece is done in a conversation that has grown long, the job carries
  on in a fresh orchestrator from its saved state.
- **Live output window.** Each task gets one window of its own that follows its work as it happens, drawn like a
  Claude Code terminal in your wallpaper colours: `● Tool(args)` lines with `⎿` results (`Read 48 lines`,
  `Found 5 files`, command output), numbered diffs, todo lists, `✻ Thinking…`, the spinner with time and tokens, and
  the assistant's text with cv's Markdown, tables and diagrams. Subagents show their last few steps while they run and
  `Done (10 tool uses · 36.8k tokens · 48.4s)` when they finish. For a job you can watch the orchestrator work: each
  worker (scout, builder, reviewer) with the plan check's verdict on its steps, the job rules' refusals, the plan,
  each piece marked done and the hand-over to a fresh orchestrator, plus a panel of the workers still running.
  It opens when a task starts (`behaviour.auto_show_output`) and again when you ask; asking twice never opens a
  second window.
- Every `claude` process is started without `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`, `ANTHROPIC_BASE_URL` and the
  Bedrock/Vertex/Foundry switches, so it can only use your claude.ai subscription.

## Platforms and hardware

| | Status |
|---|---|
| Linux + Hyprland + kitty | **The tested setup.** This is what the author runs. |
| Linux, other desktops | Should work through fallbacks (clipboard via `wl-copy`/`xclip`/`xsel`, kitty for extra windows); only `scripts/cv-window` needs Hyprland. Not tested. |
| Windows 10/11 (native) | **Experimental, untested on real hardware.** See [Windows](#windows-experimental-untested-on-real-hardware). |
| NVIDIA GPU (CUDA) | Whisper `distil-large-v3` and Kokoro on the GPU. The cuBLAS/cuDNN libraries come from pip wheels, so no system CUDA toolkit is needed. |
| CPU only | Works, but slower (see [CPU mode](#cpu-mode)). Non-NVIDIA GPUs (AMD, Intel) are not used: there is no DirectML support, so they run CPU-only. |

## Requirements

- [uv](https://docs.astral.sh/uv/) and Python 3.12 or newer.
- The [`claude` CLI](https://docs.anthropic.com/en/docs/claude-code), logged in to a claude.ai subscription.
- A microphone and speakers or headphones. On speakers cv learns how loud its own voice comes back through the mic and
  ignores it, so you can still talk over it by speaking up; if it still interrupts itself, use `--half-duplex`.
- Linux: the [kitty](https://sw.kovidgoyal.net/kitty/) terminal (the output window and prompt editor open in kitty).
- Windows: [Windows Terminal](https://aka.ms/terminal) (the output window and prompt editor open in it; kitty doesn't
  run natively on Windows). See [Windows](#windows-experimental-untested-on-real-hardware).
- Optional, for `scripts/cv-window` only: Hyprland (`hyprctl`) and `jq`. It is written to be launched from Rofi.

## Setup

```sh
git clone <this repo> ~/src/claude-voice
cd ~/src/claude-voice/sidecar
uv sync --extra gpu     # NVIDIA GPU (CUDA)
uv sync --extra cpu     # no NVIDIA GPU
```

Pick exactly one extra; they conflict (`onnxruntime` vs `onnxruntime-gpu`).

Fetch the Kokoro v1.0 text-to-speech files into `sidecar/models/` (the folder is git-ignored):

```
scripts/get-models          # all three (about 450 MB); scripts\get-models.cmd on Windows
scripts/get-models --cpu    # only what a machine without CUDA needs (about 120 MB)
```

They are `kokoro-v1.0.onnx` (used on CUDA), `kokoro-v1.0.int8.onnx` (CPU) and `voices-v1.0.bin`, from the
[kokoro-onnx releases](https://github.com/thewh1teagle/kokoro-onnx/releases/tag/model-files-v1.0). Complete files are
skipped and each download is checked by size. `--from DIR` takes them from another copy's `models` folder instead.

The Whisper models (`distil-large-v3`, and `small.en`) are fetched by faster-whisper on first run.

Pip users can instead run `pip install -r requirements-gpu.txt` or `pip install -r requirements-cpu.txt` (read the
header of `requirements-gpu.txt` first: plain `onnxruntime` conflicts with `onnxruntime-gpu`). The launchers run
`uv run --no-sync`, so they need the uv environment in `sidecar/.venv`.

### CPU mode

`--device auto|cuda|cpu` (default `auto`) chooses where Whisper runs: `auto` uses CUDA if CTranslate2 finds an NVIDIA
GPU, otherwise the CPU. `cpu` also keeps Kokoro off the GPU. On the CPU the defaults change: the main model is
`small.en` (instead of `distil-large-v3`) and the separate fast model is off. Expect slower replies: `distil-large-v3`
on a CPU would take roughly 5-15 s per utterance (an estimate, not measured here), which is why it is not the CPU
default. Kokoro speech is also slower on the CPU. `--stt-model` overrides the model on either device.

## Running

```sh
scripts/cv              # the orb UI
scripts/cv --plain      # plain scrolling text UI
scripts/cv --help
scripts/cv --list-voices   # the voices you can set in config.yaml
scripts/cv-window       # Linux/Hyprland only: focus Claude Voice, or open it in a floating kitty window (class claude-voice)
```

On Windows use `scripts\cv.cmd` or `scripts\cv.ps1` instead (below).

Options (the defaults of most of them come from your `config.yaml`, see Configuration): `--plain`, `--list-voices`, `--device auto|cuda|cpu`, `--half-duplex` (ignore the mic while it speaks), `--no-mic` (type only),
`--verbosity quiet|normal|chatty`, `--stt-model` (default `distil-large-v3` on CUDA, `small.en` on CPU; `small.en` is faster, less accurate),
`--front-model` (default `haiku`), `--no-fast-stt` / `--fast-stt` (don't use `small.en` for short answers to a pending question).
Only one copy can run at a time. `scripts/cv` (and the Windows launchers, via `sidecar/supervisor.py`) restarts the app when a task asks for it: the app exits with code 75, which the supervisor treats as "restart"; any other exit code ends the loop.

Keys in the orb UI: `space` mute, `t` transcript, `i` type, `Esc` stop talking, `o` open the latest output, `q` quit,
and `a` / `r` / `e` / `m` to answer a pending decision (approve, reject, edit the task prompt, change back-end model).

Plain UI: talk or type; Enter on an empty line stops speech; `/mute`, `/unmute`, `/quiet`, `/normal`, `/chatty`,
`/tasks`, `/kill <task>`, `/quit`; the decision keys above work when a card is shown.

### Push to talk

Set `input.mode: push_to_talk` in `config.yaml` (default `always_listening`). Then the mic only listens while you hold
the talk key: hold `v` in the cv window (`input.ptt_key` changes the key). In the orb UI, `p` switches between
always-listening and push-to-talk while it runs. `input.global_ptt_key` (for example `ctrl+alt+v`) optionally adds a
system-wide key; it needs the optional pynput or evdev packages.

You can also press the key from anywhere with the running app's control socket:

```sh
cv ptt down      # start talking (like pressing the key)
cv ptt up        # stop talking (like releasing it)
cv ptt toggle    # down if idle, up if talking
cv ptt status
```

It exits 0 when done, 1 if cv is not running, 2 on a usage error. On Linux, bind it in your window manager or hotkey
tool; for example with `sxhkd` (press and release, the `@` prefix means on release):

```
super + v
    ~/src/claude-voice/scripts/cv ptt down
@super + v
    ~/src/claude-voice/scripts/cv ptt up
```

If your tool cannot see key release, bind `cv ptt toggle` to one key. On Windows, with AutoHotkey v2 (hold F9 to talk):

```
F9::Run('C:\path\to\claude-voice\scripts\cv.cmd ptt down', , 'Hide')
F9 Up::Run('C:\path\to\claude-voice\scripts\cv.cmd ptt up', , 'Hide')
```

Output window keys: arrows or `j`/`k` scroll, `h`/`l` sideways, `space`/`b` page, `g`/`G` top/end, `]`/`[` section,
`t` contents, `y` copy, `q` close. In the live window: `e` expands or collapses the result or subagent under the
cursor, `Enter` opens the worker under the cursor on its own (its brief, every step, its report; `Esc` goes back),
`f` turns following new output on and off, scrolling up stops following and `G` (or `End`) resumes it.

## Windows (experimental, untested on real hardware)

Windows 10/11 support was written and tested only on Linux plus CI runners. It has never been run on a real Windows
machine with a microphone, so expect rough edges.

1. Install [uv](https://docs.astral.sh/uv/), the `claude` CLI, and [Windows Terminal](https://aka.ms/terminal), the
   Windows counterpart of kitty (`winget install Microsoft.WindowsTerminal` if `wt` isn't found; some Windows 11
   editions don't ship it). Start cv from it.
2. From `sidecar\`, run `uv sync --extra gpu` (NVIDIA) or `uv sync --extra cpu`. With pip, use
   `requirements-gpu.txt` or `requirements-cpu.txt` as above.
3. Run `scripts\get-models.cmd` (or `scripts\get-models.cmd --cpu`). Besides the Kokoro files, on Windows it also
   fetches the Whisper models (about 2 GB; only `small.en` with `--cpu`) into the Hugging Face cache, so the first
   launch doesn't sit at "waking up" while they download. Run it after `uv sync`: that part runs in the uv environment.
   `--no-whisper` skips it.
4. Launch from a terminal: `scripts\cv.cmd` (cmd.exe) or `scripts\cv.ps1` (PowerShell). Arguments pass through, e.g.
   `scripts\cv.cmd --plain --device cpu`.
5. Optional: run `scripts\install-cv.cmd` once to type just `cv` in any terminal, as on Linux. It writes a small
   `cv.cmd` into `%USERPROFILE%\.local\bin` (where uv installs itself) that runs this copy's `scripts\cv.cmd`, and adds
   that folder to your user PATH if needed; open a new terminal afterwards. `scripts\install-cv.cmd --remove` undoes it.

PowerShell blocks unsigned scripts by default. Either run
`powershell -ExecutionPolicy Bypass -File scripts\cv.ps1`, or allow local scripts once with
`Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`. `cv.cmd` has no such restriction.

State (logs, tasks, outputs) goes to `%LOCALAPPDATA%\claude-voice` unless `CLAUDE_VOICE_STATE_DIR` is set. Extra windows
open in Windows Terminal (`wt.exe`) when it is on `PATH`. Without it they fall back to a plain console window, which
Windows tends to open behind the current one, and `o` doesn't raise it. The clipboard uses `pyperclip`.

**Permission rules are weaker on Windows.** Spoken approval is required for risky commands, and the checks know the
Windows shells (`PowerShell` tool, `del`, `erase`, `rd /s`, `rmdir`, `Remove-Item`, `ri`, and sensitive locations such
as `C:\Windows`, `C:\Program Files`, `%APPDATA%` and `~\.ssh`). But delete detection is regex-based. Obfuscated or
variable-built commands (for example a command assembled from pieces in a variable) and aliases beyond the ones
listed are **not** detected. (`Invoke-Expression`/`iex`, encoded PowerShell and downloaded-script patterns do trigger
an approval request, but that only covers the obvious spellings.) Do not rely on it as a security boundary; review plans and approvals
yourself.

## Continuous integration

`.github/workflows/ci.yml` runs pytest on `ubuntu-latest` and `windows-latest` with Python 3.12 and 3.13 (220 tests on Linux; fewer run on Windows because the `linux_only` ones skip),
installed with `uv sync --extra cpu --group dev`. It uses no audio device, no models, no microphone and no Claude;
tests that need Linux (Hyprland, tty, POSIX signals) are marked `linux_only` and skipped on Windows.

CI does **not** cover: real WASAPI audio capture and playback, Windows Terminal / `wt.exe`, `.cmd` shims for `claude`,
CUDA/cuDNN DLL loading, the Windows launchers (`cv.cmd`, `cv.ps1`), or real CPU speed. These are the parts most
likely to break on Windows.

### Trying it as a new user

`scripts/try-fresh` (`scripts\try-fresh.cmd` on Windows) runs cv with a throwaway config and state folder in your temp
directory, so your own config and saved tasks are never read or touched. The first run creates the config from
`config.example.yaml` as it would for anyone; `try-fresh --keep` reuses the last one. `test_release.py` checks that no
file git tracks holds a personal path, an e-mail address, or (on a machine with a real config) your own name or
system line.

## Configuration

Everything you would want to change lives in **one YAML file**, `config.yaml`:

| Platform | Location |
|---|---|
| Linux | `~/.config/claude-voice/config.yaml` (`$XDG_CONFIG_HOME` is honoured) |
| Windows | `%APPDATA%\claude-voice\config.yaml` |
| macOS | `~/Library/Application Support/claude-voice/config.yaml` |

Set `CLAUDE_VOICE_CONFIG` to use another file (or a folder holding `config.yaml`). On the first run, if the file is
missing, `config.example.yaml` from this repo is copied there and a one-time message says so. The example lists every
key with a comment; keys you delete keep their built-in default. The file is read at every start, and a mistake gives a
message naming the key and line (for example `voice.name: 'af_bela' is not a Kokoro voice; did you mean 'af_bella'?`);
unknown keys only produce a warning. Merge order: built-in defaults, then your file, then command-line flags, so
`cv --verbosity quiet` always wins over the file.

```yaml
user:        { name, system, project_dirs, notes_dir, preferences, load_global_claude_md }
behaviour:   { verbosity, auto_show_output, half_duplex }
models:      { front, backend, job, scout, checker }
voice:       { name, blend, speed, lang }
stt:         { device, model, fast_model }
input:       { mode, ptt_key, global_ptt_key }
prompts:     { front_agent, front_context, narration, go_note, show_note, short_ack,
               handover_ask, handover_start, handover_job, orchestrator, step_check }
```

- **`user`** is your name, machine, project folders (the first is where new projects go), notes folder and
  preferences. They are filled into the prompts. `load_global_claude_md` (default true) also loads your
  `~/.claude/CLAUDE.md` into the assistant's context.
- **Prompts are fully replaceable.** Setting a prompt key replaces the whole built-in text (it is not merged); leave it
  out to keep the built-in one. Placeholders: `{name} {system} {project_dirs} {new_project_dir} {notes_dir}
  {notes_line} {preferences} {app_dir}`. `handover_start` must keep `{project}` and `{note}`, `handover_job` must keep
  `{note}`; this is checked at start. Other braces (such as JSON in a prompt) are left alone.
- **Voice.** `voice.name` is any voice in `models/voices-v1.0.bin`; `cv --list-voices` prints them grouped by language
  and gender. `voice.blend` (for example `{af_bella: 0.6, af_heart: 0.4}`) mixes voices and, when set, replaces
  `name`.
  `voice.lang` is the language of the spoken text. The Kokoro model itself is not configurable.
- `input.*` is push to talk (see [Push to talk](#push-to-talk)). `behaviour.auto_show_output` opens a task's live
  output window when it starts.
- Tasks never speak: their work shows in the live output window, and the front agent sums up each round.
  `behaviour.verbosity` sets how much it says then: `quiet` one sentence, `normal` up to three, `chatty` up to five
  with the notable steps.
- `behaviour.verbosity`, `models.front`, `stt.device`, `stt.model`, `stt.fast_model` and `behaviour.half_duplex` are
  the defaults of the matching flags (`--verbosity`, `--front-model`, `--device`, `--stt-model`,
  `--fast-stt/--no-fast-stt`, `--half-duplex`).
- PyYAML is a base dependency. If it is somehow missing, cv starts with the built-in defaults and says so.

Other settings:

- `CLAUDE_VOICE_STATE_DIR` - state directory (default `~/.local/state/claude-voice`; `%LOCALAPPDATA%\claude-voice` on Windows): logs, open tasks
  (`tasks.json`), outputs, job plans.
- `CV_LOG_AUDIO=1` - also save each heard utterance as a 16 kHz WAV in the session log folder.
- `VISUAL` / `EDITOR` - editor used when you edit a task prompt (opened in kitty; default `vim`, `notepad` on Windows).
- `XDG_RUNTIME_DIR` - where the single-instance lock file lives (falls back to the system temp dir).
- `<state dir>/ui.json` - UI preferences (whether the transcript is shown); written by the app.
- Project roots searched when you name a project: `~/src` (2 levels), `~` (3 levels), `~/.config` (1 level); change
  `ROOTS` in `sidecar/projects.py`.
- Colours follow the kitty palette in `~/.config/kitty/matugen.conf` if it exists (`sidecar/theme.py`), otherwise
  built-in defaults.

## Main parts (`sidecar/`)

- `tui.py` - entry point; the orb terminal UI. `voice_term.py` - the `--plain` text UI.
- `engine.py` - the UI-free core: option parsing, startup, wiring of audio and agents.
- `config.py` - the config file: defaults, merge, validation, the example file. `default_prompts.py` - the built-in prompt texts.
- `audio.py` - mic, Silero VAD, Whisper STT, Kokoro TTS, the interruptible/ducking player.
- `agents.py` - front agent, tasks, decisions, risk checks. `orchestrator.py` - project jobs.
- `claude_proto.py` - client for Claude Code's headless stream-json protocol.
- `projects.py` - project folder lookup. `sessions.py` - earlier Claude Code conversations per project.
- `show_output.py` - the Markdown output window. `live.py` - a task's live event log and its window;
  `live_view.py` - the live window itself. `orb.py` - braille orb renderer. `theme.py` - colours.
- `cvlog.py` - session logs. `restart_job.py` - runs a task's command between restarts.

## Tests

From `sidecar/` (pytest is in the `dev` group; no models, audio device or Claude needed):

```sh
uv sync --extra cpu --group dev
uv run --no-sync pytest -q
```

## Privacy

Each launch writes a log folder under `<state dir>/logs/` (the newest 15 are kept). `events.log` has what was said
and heard, tool calls and results, and `raw/*.jsonl` has every message to and from each `claude` process, so these
contain your full conversations. Audio is saved only if you set `CV_LOG_AUDIO=1`. Task outputs and job plans are
also stored in the state dir. Claude Code keeps its own transcripts in `~/.claude/projects/`.

## Known limitations

- Linux + Hyprland + kitty is the only tested setup; Windows is experimental and untested on real hardware.
- On CPU, speech recognition uses a smaller model and replies are slower. Only NVIDIA GPUs are accelerated.
- Windows permission rules are regex-based and miss obfuscated commands (see above).
- English only (Whisper is called with `language="en"`).

## License

MIT, see [LICENSE](LICENSE).
