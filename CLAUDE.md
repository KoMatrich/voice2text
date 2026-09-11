# CLAUDE.md

Push-to-talk Whisper dictation for NixOS. Hold right Shift, speak, release —
the transcript lands on the clipboard (and, with `AUTO_PASTE`, is pasted into
the focused window).

## Running

```bash
nix run .            # the packaged app
nix develop          # then: python main.py, for hacking on the package
python -m unittest discover -s tests    # in the dev shell
```

The flake builds a self-contained Python env — no `uv`, no `.venv`. On boot the
app runs as a systemd user service via `homeModules.default`, imported from
`/etc/nixos/home.nix` (`services.voice2text.enable = true`). Because that input
is `git+file://`, changes must be committed before `nh os switch` picks them
up, and a new file must at least be `git add`ed before even a local
`nix build` can see it. Only `main.py` and `voice2text/` go into the package's
store path, so edits to anything else don't restart the service.

Installing the package is also what GC-roots cuDNN. Without it, `nh clean`
collects it and the next launch spends ~6 min re-fetching 900 MB.

## Architecture

`main.py` is only a launcher; the code is the `voice2text/` package:

| Module | Job |
|---|---|
| `config.py` | Every tunable constant |
| `gesture.py` | `PushToTalk`: raw key events → start / stop / cancel. Pure, unit-tested |
| `keyboard.py` | evdev device scan, and the listener thread that drives the gesture |
| `audio.py` | `Recorder`: the microphone stream and the captured chunks |
| `model.py` | `ModelLoader`: background load, wait, release |
| `output.py` | `wl-copy`, plus the optional uinput paste chord |
| `overlay.py` | The Tk status window — visuals only |
| `app.py` | `DictationApp`, which wires the rest together |

Nothing is acquired until it is needed:

- **Idle** — only the hidden Tk window and the evdev listener thread exist
  (plus the uinput device, if `AUTO_PASTE`). No model, no microphone.
- **Key down** — still nothing. The key must be held on its own for
  `ARM_DELAY_MS`. Any other key pressed inside that window means right Shift is
  being used as Shift — a capital letter, a shortcut — and the press is dropped
  silently: no overlay, no microphone, no model. Without this, every capital
  typed with right Shift loaded 2.1 GB into VRAM (and once ran the card out of
  memory). Releases of other keys don't count, so the tail of the previous word
  doesn't spoil a press.
- **Armed** — the stream opens (~25 ms), recording starts, the overlay appears
  and `ModelLoader.preload()` begins the ~1.7 s load on a background thread, so
  it overlaps with the user speaking rather than following it. Speech before
  the overlay appears is not captured; its appearance is the cue to talk.
- **Another key while recording** — cancels, with a "Canceled" flash. Further
  keys are ignored until the release.
- **Key up** — a daemon thread waits for the model, transcribes with streaming
  segments, and hands the text to `Output.deliver()`. Clips shorter than
  `MIN_AUDIO_SEC` flash "Canceled" instead.
- **After `IDLE_RELEASE_SEC`** — `_release_resources()` drops the model and
  closes the stream, returning ~2.1 GB of VRAM and clearing GNOME's
  "microphone in use" indicator. Mid-load it reschedules itself rather than
  block the Tk thread.

`Overlay.show(state, text)` is the only path to a visual change:
`ready → recording → (loading) → transcribing → done → ready`, with `canceled`
(nothing to type: too short, no speech, interrupted) and `error` (something
broke) as the other two exits. Both are visible states on purpose -- returning
straight to `ready` made an unloadable model look identical to a recording that
never started. In `recording` the status line is an elapsed clock rather than a
word. The pulse, the clock and the auto-hide are three separate `after` chains,
and every `show()` stops all three.

### Threads

- The **listener thread** owns the `PushToTalk` gesture outright. The arm
  delay is just its `select()` timeout, so there is no timer thread and no lock.
- **All `DictationApp` state lives on the Tk thread.** Gesture callbacks and
  the transcription thread reach it only through `_post()`
  (`root.after(0, ...)`); never call Tk, or a `DictationApp` method, directly
  from another thread. That is why `_recording`, `_busy` and the idle job need
  no locks. `_busy` serialises transcriptions and is cleared on entering a
  terminal state, so a new recording can start while "Copied" is still up.
- `Recorder` locks its chunk list against the PortAudio callback thread.
- `ModelLoader` loads outside its lock. `_idle` is set whenever no load is in
  flight, even after a failure, or a waiting transcription would hang forever
  with `_busy` stuck true.

## Input and output both bypass X11

This is the single most important thing to understand before changing either
end, and the reason `pynput` is *not* a dependency.

Under GNOME Wayland, mutter forwards keystrokes to XWayland only while an X11
window has focus. On this machine `xlsclients` lists five X11 clients — Discord,
Steam, steamwebhelper, ibus-x11, mutter-x11-frames — and nothing else. So an
XRecord listener (what `pynput` uses) sees the push-to-talk key essentially
never. `pynput`'s own `uinput` backend is not an escape hatch either: it builds
its layout table at *import* time by running `dumpkeys`, which fails for a
non-root user.

