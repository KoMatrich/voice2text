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
PUSH_TO_TALK_CODE = ecodes.KEY_RIGHTSHIFT
PASTE_CHORD = (ecodes.KEY_LEFTCTRL, ecodes.KEY_V)
RESTORE_CLIPBOARD = False
AUTO_PASTE = False
UINPUT_NAME = "voice2text"
MODEL_SIZE = "large-v3-turbo"
LANGUAGE = "en"
SAMPLE_RATE = 16000
MIN_AUDIO_SEC = 1.5
CANCEL_FLASH_MS = 500
ERROR_FLASH_MS = 3000
TIMER_TICK_MS = 100
IDLE_RELEASE_SEC = 10

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
        self._cancelled_by_key = False
        self.audio_buffer = []
        self._pulse_state = False
        self._pulse_job = None
        self._hide_job = None
        self._idle_job = None
        self._timer_job = None
        self._record_started = None
        self._is_busy = False

        self._model_lock = threading.Lock()
        self._model_ready = threading.Event()
        self._model_error = None

        self._uinput = evdev.UInput(name=UINPUT_NAME) if AUTO_PASTE else None

        self._set_state("ready")
        self._listening = True
        self.listener = threading.Thread(target=self._listen_loop, daemon=True)
        self.listener.start()

    # ------------------------------------------------------------------ UI build

    def _build_ui(self):
        self.main_frame = tk.Frame(self.root, bg=BG_COLOR)
        self.main_frame.pack(fill="both", expand=True, padx=14, pady=10)
        self.main_frame.columnconfigure(1, weight=1)

        self.dot_canvas = tk.Canvas(
            self.main_frame, width=14, height=14,
            bg=BG_COLOR, highlightthickness=0,
        )
        self.dot_canvas.grid(row=0, column=0, padx=(0, 8), pady=(2, 0), sticky="n")
        self.dot_item = self.dot_canvas.create_oval(2, 2, 12, 12, fill=ACCENT_IDLE, outline="")

        self.status_label = tk.Label(
            self.main_frame, text="",
            font=("Helvetica", 12, "bold"),
            fg=TEXT_COLOR, bg=BG_COLOR, anchor="w",
        )
        self.status_label.grid(row=0, column=1, sticky="ew")

        key_name = ecodes.KEY[PUSH_TO_TALK_CODE].replace("KEY_", "")
        self.key_label = tk.Label(
            self.main_frame, text=f"[{key_name}]",
            font=("Helvetica", 9),
            fg=SUBTEXT_COLOR, bg=BG_COLOR, anchor="e",
        )
        self.key_label.grid(row=0, column=2, sticky="e", padx=(4, 0))

        self.text_label = tk.Label(
            self.main_frame, text="",
            font=("Helvetica", 10),
            fg=SUBTEXT_COLOR, bg=BG_COLOR,
            wraplength=WIN_W - 28, justify="left", anchor="w",
        )
        self.text_label.grid(row=1, column=0, columnspan=3, sticky="ew", pady=(4, 0))

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

    # ---------------------------------------------------------------- recording clock

    def _start_timer(self):
        self._record_started = time.monotonic()
        self._tick_timer()

    def _tick_timer(self):
        if self._record_started is None:
            return
        elapsed = time.monotonic() - self._record_started
        self.status_label.config(
            text=f"{int(elapsed // 60)}:{elapsed % 60:04.1f}", fg=ACCENT_REC
        )
        self._timer_job = self.root.after(TIMER_TICK_MS, self._tick_timer)

    def _stop_timer(self):
        self._record_started = None
        if self._timer_job:
            self.root.after_cancel(self._timer_job)
            self._timer_job = None

    # ---------------------------------------------------------------- state machine

    def _set_state(self, state: str, text: str = ""):
        if state == "ready":
            self._stop_pulse()
            self._stop_timer()
            self.dot_canvas.itemconfig(self.dot_item, fill=ACCENT_IDLE)
            self._hide_window()
            self._is_busy = False

        elif state == "recording":
            if self._hide_job:
                self.root.after_cancel(self._hide_job)
                self._hide_job = None
            self.text_label.config(text="")
            self._start_timer()
            self._start_pulse(ACCENT_REC, "#7a0000", 400)
            self._show_window()

        elif state == "loading":
            self._stop_timer()
            self.status_label.config(text="Loading model...", fg=ACCENT_PROC)
            self.text_label.config(text="")
            self._start_pulse(ACCENT_PROC, "#7a6500", 700)
            self._show_window()

        elif state == "transcribing":
            self._stop_timer()
            self.status_label.config(text="Transcribing...", fg=ACCENT_PROC)
            self._start_pulse(ACCENT_PROC, "#7a6500", 700)
            if text:
                self.text_label.config(text=text)

        elif state == "done":
            self._stop_pulse()
            self._stop_timer()
            self.dot_canvas.itemconfig(self.dot_item, fill=ACCENT_IDLE)
            done_label = "Pasted" if AUTO_PASTE else "Copied"
            self.status_label.config(text=done_label, fg=ACCENT_IDLE)
            self.text_label.config(text=text if text else "")
            self._hide_job = self.root.after(1400, lambda: self._set_state("ready"))
            self._schedule_idle_release()

        elif state == "canceled":
            self._stop_pulse()
            self._stop_timer()
            self.dot_canvas.itemconfig(self.dot_item, fill=ACCENT_REC)
            self.status_label.config(text="Canceled", fg=ACCENT_REC)
            self.text_label.config(text=text if text else "")
            self._is_busy = False
            self._show_window()
            self._hide_job = self.root.after(
                CANCEL_FLASH_MS, lambda: self._set_state("ready")
            )
            self._schedule_idle_release()

        elif state == "error":
            self._stop_pulse()
            self._stop_timer()
            self.dot_canvas.itemconfig(self.dot_item, fill=ACCENT_REC)
            self.status_label.config(text="Error", fg=ACCENT_REC)
            self.text_label.config(text=text if text else "")
            self._show_window()
            self._hide_job = self.root.after(
                ERROR_FLASH_MS, lambda: self._set_state("ready")
            )
            self._schedule_idle_release()

    # ---------------------------------------------------------------- acquire / release

    def _acquire(self):
        self.root.after(0, self._cancel_idle_release)

        if self.stream is None:
            self.stream = sd.InputStream(
                samplerate=SAMPLE_RATE, channels=1, dtype="float32",
                callback=self.audio_callback,
            )
            self.stream.start()

        if self.model is None:
            self._model_ready.clear()
            threading.Thread(target=self._ensure_model, daemon=True).start()

    def _ensure_model(self):
        with self._model_lock:
            try:
                if self.model is None:
                    self._model_error = None
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
                self._model_error = str(e)
            finally:
                self._model_ready.set()

    @staticmethod
    def _new_model(device, compute_type):
        try:
            return WhisperModel(
                MODEL_SIZE, device=device, compute_type=compute_type,
                local_files_only=True,
            )
        except Exception:
            return WhisperModel(MODEL_SIZE, device=device, compute_type=compute_type)

    def _schedule_idle_release(self):
        self._cancel_idle_release()
        self._idle_job = self.root.after(IDLE_RELEASE_SEC * 1000, self._release_resources)

    def _cancel_idle_release(self):
        if self._idle_job:
            self.root.after_cancel(self._idle_job)
            self._idle_job = None

    def _release_resources(self):
        self._idle_job = None
        if self.key_pressed or self._is_busy:
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
        devices = []
        for path in evdev.list_devices():
            try:
                dev = evdev.InputDevice(path)
            except OSError:
                continue
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
                print(f"Input device went away ({e}); rescanning.")
            finally:
                for dev in devices:
                    try:
                        dev.close()
                    except OSError:
                        pass

    def _handle_key(self, event):
        if event.type != ecodes.EV_KEY:
            return

        # If any other key is pressed down while recording, cancel the dictation
        if self.key_pressed and event.code != PUSH_TO_TALK_CODE and event.value == 1:
            self._cancelled_by_key = True
            self.is_recording = False
            self.audio_buffer = []
            self.root.after(0, lambda: self._set_state("canceled", "interrupted by key"))
            return

        if event.code != PUSH_TO_TALK_CODE:
            return

        if event.value == 1:
            self.on_press()
        elif event.value == 0:
            self.on_release()

    def on_press(self):
        if not self.key_pressed and not self._is_busy:
            self.key_pressed = True
            self._cancelled_by_key = False
            self.audio_buffer = []
            self._acquire()
            self.is_recording = True
            self.root.after(0, lambda: self._set_state("recording"))

    def on_release(self):
        if self.key_pressed:
            self.key_pressed = False
            self.is_recording = False
            if self._cancelled_by_key:
                return  # Skip transcription since recording was interrupted
            self._is_busy = True
            threading.Thread(target=self.transcribe_and_type, daemon=True).start()

    # ---------------------------------------------------------------- output

    def _paste(self, text):
        previous = None
        if AUTO_PASTE and RESTORE_CLIPBOARD:
            previous = subprocess.run(
                ["wl-paste", "--no-newline"], capture_output=True
            ).stdout

        subprocess.run(
            ["wl-copy"], input=text.encode(), check=True,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )

        if not AUTO_PASTE:
            return

        for code in PASTE_CHORD:
            self._uinput.write(ecodes.EV_KEY, code, 1)
        self._uinput.syn()
        for code in reversed(PASTE_CHORD):
            self._uinput.write(ecodes.EV_KEY, code, 0)
        self._uinput.syn()

        if previous is not None:
            time.sleep(0.5)
            subprocess.run(
                ["wl-copy"], input=previous, check=False,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )

    def _abort(self, state="canceled", text=""):
        self.root.after(0, lambda: self._set_state(state, text))
        self.root.after(0, self._schedule_idle_release)

    def transcribe_and_type(self):
        try:
            self._transcribe_and_type()
        except Exception as e:
            print(f"Transcription failed: {e}")
            self._abort("error", str(e))

    def _transcribe_and_type(self):
        if not self.audio_buffer:
            self._abort("canceled", "no audio captured")
            return

        audio_data = np.concatenate(self.audio_buffer).flatten()
        duration = len(audio_data) / SAMPLE_RATE
        if duration < MIN_AUDIO_SEC:
            self._abort("canceled", f"{duration:.1f}s < {MIN_AUDIO_SEC}s minimum")
            return

        if not self._model_ready.is_set():
            self.root.after(0, lambda: self._set_state("loading"))
            self._model_ready.wait()

        with self._model_lock:
            model = self.model
        if model is None:
            self._abort("error", self._model_error or f"no model '{MODEL_SIZE}'")
            return

        self.root.after(0, lambda: self._set_state("transcribing"))

        segments, _ = model.transcribe(
            audio_data, beam_size=5, language=LANGUAGE, vad_filter=True,
        )

        accumulated = ""
        for segment in segments:
            accumulated += segment.text
            preview = accumulated.strip()
            self.root.after(0, lambda t=preview: self._set_state("transcribing", t))

        final_text = accumulated.strip()

        if not final_text:
            self._abort("canceled", "no speech detected")
            return

        self._paste(final_text + " ")
        print(f"{'Pasted' if AUTO_PASTE else 'Copied'}: {final_text}")

        self.root.after(0, lambda t=final_text: self._set_state("done", t))

    # ---------------------------------------------------------------- cleanup

    def on_close(self):
        self._cancel_idle_release()
        self._listening = False
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