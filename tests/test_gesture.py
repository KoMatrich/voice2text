import unittest

from voice2text.gesture import IDLE, PushToTalk

KEY, A, S = 54, 30, 31  # KEY_RIGHTSHIFT, KEY_A, KEY_S
UP, DOWN, REPEAT = 0, 1, 2
DELAY = 0.3


class PushToTalkTest(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.ptt = PushToTalk(
            KEY, DELAY,
            on_start=lambda: self.calls.append("start"),
            on_stop=lambda: self.calls.append("stop"),
            on_cancel=lambda reason: self.calls.append(("cancel", reason)),
        )

    def feed(self, code, value, now):
        """What the listener does for each event: feed, then poll."""
        self.ptt.feed(code, value, now)
        self.ptt.poll(now)

    def test_hold_past_the_delay_records(self):
        self.feed(KEY, DOWN, 0.0)
        self.ptt.poll(0.29)
        self.assertEqual(self.calls, [])
        self.ptt.poll(0.30)
        self.assertEqual(self.calls, ["start"])
        self.feed(KEY, UP, 2.0)
        self.assertEqual(self.calls, ["start", "stop"])

    def test_tap_inside_the_delay_is_silent(self):
        self.feed(KEY, DOWN, 0.0)
        self.feed(KEY, UP, 0.1)
        self.ptt.poll(1.0)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.ptt.state, IDLE)

    def test_typing_a_capital_is_silent(self):
        self.feed(KEY, DOWN, 0.0)
        self.feed(A, DOWN, 0.08)
        self.feed(A, UP, 0.15)
        self.ptt.poll(1.0)  # Shift still held well past the delay
        self.feed(KEY, UP, 1.2)
        self.assertEqual(self.calls, [])

    def test_key_at_the_deadline_still_counts_as_typing(self):
        self.feed(KEY, DOWN, 0.0)
        self.feed(A, DOWN, 0.3)
        self.ptt.poll(1.0)
        self.assertEqual(self.calls, [])

    def test_releasing_an_earlier_key_does_not_spoil(self):
        self.feed(A, DOWN, 0.0)
        self.feed(KEY, DOWN, 0.05)
        self.feed(A, UP, 0.1)
        self.ptt.poll(0.35)
        self.assertEqual(self.calls, ["start"])

    def test_other_key_while_recording_cancels_once(self):
        self.feed(KEY, DOWN, 0.0)
        self.ptt.poll(0.3)
        self.feed(A, DOWN, 1.0)
        self.feed(S, DOWN, 1.1)
        self.feed(KEY, UP, 1.5)
        self.assertEqual(self.calls, ["start", ("cancel", "interrupted by key")])

    def test_autorepeat_is_ignored(self):
        self.feed(KEY, DOWN, 0.0)
        self.feed(A, REPEAT, 0.1)  # a key held since before the press
        self.feed(KEY, REPEAT, 0.25)
        self.ptt.poll(0.3)
        self.feed(KEY, REPEAT, 0.5)
        self.assertEqual(self.calls, ["start"])

    def test_next_press_after_a_spoiled_one_works(self):
        self.feed(KEY, DOWN, 0.0)
        self.feed(A, DOWN, 0.1)
        self.feed(KEY, UP, 0.2)
        self.feed(KEY, DOWN, 2.0)
        self.ptt.poll(2.3)
        self.assertEqual(self.calls, ["start"])

    def test_timeout_tracks_the_arm_deadline(self):
        self.assertIsNone(self.ptt.timeout(0.0))
        self.feed(KEY, DOWN, 1.0)
        self.assertAlmostEqual(self.ptt.timeout(1.1), 0.2)
        self.assertEqual(self.ptt.timeout(5.0), 0.0)
        self.ptt.poll(1.3)
        self.assertIsNone(self.ptt.timeout(1.3))

    def test_reset_cancels_only_a_live_recording(self):
        self.feed(KEY, DOWN, 0.0)
        self.ptt.reset("input device lost")
        self.assertEqual(self.calls, [])

        self.feed(KEY, DOWN, 1.0)
        self.ptt.poll(1.3)
        self.ptt.reset("input device lost")
        self.feed(KEY, UP, 1.5)
        self.assertEqual(self.calls, ["start", ("cancel", "input device lost")])


if __name__ == "__main__":
    unittest.main()
