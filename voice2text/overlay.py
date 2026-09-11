"""The small always-on-top status window.

Visuals only: it knows nothing about audio or models. Like all Tk code it must
only be touched from the Tk thread.
"""
import time
import tkinter as tk

from .config import (
    ACCENT_IDLE, ACCENT_PROC, ACCENT_PROC_DIM, ACCENT_REC, ACCENT_REC_DIM,
    AUTO_PASTE, BG_COLOR, CANCEL_FLASH_MS, DONE_FLASH_MS, ERROR_FLASH_MS,
    SUBTEXT_COLOR, TEXT_COLOR, TIMER_TICK_MS, WIN_H, WIN_W,
)

# Terminal states: label, colour, and how long they linger before hiding.
_FLASHES = {
    "done":     ("Pasted" if AUTO_PASTE else "Copied", ACCENT_IDLE, DONE_FLASH_MS),
    "canceled": ("Canceled", ACCENT_REC, CANCEL_FLASH_MS),
    "error":    ("Error", ACCENT_REC, ERROR_FLASH_MS),
}


class Overlay:
    def __init__(self, root, key_name, on_quit):
        self.root = root
        # Three independent after() chains. Every show() stops all of them.
        self._pulse_job = None
        self._clock_job = None
        self._hide_job = None

        root.overrideredirect(True)
        root.attributes("-topmost", True)
        root.attributes("-alpha", 0.95)
        root.configure(bg=BG_COLOR)
        root.protocol("WM_DELETE_WINDOW", on_quit)

        root.update_idletasks()
        x = (root.winfo_screenwidth() - WIN_W) // 2
        y = root.winfo_screenheight() - WIN_H - 60
        root.geometry(f"{WIN_W}x{WIN_H}+{x}+{y}")

        menu = tk.Menu(
            root, tearoff=0, bg="#2c2c2e", fg=TEXT_COLOR,
            activebackground="#3a3a3c", activeforeground=TEXT_COLOR,
        )
        menu.add_command(label="Quit Voice2Text", command=on_quit)

        for widget in self._build(key_name):
            widget.bind("<ButtonPress-1>", self._drag_start)
            widget.bind("<B1-Motion>", self._drag_motion)
            widget.bind("<Button-3>", lambda e: menu.tk_popup(e.x_root, e.y_root))

        self.show("ready")

    def _build(self, key_name):
        frame = tk.Frame(self.root, bg=BG_COLOR)
        frame.pack(fill="both", expand=True, padx=14, pady=10)
        frame.columnconfigure(1, weight=1)

        self.dot_canvas = tk.Canvas(
            frame, width=14, height=14, bg=BG_COLOR, highlightthickness=0,
        )
        self.dot_canvas.grid(row=0, column=0, padx=(0, 8), pady=(2, 0), sticky="n")
        self.dot_item = self.dot_canvas.create_oval(2, 2, 12, 12, fill=ACCENT_IDLE, outline="")

        self.status_label = tk.Label(
            frame, text="", font=("Helvetica", 12, "bold"),
            fg=TEXT_COLOR, bg=BG_COLOR, anchor="w",
        )
        self.status_label.grid(row=0, column=1, sticky="ew")

        key_label = tk.Label(
            frame, text=f"[{key_name}]", font=("Helvetica", 9),
            fg=SUBTEXT_COLOR, bg=BG_COLOR, anchor="e",
        )
        key_label.grid(row=0, column=2, sticky="e", padx=(4, 0))

        self.text_label = tk.Label(
            frame, text="", font=("Helvetica", 10),
            fg=SUBTEXT_COLOR, bg=BG_COLOR,
            wraplength=WIN_W - 28, justify="left", anchor="w",
        )
        self.text_label.grid(row=1, column=0, columnspan=3, sticky="ew", pady=(4, 0))

        return (self.root, frame, self.dot_canvas,
                self.status_label, key_label, self.text_label)

    # ------------------------------------------------------------------ states

    def show(self, state, text=""):
        """The only path to a visual change.

        ready -> recording -> (loading) -> transcribing -> done/canceled/error,
        each terminal state lingering for a moment before returning to ready.
        """
        self._stop_jobs()

        if state == "ready":
            self._dot(ACCENT_IDLE)
            self.root.withdraw()
            return

        if state == "recording":
            self.text_label.config(text="")
            self._start_clock()
            self._start_pulse(ACCENT_REC, ACCENT_REC_DIM, 400)
        elif state == "loading":
            self.status_label.config(text="Loading model...", fg=ACCENT_PROC)
            self.text_label.config(text="")
            self._start_pulse(ACCENT_PROC, ACCENT_PROC_DIM, 700)
        elif state == "transcribing":
            self.status_label.config(text="Transcribing...", fg=ACCENT_PROC)
            self._start_pulse(ACCENT_PROC, ACCENT_PROC_DIM, 700)
            if text:
                self.text_label.config(text=text)
        elif state in _FLASHES:
            label, color, linger_ms = _FLASHES[state]
            self._dot(color)
            self.status_label.config(text=label, fg=color)
            self.text_label.config(text=text)
            self._hide_job = self.root.after(linger_ms, self._auto_hide)
        else:
            raise ValueError(f"unknown overlay state {state!r}")

        if not self.root.winfo_viewable():
            self.root.deiconify()
            self.root.lift()

    def _auto_hide(self):
        self._hide_job = None
        self.show("ready")

    def _dot(self, color):
        self.dot_canvas.itemconfig(self.dot_item, fill=color)

    # -------------------------------------------------------------- animations

    def _start_pulse(self, bright, dim, interval_ms):
        def step(lit):
            self._dot(bright if lit else dim)
            self._pulse_job = self.root.after(interval_ms, step, not lit)
        step(True)

    def _start_clock(self):
        started = time.monotonic()

        def tick():
            elapsed = time.monotonic() - started
            self.status_label.config(
                text=f"{int(elapsed // 60)}:{elapsed % 60:04.1f}", fg=ACCENT_REC
            )
            self._clock_job = self.root.after(TIMER_TICK_MS, tick)
        tick()

    def _stop_jobs(self):
        for job in (self._pulse_job, self._clock_job, self._hide_job):
            if job:
                self.root.after_cancel(job)
        self._pulse_job = self._clock_job = self._hide_job = None

    # -------------------------------------------------------------------- drag

    def _drag_start(self, event):
        self._drag_x = event.x_root - self.root.winfo_x()
        self._drag_y = event.y_root - self.root.winfo_y()

    def _drag_motion(self, event):
        x = event.x_root - self._drag_x
        y = event.y_root - self._drag_y
        self.root.geometry(f"+{x}+{y}")
