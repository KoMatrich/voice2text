import tkinter as tk
import threading
import sounddevice as sd
import numpy as np
from faster_whisper import WhisperModel
from pynput import keyboard

# --- CONFIGURATION ---
PUSH_TO_TALK_KEY = keyboard.Key.shift_r
MODEL_SIZE = "medium.en"  # Options: tiny, base, small, medium, large (and .en variants)
SAMPLE_RATE = 16000

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
        self.is_recording = False
        self.key_pressed = False
        self.audio_buffer = []
        self._pulse_state = False
        self._pulse_job = None
        self._hide_job = None
        self._is_busy = False
        self.keyboard_controller = keyboard.Controller()

        self._set_state("loading")
        threading.Thread(target=self.load_model, daemon=True).start()

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
        key_name = str(PUSH_TO_TALK_KEY).replace("Key.", "").upper()
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
        if state == "loading":
            self.status_label.config(text="Loading model...", fg=ACCENT_PROC)
            self.text_label.config(text="")
            self._start_pulse(ACCENT_PROC, "#7a6500", 700)
            self._show_window()

        elif state == "ready":
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

    # ---------------------------------------------------------------- model / audio

    def load_model(self):
        try:
            print("Attempting to load model on GPU...")
            self.model = WhisperModel(MODEL_SIZE, device="cuda", compute_type="float16")
            print("GPU loaded successfully.")
        except Exception as e:
            print(f"GPU load failed ({e}). Falling back to CPU...")
            self.model = WhisperModel(MODEL_SIZE, device="cpu", compute_type="int8")
            print("CPU loaded successfully.")

        self.root.after(0, lambda: self._set_state("ready"))

        self.listener = keyboard.Listener(on_press=self.on_press, on_release=self.on_release)
        self.listener.start()

        self.stream = sd.InputStream(
            samplerate=SAMPLE_RATE, channels=1, dtype="float32",
            callback=self.audio_callback,
        )
        self.stream.start()

    def audio_callback(self, indata, frames, time, status):
        if status:
            print(status)
        if self.is_recording:
            self.audio_buffer.append(indata.copy())

    def on_press(self, key):
        if key == PUSH_TO_TALK_KEY and not self.key_pressed and not self._is_busy:
            self.key_pressed = True
            self.is_recording = True
            self.audio_buffer = []
            self.root.after(0, lambda: self._set_state("recording"))

    def on_release(self, key):
        if key == PUSH_TO_TALK_KEY and self.key_pressed:
            self.key_pressed = False
            self.is_recording = False
            self._is_busy = True
            threading.Thread(target=self.transcribe_and_type, daemon=True).start()

    def transcribe_and_type(self):
        if not self.audio_buffer:
            self.root.after(0, lambda: self._set_state("ready"))
            return

        audio_data = np.concatenate(self.audio_buffer).flatten()
        if len(audio_data) < SAMPLE_RATE * 0.5:
            self.root.after(0, lambda: self._set_state("ready"))
            return

        self.root.after(0, lambda: self._set_state("transcribing"))

        segments, _ = self.model.transcribe(audio_data, beam_size=5)

        accumulated = ""
        for segment in segments:
            accumulated += segment.text
            preview = accumulated.strip()
            self.root.after(0, lambda t=preview: self._set_state("transcribing", t))

        final_text = accumulated.strip()

        if final_text:
            self.keyboard_controller.type(final_text + " ")
            print(f"Typed: {final_text}")

        self.root.after(0, lambda t=final_text: self._set_state("done", t))

    # ---------------------------------------------------------------- cleanup

    def on_close(self):
        if hasattr(self, "stream"):
            self.stream.stop()
            self.stream.close()
        if hasattr(self, "listener"):
            self.listener.stop()
        self.root.destroy()


if __name__ == "__main__":
    root = tk.Tk()
    app = DictationApp(root)
    root.mainloop()
