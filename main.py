import gc
import select
import subprocess
import tkinter as tk
import threading
import time
import evdev
import sounddevice as sd
import numpy as np
from evdev import ecodes
from faster_whisper import WhisperModel

# --- CONFIGURATION ---
# Input and output both bypass X11 entirely. Under GNOME Wayland, mutter only
# forwards keystrokes to XWayland while an X11 window has focus, so an XRecord
# listener (what pynput uses) never sees the key in a native Wayland window --
# which is nearly every window. Reading /dev/input is compositor-independent.
PUSH_TO_TALK_CODE = ecodes.KEY_RIGHTSHIFT
# The transcript goes onto the clipboard and is pasted with one chord, rather
# than typed character by character: no keymap is involved, so diacritics
# survive. Terminals want (KEY_LEFTCTRL, KEY_LEFTSHIFT, KEY_V) instead.
PASTE_CHORD = (ecodes.KEY_LEFTCTRL, ecodes.KEY_V)
RESTORE_CLIPBOARD = False  # restoring races the paste; opt in if you want it
# Our own virtual keyboard advertises every key code, so it would otherwise be
# picked up by the listener's own device scan.
UINPUT_NAME = "voice2text"
MODEL_SIZE = "medium.en"  # Options: tiny, base, small, medium, large (and .en variants)
SAMPLE_RATE = 16000
MIN_AUDIO_SEC = 0.5
# How long to stay idle before dropping the model (~1.9 GB of VRAM on this 4 GB
# card) and closing the microphone. Reacquiring both costs ~1.8 s, and that is
# hidden under the next recording because the load starts on key press.
IDLE_RELEASE_SEC = 90

# --- THEME ---
BG_COLOR      = "#1c1c1e"
TEXT_COLOR    = "#f5f5f7"
SUBTEXT_COLOR = "#8e8e93"
ACCENT_IDLE   = "#30d158"
ACCENT_REC    = "#ff453a"
ACCENT_PROC   = "#ffd60a"

WIN_W = 270
WIN_H = 50


