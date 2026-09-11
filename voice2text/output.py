"""Delivering the transcript: onto the clipboard, and optionally pasted.

Nothing is ever typed character by character, so no keymap is involved and
diacritics survive exactly.
"""
import subprocess
import time

import evdev
from evdev import ecodes

from .config import AUTO_PASTE, PASTE_CHORD, RESTORE_CLIPBOARD, UINPUT_NAME


class Output:
    def __init__(self):
        # Created once and held: a fresh uinput device takes a moment for the
        # compositor to notice, so one per paste would race the keystroke.
        self._uinput = evdev.UInput(name=UINPUT_NAME) if AUTO_PASTE else None

    def deliver(self, text):
        previous = None
        if self._uinput is not None and RESTORE_CLIPBOARD:
            previous = subprocess.run(
                ["wl-paste", "--no-newline"], capture_output=True
            ).stdout

        self._copy(text.encode(), check=True)
        if self._uinput is None:
            return

        for code in PASTE_CHORD:
            self._uinput.write(ecodes.EV_KEY, code, 1)
        self._uinput.syn()
        for code in reversed(PASTE_CHORD):
            self._uinput.write(ecodes.EV_KEY, code, 0)
        self._uinput.syn()

        if previous is not None:
            time.sleep(0.5)
            self._copy(previous, check=False)

    @staticmethod
    def _copy(data, check):
        subprocess.run(
            ["wl-copy"], input=data, check=check,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )

    def close(self):
        if self._uinput is not None:
            self._uinput.close()
            self._uinput = None
