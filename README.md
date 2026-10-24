# Claude Voice

A hands-free voice front end for Claude Code. You talk; a fast Claude model answers out loud and turns bigger
requests into Claude Code tasks that run in the background. Anything risky waits for your spoken (or typed) approval.

```
 mic ──▶ VAD ──▶ Whisper ──▶ front agent (Haiku) ──▶ tasks / jobs (Claude Code)
 speakers ◀── Kokoro TTS ◀──────────┘
```

- **Quick tasks** run as one Claude Code session in a project folder.
- **Jobs** handle multi-part work: an orchestrator plans (you approve the plan), then scout, builder and reviewer
  workers carry it out while a step checker vets their edits and commands.
- **Live output window**: each task gets a window that follows its work as it happens. Press `o` to open it.
- Only your claude.ai subscription is used. API keys and other providers are removed from every `claude` process.

## Requirements

- [uv](https://docs.astral.sh/uv/), Python 3.12+, and the [`claude` CLI](https://docs.anthropic.com/en/docs/claude-code) logged in
- A microphone and speakers or headphones
- Linux: [kitty](https://sw.kovidgoyal.net/kitty/). Windows: [Windows Terminal](https://aka.ms/terminal)
- Optional: an NVIDIA GPU (the CUDA libraries come from pip, so no CUDA toolkit is needed). Without one, cv runs on the CPU, more slowly.

## Install

### Linux

```sh
git clone https://github.com/dootdootABM/claude-voice.git ~/src/claude-voice
cd ~/src/claude-voice/sidecar
uv sync --extra gpu     # or: uv sync --extra cpu
../scripts/get-models   # or: ../scripts/get-models --cpu
mkdir -p ~/.local/bin && ln -sf ~/src/claude-voice/scripts/cv ~/.local/bin/cv
```

The last line lets you type `cv` in any terminal. If `~/.local/bin` isn't on your PATH yet, add
`export PATH="$HOME/.local/bin:$PATH"` to your `~/.bashrc` (or your shell's rc file) and open a new terminal.

### Windows

```bat
git clone https://github.com/dootdootABM/claude-voice.git
cd claude-voice\sidecar
uv sync --extra gpu     &rem or: uv sync --extra cpu
..\scripts\get-models.cmd
..\scripts\install-cv.cmd
```

On Windows, `get-models.cmd` also downloads the Whisper models (about 2 GB), so the first launch starts straight away.
`install-cv.cmd` lets you type `cv` in any terminal. Open a new terminal after running it.

## Usage

```sh
cv                  # orb UI
cv --plain          # plain text UI
cv --help           # all options
cv --list-voices
```

**Orb UI keys:** `space` mute · `t` transcript · `i` type · `Esc` stop talking · `o` output window · `q` quit ·
`a`/`r`/`e`/`m` approve, reject, edit or change the model of a pending decision.

**Push to talk:** set `input.mode: push_to_talk` in the config, then hold `v`. For a global hotkey, bind
`cv ptt down` / `cv ptt up` (or `cv ptt toggle`) in your hotkey tool.

## Configuration

Settings are in one file, created on first run from `config.example.yaml`, which documents every key:

- Linux: `~/.config/claude-voice/config.yaml`
- Windows: `%APPDATA%\claude-voice\config.yaml`

Set your name, project folders, voice, models and prompts there. Command-line flags override the file.

## Privacy

Each session logs its full conversation to the state folder (`~/.local/state/claude-voice/logs`, or
`%LOCALAPPDATA%\claude-voice\logs` on Windows). The newest 15 are kept. Audio is saved only with `CV_LOG_AUDIO=1`.

## Limitations

- English only.
- Only NVIDIA GPUs are used for acceleration.
- On Windows, risky-command detection is pattern-based and can miss obfuscated commands. Review plans and approvals yourself.

## Development

```sh
cd sidecar
uv sync --extra cpu --group dev
uv run --no-sync pytest -q
```

## License

MIT, see [LICENSE](LICENSE).