class DictationApp:
    def __init__(self, root):
        self.root = root
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.attributes("-alpha", 0.95)
        self.root.configure(bg=BG_COLOR)
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

        # Force Tk to resolve geometry before we query screen dimensions
        self.root.update_idletasks()
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        x = (sw - WIN_W) // 2
        y = sh - WIN_H - 60
        self.root.geometry(f"{WIN_W}x{WIN_H}+{x}+{y}")

        self._build_ui()
        self._build_context_menu()

        # State variables
        self.model = None
        self.stream = None
        self.is_recording = False
        self.key_pressed = False
        self.audio_buffer = []
        self._pulse_state = False
        self._pulse_job = None
        self._hide_job = None
        self._idle_job = None
        self._is_busy = False
        # self.model is written by the loader thread, read by the transcription
        # thread and cleared from the Tk thread, so every touch of it is either
        # under this lock or via a local reference taken under it.
        self._model_lock = threading.Lock()
        self._model_ready = threading.Event()

        # Created once and held: a freshly created uinput device takes a moment
        # to be noticed by the compositor, so building one per paste would race
        # the very keystroke we are trying to send.
        self._uinput = evdev.UInput(name=UINPUT_NAME)

        # Nothing is acquired up front: no model, no audio stream. Only the
        # listener, so push-to-talk is live as soon as the imports finish.
        self._set_state("ready")
        self._listening = True
        self.listener = threading.Thread(target=self._listen_loop, daemon=True)
        self.listener.start()

    # ------------------------------------------------------------------ UI build

    def _build_ui(self):
        self.main_frame = tk.Frame(self.root, bg=BG_COLOR)
        self.main_frame.pack(fill="both", expand=True, padx=14, pady=10)
        self.main_frame.columnconfigure(1, weight=1)

        # Animated status dot
        self.dot_canvas = tk.Canvas(
            self.main_frame, width=14, height=14,
            bg=BG_COLOR, highlightthickness=0,
        )
        self.dot_canvas.grid(row=0, column=0, padx=(0, 8), pady=(2, 0), sticky="n")
        self.dot_item = self.dot_canvas.create_oval(2, 2, 12, 12, fill=ACCENT_IDLE, outline="")

        # Status label
        self.status_label = tk.Label(
            self.main_frame, text="",
            font=("Helvetica", 12, "bold"),
            fg=TEXT_COLOR, bg=BG_COLOR, anchor="w",
        )
        self.status_label.grid(row=0, column=1, sticky="ew")

        # Key hint label (right-aligned)
        key_name = ecodes.KEY[PUSH_TO_TALK_CODE].replace("KEY_", "")
        self.key_label = tk.Label(
            self.main_frame, text=f"[{key_name}]",
            font=("Helvetica", 9),
            fg=SUBTEXT_COLOR, bg=BG_COLOR, anchor="e",
        )
        self.key_label.grid(row=0, column=2, sticky="e", padx=(4, 0))

        # Progressive transcription / info text label
        self.text_label = tk.Label(
            self.main_frame, text="",
            font=("Helvetica", 10),
            fg=SUBTEXT_COLOR, bg=BG_COLOR,
            wraplength=WIN_W - 28, justify="left", anchor="w",
        )
        self.text_label.grid(row=1, column=0, columnspan=3, sticky="ew", pady=(4, 0))

        # Make the whole window draggable
        for widget in (self.root, self.main_frame, self.dot_canvas,
                       self.status_label, self.key_label, self.text_label):
            widget.bind("<ButtonPress-1>", self._drag_start)
            widget.bind("<B1-Motion>", self._drag_motion)

    def _build_context_menu(self):
        self.menu = tk.Menu(
            self.root, tearoff=0,
            bg="#2c2c2e", fg=TEXT_COLOR,
            activebackground="#3a3a3c", activeforeground=TEXT_COLOR,
        )
        self.menu.add_command(label="Quit Voice2Text", command=self.on_close)

        for widget in (self.root, self.main_frame, self.dot_canvas,
                       self.status_label, self.key_label, self.text_label):
            widget.bind("<Button-3>", self._show_context_menu)

    # ---------------------------------------------------------------- drag support

    def _drag_start(self, event):
        self._drag_x = event.x_root - self.root.winfo_x()
        self._drag_y = event.y_root - self.root.winfo_y()

    def _drag_motion(self, event):
        x = event.x_root - self._drag_x
        y = event.y_root - self._drag_y
        self.root.geometry(f"+{x}+{y}")

    def _show_context_menu(self, event):
        self.menu.tk_popup(event.x_root, event.y_root)

    # ---------------------------------------------------------------- show / hide

    def _show_window(self):
        if self._hide_job:
            self.root.after_cancel(self._hide_job)
            self._hide_job = None
        if not self.root.winfo_viewable():
            self.root.deiconify()
            self.root.lift()

    def _hide_window(self):
        self.root.withdraw()

    # ---------------------------------------------------------------- pulse animation

    def _start_pulse(self, color_a, color_b, interval_ms=600):
        self._stop_pulse()
        self._pulse_state = True
        self._do_pulse(color_a, color_b, interval_ms, toggle=True)

    def _do_pulse(self, color_a, color_b, interval_ms, toggle):
        if not self._pulse_state:
            return
        self.dot_canvas.itemconfig(self.dot_item, fill=color_a if toggle else color_b)
        self._pulse_job = self.root.after(
            interval_ms,
            lambda: self._do_pulse(color_a, color_b, interval_ms, not toggle),
        )

    def _stop_pulse(self):
        self._pulse_state = False
        if self._pulse_job:
            self.root.after_cancel(self._pulse_job)
            self._pulse_job = None

    # ---------------------------------------------------------------- state machine

    def _set_state(self, state: str, text: str = ""):
        if state == "ready":
            self._stop_pulse()
            self.dot_canvas.itemconfig(self.dot_item, fill=ACCENT_IDLE)
            self._hide_window()
            self._is_busy = False

        elif state == "recording":
            if self._hide_job:
                self.root.after_cancel(self._hide_job)
                self._hide_job = None
            self.text_label.config(text="")
            self.status_label.config(text="Recording", fg=ACCENT_REC)
            self._start_pulse(ACCENT_REC, "#7a0000", 400)
            self._show_window()

        elif state == "loading":
            # Only reached when the key was released before the model finished
            # loading; otherwise the load is fully hidden by the recording.
            self.status_label.config(text="Loading model...", fg=ACCENT_PROC)
            self.text_label.config(text="")
            self._start_pulse(ACCENT_PROC, "#7a6500", 700)
            self._show_window()

        elif state == "transcribing":
            self.status_label.config(text="Transcribing...", fg=ACCENT_PROC)
            self._start_pulse(ACCENT_PROC, "#7a6500", 700)
            if text:
                self.text_label.config(text=text)

        elif state == "done":
            self._stop_pulse()
            self.dot_canvas.itemconfig(self.dot_item, fill=ACCENT_IDLE)
            self.status_label.config(text="Done", fg=ACCENT_IDLE)
            self.text_label.config(text=text if text else "")
            self._hide_job = self.root.after(1400, lambda: self._set_state("ready"))
            self._schedule_idle_release()

    # ---------------------------------------------------------------- acquire / release

    def _acquire(self):
        """Open the microphone and start loading the model, both on key press."""
        # after_cancel must run on the Tk thread; we are on the listener
        # thread here. The cancel is therefore async and can lose a race with
        # the timer firing - _release_resources re-checks key_pressed for that.
        self.root.after(0, self._cancel_idle_release)

        if self.stream is None:
            self.stream = sd.InputStream(
                samplerate=SAMPLE_RATE, channels=1, dtype="float32",
                callback=self.audio_callback,
            )
            self.stream.start()

        if self.model is None:
            # Safe to clear: on_press bails while _is_busy, so no transcription
            # thread is waiting on the event at this point.
            self._model_ready.clear()
            threading.Thread(target=self._ensure_model, daemon=True).start()

    def _ensure_model(self):
        with self._model_lock:
            try:
                if self.model is None:
                    try:
                        print("Attempting to load model on GPU...")
                        self.model = self._new_model("cuda", "float16")
                        print("GPU loaded successfully.")
                    except Exception as e:
                        print(f"GPU load failed ({e}). Falling back to CPU...")
                        self.model = self._new_model("cpu", "int8")
                        print("CPU loaded successfully.")
            except Exception as e:
                print(f"Model load failed: {e}")
            finally:
                # Always signal, including on failure: transcribe_and_type
                # waits on this and would otherwise hang with _is_busy stuck.
                self._model_ready.set()

    @staticmethod
    def _new_model(device, compute_type):
        try:
            # Keeps the Hugging Face Hub out of the hot path: without this every
            # load makes a network round trip to check for a newer revision.
            return WhisperModel(
                MODEL_SIZE, device=device, compute_type=compute_type,
                local_files_only=True,
            )
        except Exception:
            # Not cached yet (first run, or MODEL_SIZE changed) - fetch it.
            return WhisperModel(MODEL_SIZE, device=device, compute_type=compute_type)

    def _schedule_idle_release(self):
        self._cancel_idle_release()
        self._idle_job = self.root.after(IDLE_RELEASE_SEC * 1000, self._release_resources)

    def _cancel_idle_release(self):
        if self._idle_job:
            self.root.after_cancel(self._idle_job)
            self._idle_job = None

    def _release_resources(self):
        """Give back the VRAM and the microphone. Runs on the Tk thread."""
        self._idle_job = None
        if self.key_pressed or self._is_busy:
            # Raced with a new press; the next 'done' reschedules us.
            return

        if self.stream is not None:
            self.stream.stop()
            self.stream.close()
            self.stream = None

        with self._model_lock:
            self.model = None
            self._model_ready.clear()
        gc.collect()
        print("Released model and microphone.")

    # ---------------------------------------------------------------- audio / keys

    def audio_callback(self, indata, frames, time, status):
        if status:
            print(status)
        if self.is_recording:
            self.audio_buffer.append(indata.copy())

    @staticmethod
    def _open_keyboards():
        """Every input device that can report the push-to-talk key.

        Enumerated rather than hard-coded: there is a built-in keyboard and a
        wireless receiver here, and the receiver comes and goes.
        """
        devices = []
        for path in evdev.list_devices():
            try:
                dev = evdev.InputDevice(path)
            except OSError:
                continue  # not readable by us
            is_ours = dev.name == UINPUT_NAME
            if not is_ours and PUSH_TO_TALK_CODE in dev.capabilities().get(ecodes.EV_KEY, ()):
                devices.append(dev)
            else:
                dev.close()
        return devices

    def _listen_loop(self):
        while self._listening:
            devices = self._open_keyboards()
            if not devices:
                print(
                    "No readable keyboard devices. Is this user in the 'input' "
                    "group, and has the session been restarted since it was added?"
                )
                time.sleep(5)
                continue

            print("Watching: " + ", ".join(f"{d.path} ({d.name})" for d in devices))
            by_fd = {d.fd: d for d in devices}
            try:
                while self._listening:
                    readable, _, _ = select.select(by_fd, [], [], 1.0)
                    for fd in readable:
                        for event in by_fd[fd].read():
                            self._handle_key(event)
            except OSError as e:
                # A device vanished (receiver unplugged, suspend/resume).
                # Re-enumerate rather than letting the hotkey die silently.
                print(f"Input device went away ({e}); rescanning.")
            finally:
                for dev in devices:
                    try:
                        dev.close()
                    except OSError:
                        pass

    def _handle_key(self, event):
        if event.type != ecodes.EV_KEY or event.code != PUSH_TO_TALK_CODE:
            return
        if event.value == 1:
            self.on_press()
        elif event.value == 0:
            self.on_release()
        # value == 2 is autorepeat while held; ignore it or every repeat would
        # restart the recording.

    def on_press(self):
        if not self.key_pressed and not self._is_busy:
            self.key_pressed = True
            self.audio_buffer = []
            self._acquire()
            self.is_recording = True
            self.root.after(0, lambda: self._set_state("recording"))

    def on_release(self):
        if self.key_pressed:
            self.key_pressed = False
            self.is_recording = False
            self._is_busy = True
            threading.Thread(target=self.transcribe_and_type, daemon=True).start()

    # ---------------------------------------------------------------- output

    def _paste(self, text):
        """Put the text on the clipboard and send one paste chord.

        Going through the clipboard means no character ever has to be mapped to
        a scancode, so diacritics and any other non-layout character survive
        intact. Only the chord itself is synthesised, and KEY_V sits in the same
        physical position in cz+qwerty as it does in US layouts.
        """
        previous = None
        if RESTORE_CLIPBOARD:
            previous = subprocess.run(
                ["wl-paste", "--no-newline"], capture_output=True
            ).stdout

        # wl-copy forks a daemon that owns the selection until it is replaced.
        # That daemon inherits our stdout/stderr, so hand it /dev/null rather
        # than leaving it holding the journal pipe open for its whole life.
        subprocess.run(
            ["wl-copy"], input=text.encode(), check=True,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )

        for code in PASTE_CHORD:
            self._uinput.write(ecodes.EV_KEY, code, 1)
        self._uinput.syn()
        for code in reversed(PASTE_CHORD):
            self._uinput.write(ecodes.EV_KEY, code, 0)
        self._uinput.syn()

        if previous is not None:
            # The paste is asynchronous; overwrite the clipboard too soon and
            # the target window reads back the old contents.
            time.sleep(0.5)
            subprocess.run(
                ["wl-copy"], input=previous, check=False,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )

    def _abort(self):
        """Return to idle without typing anything."""
        self.root.after(0, lambda: self._set_state("ready"))
        self.root.after(0, self._schedule_idle_release)

    def transcribe_and_type(self):
        try:
            self._transcribe_and_type()
        except Exception as e:
            # _is_busy is only cleared by the "ready" state, and on_press bails
            # while it is set, so an escaping exception would wedge the hotkey
            # for the lifetime of the service.
            print(f"Transcription failed: {e}")
            self._abort()

    def _transcribe_and_type(self):
        if not self.audio_buffer:
            self._abort()
            return

        audio_data = np.concatenate(self.audio_buffer).flatten()
        if len(audio_data) < SAMPLE_RATE * MIN_AUDIO_SEC:
            self._abort()
            return

        if not self._model_ready.is_set():
            self.root.after(0, lambda: self._set_state("loading"))
            self._model_ready.wait()

        with self._model_lock:
            model = self.model
        if model is None:
            self._abort()
            return

        self.root.after(0, lambda: self._set_state("transcribing"))

        segments, _ = model.transcribe(audio_data, beam_size=5)

        accumulated = ""
        for segment in segments:
            accumulated += segment.text
            preview = accumulated.strip()
            self.root.after(0, lambda t=preview: self._set_state("transcribing", t))

        final_text = accumulated.strip()

        if final_text:
            self._paste(final_text + " ")
            print(f"Pasted: {final_text}")

        self.root.after(0, lambda t=final_text: self._set_state("done", t))

    # ---------------------------------------------------------------- cleanup

    def on_close(self):
        self._cancel_idle_release()
        self._listening = False  # the listen loop is a daemon; let it unwind
        if self.stream is not None:
            self.stream.stop()
            self.stream.close()
            self.stream = None
        if self._uinput is not None:
            self._uinput.close()
            self._uinput = None
        self.root.destroy()


if __name__ == "__main__":
    root = tk.Tk()
    app = DictationApp(root)
    root.mainloop()
