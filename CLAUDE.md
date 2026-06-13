# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Running the app

```bash
uv run main.py
```

Dependencies are managed with `uv`. To add packages: `uv add <package>`.

## Architecture

Single-file app (`main.py`). The `DictationApp` class owns everything:

**Startup sequence** — `__init__` builds the tkinter UI, then spawns a daemon thread to load the Whisper model (`load_model`). Model load tries GPU (`cuda/float16`) first, falls back to CPU (`int8`). After load, `load_model` also starts the `sounddevice` input stream and the `pynput` keyboard listener — both run for the lifetime of the process.

**Push-to-talk loop** — `pynput` listener fires `on_press`/`on_release` on `PUSH_TO_TALK_KEY` (default: right Shift). Press starts recording into `audio_buffer` via the `sounddevice` callback. Release stops recording and spawns a daemon thread for `transcribe_and_type`.

**Transcription** — `transcribe_and_type` concatenates the buffer, calls `faster_whisper` with streaming segments, and types the result directly into the active window via `pynput.keyboard.Controller`. Minimum audio length is 0.5 s; shorter clips are silently discarded.

**UI / state machine** — `_set_state(state, text)` drives all visual changes. States: `loading → ready → recording → transcribing → done → ready`. All tkinter mutations must go through `root.after(0, ...)` since they originate from non-main threads.

**Threading model** — three concurrent threads beyond the main Tk thread: model-load thread, `sounddevice` audio callback thread, transcription thread. The `_is_busy` flag prevents overlapping transcriptions.

## Key configuration constants (top of `main.py`)

| Constant | Default | Purpose |
|---|---|---|
| `PUSH_TO_TALK_KEY` | `keyboard.Key.shift_r` | Trigger key |
| `MODEL_SIZE` | `"medium.en"` | Whisper model variant |
| `SAMPLE_RATE` | `16000` | Audio sample rate (Hz) |
