"""Wires the pieces together.

Every DictationApp method runs on the Tk thread, so its state needs no lock.
The key listener and the transcription thread reach it only through `_post`.
"""
import threading
import tkinter as tk

from evdev import ecodes

from .audio import Recorder
from .config import (
    ARM_DELAY_MS, AUTO_PASTE, IDLE_RELEASE_SEC, LANGUAGE, MIN_AUDIO_SEC,
    MODEL_SIZE, PUSH_TO_TALK_CODE, SAMPLE_RATE,
)
from .gesture import PushToTalk
from .keyboard import KeyListener
from .model import ModelLoader
from .output import Output
from .overlay import Overlay


class DictationApp:
    def __init__(self, root):
        self.root = root
        key_name = ecodes.KEY[PUSH_TO_TALK_CODE].replace("KEY_", "")
        self.overlay = Overlay(root, key_name, on_quit=self.close)
        self.recorder = Recorder(SAMPLE_RATE)
        self.models = ModelLoader(MODEL_SIZE)
        self.output = Output()

        self._recording = False
        self._busy = False  # a transcription is in flight
        self._idle_job = None

        # Nothing is acquired up front -- no model, no audio stream. Only the
        # listener, so push-to-talk is live as soon as the imports finish.
        gesture = PushToTalk(
            PUSH_TO_TALK_CODE, ARM_DELAY_MS / 1000,
            on_start=lambda: self._post(self._start_recording),
            on_stop=lambda: self._post(self._stop_recording),
            on_cancel=lambda reason: self._post(self._cancel_recording, reason),
        )
        self.listener = KeyListener(gesture)
        self.listener.start()

    def _post(self, fn, *args):
        """Run fn on the Tk thread: the only safe way in from any other."""
        self.root.after(0, fn, *args)

    # --------------------------------------------------------------- recording

    def _start_recording(self):
        if self._busy or self._recording:
            return
        self._cancel_idle_release()
        try:
            self.recorder.open()
        except Exception as e:
            print(f"Could not open the microphone: {e}")
            self._finish("error", f"microphone: {e}")
            return
        # Only now, past the arm delay, is the press known to be dictation.
        # The ~1.7 s load then overlaps with the user speaking.
        self.models.preload()
        self.recorder.start()
        self._recording = True
        self.overlay.show("recording")

    def _stop_recording(self):
        if not self._recording:
            return
        self._recording = False
        audio = self.recorder.stop()
        self._busy = True
        threading.Thread(target=self._transcribe, args=(audio,), daemon=True).start()

    def _cancel_recording(self, reason):
        if not self._recording:
            return
        self._recording = False
        self.recorder.stop()
        self._finish("canceled", reason)

    def _finish(self, state, text=""):
        """Enter a terminal state: done, canceled or error."""
        self._busy = False
        self.overlay.show(state, text)
        self._schedule_idle_release()

    # ----------------------------------------------------------- transcription

    def _transcribe(self, audio):
        """Runs on its own thread."""
        try:
            state, text = self._transcribe_clip(audio)
        except Exception as e:
            print(f"Transcription failed: {e}")
            state, text = "error", str(e)
        self._post(self._finish, state, text)

    def _transcribe_clip(self, audio):
        """Returns the terminal (state, text) for this clip."""
        if len(audio) == 0:
            return "canceled", "no audio captured"
        duration = len(audio) / SAMPLE_RATE
        if duration < MIN_AUDIO_SEC:
            return "canceled", f"{duration:.1f}s < {MIN_AUDIO_SEC}s minimum"

        if not self.models.ready:
            self._post(self.overlay.show, "loading")
        model, error = self.models.wait()
        if model is None:
            return "error", error or f"no model '{MODEL_SIZE}'"

        self._post(self.overlay.show, "transcribing")
        segments, _ = model.transcribe(
            audio, beam_size=5, language=LANGUAGE, vad_filter=True,
        )
        text = ""
        for segment in segments:
            text += segment.text
            self._post(self.overlay.show, "transcribing", text.strip())
        text = text.strip()
        if not text:
            return "canceled", "no speech detected"

        self.output.deliver(text + " ")
        print(f"{'Pasted' if AUTO_PASTE else 'Copied'}: {text}")
        return "done", text

    # ------------------------------------------------------------ idle release

    def _schedule_idle_release(self):
        self._cancel_idle_release()
        self._idle_job = self.root.after(IDLE_RELEASE_SEC * 1000, self._release_resources)

    def _cancel_idle_release(self):
        if self._idle_job:
            self.root.after_cancel(self._idle_job)
            self._idle_job = None

    def _release_resources(self):
        """Return the ~2.1 GB of VRAM and clear the "microphone in use" icon."""
        self._idle_job = None
        if self._recording or self._busy:
            return
        if not self.models.release():
            # Still loading (a first download takes minutes); try again later.
            self._schedule_idle_release()
            return
        self.recorder.close()
        print("Released model and microphone.")

    # ----------------------------------------------------------------- cleanup

    def close(self):
        self._cancel_idle_release()
        self.listener.stop()
        self.recorder.close()
        self.output.close()
        self.root.destroy()


def main():
    root = tk.Tk()
    DictationApp(root)
    root.mainloop()
