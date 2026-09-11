"""Push-to-talk gesture recognition.

Free of threads, clocks and devices so it can be tested on its own: the caller
feeds it key events together with the current time, and calls `poll` whenever
`timeout` says the arm deadline is due.
"""

IDLE = "idle"
PENDING = "pending"  # key down, waiting out the arm delay
ACTIVE = "active"    # armed: the app is recording
SPOILED = "spoiled"  # another key got in; ignore everything until release

_UP, _DOWN, _REPEAT = 0, 1, 2


class PushToTalk:
    """Turns raw key events into on_start / on_stop / on_cancel calls.

    A press only becomes a recording once the key has been held alone for
    `arm_delay` seconds. Any other key before that marks it as ordinary Shift
    use and nothing is reported at all. Any other key after that cancels the
    recording, once.
    """

    def __init__(self, key, arm_delay, on_start, on_stop, on_cancel):
        self.key = key
        self.arm_delay = arm_delay
        self._on_start = on_start
        self._on_stop = on_stop
        self._on_cancel = on_cancel
        self.state = IDLE
        self._deadline = None

    def feed(self, code, value, now):
        if value == _REPEAT:
            return

        if code == self.key:
            if value == _DOWN and self.state == IDLE:
                self.state = PENDING
                self._deadline = now + self.arm_delay
            elif value == _UP:
                was = self.state
                self._to_idle()
                if was == ACTIVE:
                    self._on_stop()
            return

        # Only a press counts. A release may belong to a key that went down
        # before ours -- the tail of the previous word while typing fast.
        if value != _DOWN:
            return
        if self.state == PENDING:
            self.state = SPOILED
            self._deadline = None
        elif self.state == ACTIVE:
            self.state = SPOILED
            self._on_cancel("interrupted by key")

    def poll(self, now):
        if self.state == PENDING and now >= self._deadline:
            self.state = ACTIVE
            self._deadline = None
            self._on_start()

    def timeout(self, now):
        """Seconds until `poll` has work to do, or None if it never will."""
        if self.state != PENDING:
            return None
        return max(0.0, self._deadline - now)

    def reset(self, reason):
        """Forget the current press, e.g. because its device vanished and the
        release will never arrive."""
        was = self.state
        self._to_idle()
        if was == ACTIVE:
            self._on_cancel(reason)

    def _to_idle(self):
        self.state = IDLE
        self._deadline = None
