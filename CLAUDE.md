# CLAUDE.md

Push-to-talk Whisper dictation for NixOS. Hold right Shift, speak, release —
the transcript is typed into the focused window.

## Running

```bash
nix run .            # the packaged app
nix develop          # then: python main.py, for hacking on main.py
```

The flake builds a self-contained Python env — no `uv`, no `.venv`. On boot the
app runs as a systemd user service via `homeModules.default`, imported from
`/etc/nixos/home.nix` (`services.voice2text.enable = true`). Because that input
is `git+file://`, `main.py` must be committed before `nh os switch` picks it up.

Installing the package is also what GC-roots cuDNN. Without it, `nh clean`
collects it and the next launch spends ~6 min re-fetching 900 MB.

## Architecture

Single file (`main.py`), one `DictationApp`. Nothing is acquired until it is
needed:

- **Idle** — only the hidden Tk window and the `pynput` listener exist. No
  model, no microphone.
- **Key press** — `_acquire()` opens the audio stream (~25 ms) and kicks off
  `_ensure_model()` on a background thread, so the ~1.8 s model load overlaps
  with the user speaking rather than following it.
- **Key release** — a daemon thread waits on `_model_ready`, transcribes with
  streaming segments, and types via `pynput.keyboard.Controller`. Clips shorter
  than `MIN_AUDIO_SEC` are dropped.
- **After `IDLE_RELEASE_SEC`** — `_release_resources()` drops the model and
  closes the stream, returning ~1.9 GB of VRAM and clearing GNOME's
  "microphone in use" indicator.

`_set_state(state, text)` is the only path to a visual change:
`ready → recording → (loading) → transcribing → done → ready`. Presses, audio
callbacks and transcription all run off the main thread, so **every** Tk call
from them goes through `root.after(0, ...)` — including `after_cancel`.

`self.model` is written by the loader thread, read by the transcription thread
and cleared from the Tk thread; all three go through `_model_lock`, and readers
take a local reference. `_is_busy` serialises transcriptions. `_ensure_model`
always sets `_model_ready`, even on failure, or a waiter would hang forever
with `_is_busy` stuck true.

## Configuration (top of `main.py`)

| Constant | Default | Purpose |
|---|---|---|
| `PUSH_TO_TALK_KEY` | `keyboard.Key.shift_r` | Trigger key |
| `MODEL_SIZE` | `"medium.en"` | Whisper model variant |
| `SAMPLE_RATE` | `16000` | Audio sample rate (Hz) |
| `MIN_AUDIO_SEC` | `0.5` | Shorter clips are discarded |
| `IDLE_RELEASE_SEC` | `90` | Idle time before freeing model + mic |

## Constraints

- **4 GB VRAM** (GTX 1650). `medium.en` at fp16 needs ~1.9 GB, so holding it
  while idle is most of the card. Don't reintroduce an eager load.
- **`pynput` runs its X11 backend** (`pynput.keyboard._xorg`) under XWayland,
  so the hotkey only fires while an X11/XWayland window has focus. Typing via
  XTEST works everywhere. Going compositor-independent means switching the
  listener to `evdev`, which needs the user in the `input` group.
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