- **In** — one thread `select()`s over every `/dev/input/event*` device whose
  capabilities include `PUSH_TO_TALK_CODE`, enumerated on each scan rather than
  hard-coded (there is a built-in keyboard *and* a wireless receiver, and the
  receiver comes and goes). Requires the user in the `input` group. `value == 2`
  is autorepeat and must be ignored, or holding the key restarts the recording
  continuously. Only keyboards are watched, so a Shift+click with the mouse is
  not seen as "another key".
- **Out** — `wl-copy` puts the transcript on the clipboard; that's the whole
  path by default. The transcript is never typed automatically, because a
  synthesized keystroke lands in whatever window has focus by the time
  transcription finishes, not necessarily the one the user meant. Setting
  `AUTO_PASTE = True` also emits a single Ctrl+V through `/dev/uinput` right
  after the copy. Nothing is ever typed character by character, so no keymap
  is involved and diacritics survive exactly. When `AUTO_PASTE` is on, the
  uinput device is created once at startup: a fresh one takes a moment for the
  compositor to notice, so building one per paste would race the keystroke.

## Configuration (`voice2text/config.py`)

| Constant | Default | Purpose |
|---|---|---|
| `PUSH_TO_TALK_CODE` | `ecodes.KEY_RIGHTSHIFT` | Trigger key (evdev code) |
| `ARM_DELAY_MS` | `300` | Hold time before recording starts; another key inside it means typing, and the press is ignored |
| `PASTE_CHORD` | `(KEY_LEFTCTRL, KEY_V)` | Add `KEY_LEFTSHIFT` for terminals |
| `RESTORE_CLIPBOARD` | `False` | Restoring races the paste; opt in |
| `AUTO_PASTE` | `False` | Also synthesize the paste chord after copying; off by default so the transcript never lands in the wrong focused window |
| `MODEL_SIZE` | `"large-v3-turbo"` | Whisper model variant |
| `LANGUAGE` | `"en"` | Pinned: auto-detection is a coin flip on short clips. `None` re-enables it |
| `SAMPLE_RATE` | `16000` | Audio sample rate (Hz) |
| `MIN_AUDIO_SEC` | `1.5` | Shorter clips are discarded |
| `DONE_FLASH_MS` | `1400` | How long "Copied"/"Pasted" stays up |
| `CANCEL_FLASH_MS` | `500` | How long "Canceled" stays up |
| `ERROR_FLASH_MS` | `3000` | How long "Error" stays up |
| `TIMER_TICK_MS` | `100` | Recording-clock refresh |
| `IDLE_RELEASE_SEC` | `10` | Idle time before freeing model + mic |

## Constraints

- **4 GB VRAM** (GTX 1650). `large-v3-turbo` at fp16 sits at ~2.1 GB resident,
  so holding it while idle is most of the card. Don't reintroduce an eager load.
- **`MODEL_SIZE` must be a name faster-whisper knows.** An unknown one (an
  invented `medium.large`, say) fails the GPU load *and* the CPU fallback, and
  before the `error` state existed the only symptom was the overlay vanishing
  on key release with nothing pasted. Valid: `tiny`/`base`/`small`/`medium`
  (+`.en`), `large-v1`/`v2`/`v3`, `large-v3-turbo`, `turbo`, `distil-*`.
- **The `input` group only takes effect after a session restart.** Supplementary
  groups are fixed when `user@1000.service` starts, so `nh os switch` alone
  leaves `voice2text.service` with the old group set and no readable input
  devices. Log out and back in, or reboot. `id | grep input` is the check.
- **Run Python with `-u`.** stdout is a pipe to the journal, so without it every
  diagnostic is block-buffered into invisibility — which is exactly why an
  earlier debugging session found a completely empty journal for a service that
  was failing.
- **CUDA CTranslate2 is built from source** and is in no binary cache. It is
  pinned to `sm_75` via `CUDA_ARCH_LIST` (CTranslate2 uses CMake's legacy
  FindCUDA path and ignores `CMAKE_CUDA_ARCHITECTURES`; the default `Auto`
  probes for a GPU, finds none in the sandbox, and builds eight architectures).
  Never set `config.cudaSupport` globally — `faster-whisper` pulls in
  `onnxruntime`, which would then also build from source, for hours.
- **Use `pkgs.python3`, never a pinned `pythonXY`.** Only the pinned nixpkgs'
  default interpreter has a populated binary cache; any other version rebuilds
  torch, transformers and friends from source. The version therefore moves with
  nixpkgs: on the currently pinned rev it is 3.13.15 with CTranslate2 4.7.2,
  and it was 3.14.7 with CTranslate2 4.8.1 one rev earlier. Don't hard-code it.
- **Keep this flake's `nixpkgs` locked to the same rev as `/etc/nixos`.** They
  are linked by `follows`, so a mismatch means CTranslate2 is compiled twice --
  once here, once at `nh os switch`. Read the system rev from the node that
  `root.inputs.nixpkgs` names, which is *not* the node literally called
  "nixpkgs":

  ```bash
  nix eval --impure --raw --expr 'let l = builtins.fromJSON (builtins.readFile /etc/nixos/flake.lock);
    in l.nodes.${l.nodes.root.inputs.nixpkgs}.locked.rev'
  nix flake lock --override-input nixpkgs github:NixOS/nixpkgs/<rev>
  ```
