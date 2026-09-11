"""Everything worth tweaking. See the table in CLAUDE.md."""
from evdev import ecodes

# --- INPUT ---
PUSH_TO_TALK_CODE = ecodes.KEY_RIGHTSHIFT
# The key has to be held on its own this long before anything happens. Another
# key pressed inside the window means it is being used as Shift -- a capital
# letter, a shortcut -- so the press is dropped silently: no overlay, no
# microphone, no model load. Speech inside the window is not recorded; the
# overlay appearing is the cue to start talking.
ARM_DELAY_MS = 300

# --- OUTPUT ---
# Terminals want (KEY_LEFTCTRL, KEY_LEFTSHIFT, KEY_V) instead.
PASTE_CHORD = (ecodes.KEY_LEFTCTRL, ecodes.KEY_V)
RESTORE_CLIPBOARD = False  # restoring races the paste; opt in
# Off by default: a synthesized paste lands in whatever window has focus when
# transcription finishes, not necessarily the one the user meant.
AUTO_PASTE = False
# Our own virtual keyboard advertises every key code; the listener skips it by
# name.
UINPUT_NAME = "voice2text"

# --- TRANSCRIPTION ---
# Must be a name faster-whisper knows, or the GPU and CPU loads both fail.
MODEL_SIZE = "large-v3-turbo"
# large-v3-turbo's language detection is close to a coin flip on a few seconds
# of dictation, so pin it. None restores auto-detection; "cs" dictates Czech.
LANGUAGE = "en"
SAMPLE_RATE = 16000
MIN_AUDIO_SEC = 1.5
IDLE_RELEASE_SEC = 10

# --- OVERLAY ---
DONE_FLASH_MS = 1400
CANCEL_FLASH_MS = 500
ERROR_FLASH_MS = 3000
TIMER_TICK_MS = 100
WIN_W = 270
WIN_H = 50

# --- THEME ---
BG_COLOR        = "#1c1c1e"
TEXT_COLOR      = "#f5f5f7"
SUBTEXT_COLOR   = "#8e8e93"
ACCENT_IDLE     = "#30d158"
ACCENT_REC      = "#ff453a"
ACCENT_REC_DIM  = "#7a0000"
ACCENT_PROC     = "#ffd60a"
ACCENT_PROC_DIM = "#7a6500"
