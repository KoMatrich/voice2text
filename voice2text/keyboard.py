"""Reads keys straight from /dev/input.

Under GNOME Wayland an X11 listener only sees keys while an X11 window has
focus, which is almost never, so this bypasses X11 entirely (see CLAUDE.md).
Requires the user in the `input` group.
"""
import select
import threading
import time

import evdev
from evdev import ecodes

from .config import UINPUT_NAME


def open_keyboards(key):
    """Every readable device that can produce `key`, except our own virtual
    keyboard. Enumerated on each scan: the wireless receiver comes and goes."""
    devices = []
    for path in evdev.list_devices():
        try:
            dev = evdev.InputDevice(path)
        except OSError:
            continue
        if dev.name != UINPUT_NAME and key in dev.capabilities().get(ecodes.EV_KEY, ()):
            devices.append(dev)
        else:
            dev.close()
    return devices


class KeyListener:
    """Feeds every key event from every keyboard into a PushToTalk gesture.

    The gesture lives entirely on this thread, and its arm delay is simply the
    select() timeout, so no timer thread and no locking are needed. Whatever
    the gesture reports must be handed to the Tk thread by its callbacks.
    """

    def __init__(self, gesture):
        self._gesture = gesture
        self._running = False
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self._running = True
        self._thread.start()

    def stop(self):
        self._running = False

    def _run(self):
        while self._running:
            devices = open_keyboards(self._gesture.key)
            if not devices:
                print(
                    "No readable keyboard devices. Is this user in the 'input' "
                    "group, and has the session been restarted since it was added?"
                )
                time.sleep(5)
                continue

            print("Watching: " + ", ".join(f"{d.path} ({d.name})" for d in devices))
            try:
                self._pump({d.fd: d for d in devices})
            except OSError as e:
                print(f"Input device went away ({e}); rescanning.")
                # The release may have gone with it; don't record forever.
                self._gesture.reset("input device lost")
            finally:
                for dev in devices:
                    try:
                        dev.close()
                    except OSError:
                        pass

    def _pump(self, by_fd):
        gesture = self._gesture
        while self._running:
            timeout = gesture.timeout(time.monotonic())
            readable, _, _ = select.select(
                by_fd, [], [], 1.0 if timeout is None else timeout
            )
            for fd in readable:
                for event in by_fd[fd].read():
                    if event.type == ecodes.EV_KEY:
                        gesture.feed(event.code, event.value, time.monotonic())
            # After the events, so a key that arrives together with the
            # deadline still counts as inside the window.
            gesture.poll(time.monotonic())
