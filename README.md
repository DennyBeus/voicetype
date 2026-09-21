# voicetype

**Talk to your terminal agents instead of typing to them.**

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
![Platform](https://img.shields.io/badge/platform-Linux%20%2F%20X11-lightgrey)
![No local models](https://img.shields.io/badge/local%20models-none-green)

*Русская версия: [README.ru.md](README.ru.md)*

Hold a key, say what you want, get an editable window, press Enter — the text
lands in the terminal where your cursor was. Built for dictating long prompts
to CLI coding agents in Russian mixed with English technical slang.

---

## Why this exists

I work with AI agents in the terminal all day. The bottleneck was never typing
speed — it's that a fully-formed thought is easier to *say* than to type. By
the time I had typed out the third sentence of context, I had lost the shape of
the fourth.

So this is not a dictation app that replaces your keyboard. It is a way to get
a paragraph of intent into a terminal prompt without breaking your train of
thought, and then still fix it by hand before it goes anywhere.

Three things it had to get right:

- **Stay out of the way.** One background process, started by one command and
  stopped by one command. It never launches itself at boot. While it is
  stopped, the trigger key behaves like an ordinary key.
- **Handle how developers actually speak.** Russian with English terms mixed
  in — «заребейзь фичу на мейн» has to come out as `rebase` and `main`, not
  transliterated into Cyrillic.
- **Never paste blind.** Everything goes through an editor window first. That
  window is where you paste the URL, fix the path, add the flag — all the
  things you cannot say out loud.

Then it's Enter to put the text in the terminal, and Enter again to hand it to
your agent.

---

## How it works

```
                 ┌─ hold the key ──── speak ──── release ─┐
cursor in        │                                        │
a terminal ──────┤  OR                                    ├──► beep
                 │                                        │
                 └─ tap ─ speak ─ tap again ──────────────┘
                                  │
                pw-record 16 kHz mono ─→ ffmpeg ─→ Opus, in memory
                                  ▼
              OpenAI  gpt-transcribe                      ~1–2 s
              keywords=<your glossary>  languages=["ru","en"]
                                  ▼
              DeepSeek  deepseek-chat                     ~1–2 s
              punctuation, filler words, self-corrections
              ↳ skipped entirely if you don't add the key
                                  ▼
                  ┌─────────────────────────────────┐
                  │  editor window, always on top   │  ← beep
                  │  grey: previous chunk, if any   │
                  │  ─────────────────────────────  │
                  │  edit freely, paste links,      │
                  │  Ctrl+Z, Ctrl+V — it's a real   │
                  │  text widget                    │
                  │  Enter = insert · Esc = cancel  │
                  └─────────────────────────────────┘
                                  ▼
              focus returns to the original window → paste
              (your clipboard is restored) → cursor at the end
                                  │
                  ┌───────────────┘
                  ▼
        press the key again → the next chunk lands right after,
        the previous one stays untouched
```

No local models. Transcription is an API call, so the whole thing is ~20 MB of
dependencies and starts instantly — nothing to load into VRAM.

---

## Before you clone: the honest limitations

- **X11 only.** Global key capture uses the X RECORD extension and pasting uses
  XTEST. Wayland deliberately makes both impossible for unprivileged apps.
  Check with `echo $XDG_SESSION_TYPE`.
- **Requires an internet connection.** There is no offline mode by design.
- **Your audio goes to OpenAI**, and if you enable the second stage, your
  transcript goes to DeepSeek. If that is not acceptable for what you dictate,
  this tool is not for you.
- **It costs money**, though not much: about **$0.0045 per minute** of audio.
- Tested on Ubuntu 24.04 with GNOME and PipeWire. Other setups should work but
  have not been verified.

---

## Requirements

- Linux with X11 and PipeWire
- [`uv`](https://docs.astral.sh/uv/)
- `pw-record`, `pw-play`, `ffmpeg` — usually already installed
- `python3-tk` — for the editor window
- a microphone
- an OpenAI API key (required); a DeepSeek API key (optional)

## Quick start

```bash
sudo apt install python3-tk
git clone https://github.com/<you>/voicetype ~/Projects/develop/voicetype
cd ~/Projects/develop/voicetype
bash install.sh
```

Add your key:

```bash
$EDITOR .env          # OPENAI_API_KEY=sk-...
```

Leave `DEEPSEEK_API_KEY` empty if you want. The cleanup stage then simply never
turns on — that is a supported configuration, not an error.

Check the microphone, then start:

```bash
./scripts/check_mic.sh     # records 10 s, plays it back, prints the config line
./bin/voicetype start
```

## Running it

```bash
./bin/voicetype start     # start
./bin/voicetype stop      # stop
./bin/voicetype restart   # after editing config.toml or keywords.txt
./bin/voicetype status    # what is configured, and is it running
./bin/voicetype logs      # live log
./bin/voicetype history   # recent dictations (history 20 for the last 20)
./bin/voicetype run       # foreground, for debugging
```

**If a paste gets lost.** Every transcript is written to `.state/history.jsonl`
right after recognition, before the review window opens. The final text after
editing is logged too, marked as inserted or failed.
`voicetype history` prints the recent entries so you can copy the text back.
The file is owner-only (`0600`), ignored by git, and keeps the last 500 entries.

Worth adding to `~/.bashrc`:

```bash
export PATH="$PATH:$HOME/Projects/develop/voicetype/bin"
```

**It does not start at boot.** The systemd unit is installed but never enabled —
`systemctl --user is-enabled voicetype` reports `linked`, which means "the unit
exists but nothing starts it". Only `voicetype start` does. If you ever want
autostart, that is `systemctl --user enable voicetype`.

## Using it

Default trigger is **Right Ctrl**. One key, two modes:

| | |
|---|---|
| **Hold and speak** | records while held; release ends it |
| **Tap once** | records until you tap again — for longer thoughts |

A beep when recording starts, a beep when it stops, a low beep on errors.

### The editor window

It always appears. Nothing reaches your terminal unreviewed.

| Key | Action |
|---|---|
| `Enter` | insert into the original window |
| `Shift+Enter` | newline |
| `Esc` | cancel |
| `Ctrl+Esc` | cancel and reset the dictation session |
| `Ctrl+R` | show the raw text, before cleanup |
| `Ctrl+Z` / `Ctrl+Shift+Z` | undo / redo |
| `Ctrl+A` | select all |
| `Ctrl+C` / `Ctrl+V` / `Ctrl+X` | normal clipboard operations |
| `Ctrl+W` | delete the word to the left |
| Right Ctrl | keep dictating straight into this window |

Your clipboard is left alone while the window is open, so `Ctrl+V` pastes what
*you* copied. The tool takes the clipboard only for the instant of pasting and
puts your content straight back.

### Continuing a dictation

Dictate, press Enter, the text is in your terminal. Remember something else?
Press the key again — the new chunk lands right after the old one, which stays
intact. Spacing and capitalisation at the seam are handled for you.

A session is tied to a window and lives for 10 minutes (configurable). Switch
windows and a new one begins. In the editor window, the previous chunk is shown
above in grey.

The tool cannot read your terminal's contents — nothing can — so it tracks only
its own insertions. Edit the line by hand between two dictations and the seam
may be off by a space.

## Configuration

Everything lives in `config.toml`, created from `config.example.toml` on install
and git-ignored. The parts worth knowing:

```toml
[audio]
source = "auto"          # PipeWire source name; check_mic.sh prints yours

[hotkey]
key = "ctrl_r"           # ctrl_r, alt_r, super_r, f13, pause, menu…
hold_threshold_ms = 400  # the line between "held" and "tapped"

[stt]
model = "gpt-transcribe"
languages = ["ru", "en"] # mixed speech stops being transliterated

[cleanup]
enabled = true           # false disables DeepSeek even if a key is present

[ui]
font_family = ""         # empty = system font
font_size = 15
```

`keywords.txt` is your glossary — terms the model must hear and spell exactly
(`kubectl`, not «кубэ контрол»). It is passed to the transcription model as the
`keywords` parameter, so it improves recognition itself rather than patching the
output afterwards. Add your stack, then `voicetype restart`.

## Under the hood

A few decisions that are not obvious from the code, and cost real debugging time:

- **The clipboard cannot be served from a blocked event loop.** In X11 the
  clipboard owner has to answer a `SelectionRequest`. Tk owns it, so sleeping in
  the main thread between "copy" and "paste" means the terminal asks and nobody
  answers — the paste silently yields nothing while every log line looks fine.
  All paste steps are staged through the Tk timer instead.
- **Never call Tk from a worker thread.** `root.after()` from a background
  thread does not raise — it is silently dropped. Network work happens off the
  main thread, so results come back through a queue drained by a main-thread
  timer.
- **`pw-record` ignores a non-existent `--target`** and quietly records from the
  default device. The configured source is validated at startup, otherwise a
  typo in `config.toml` looks like "the microphone records silence".
- **Bluetooth headsets are a poor fit.** In microphone mode they drop to 16 kHz
  HFP with aggressive noise suppression, which is exactly the wrong input for
  accented, slang-heavy speech. A cheap USB microphone beats expensive earbuds
  here. AirPods in particular often fail to negotiate HFP on Linux at all.

Zero `apt` packages beyond `python3-tk`: recording uses `pw-record`, beeps use
`pw-play`, encoding uses `ffmpeg`, key emulation uses `python-xlib`.

## Secrets

`.env` sits in the project root and is the first line of `.gitignore`.
`install.sh` sets it to `chmod 600` and installs a `pre-commit` hook that
refuses to commit either the file or anything that looks like `sk-...`.

That will not save you from `git add -f` or from copying the whole folder
somewhere else.

## Troubleshooting

```bash
./bin/voicetype run -v                                    # log to the terminal
uv run python -m voicetype.stt --file recording.ogg       # transcription only
uv run python -m voicetype.cleanup --text "ну ээ ребейз"  # cleanup only
```

- **The key does nothing** — you are probably on Wayland: `echo $XDG_SESSION_TYPE`.
- **Empty recording** — wrong source in `config.toml`, or the headset went to
  sleep. Run `./scripts/check_mic.sh`.
- **Ugly, unreadable font** — Tk was built without fontconfig. Install
  `python3-tk` and re-run `bash install.sh`; it picks the better interpreter
  automatically.
- **Text pastes into the wrong window** — the terminal list for `Ctrl+Shift+V`
  is `[inject].terminal_classes`; find your window's class with `xprop WM_CLASS`.

## Uninstalling

```bash
bash uninstall.sh        # unit and venv; .env and settings are kept
rm -rf ~/Projects/develop/voicetype
```

## License

MIT — see [LICENSE](LICENSE).
