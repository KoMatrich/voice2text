"""Microphone capture.

The stream is opened on demand and stays open between recordings until the
app releases it (closing it is what clears GNOME's "microphone in use"
indicator). Audio is only kept between start() and stop().
"""
import threading

import numpy as np
import sounddevice as sd


class Recorder:
    def __init__(self, sample_rate):
        self.sample_rate = sample_rate
        self._stream = None
        # Guards the chunk list against the PortAudio callback thread.
        self._lock = threading.Lock()
        self._chunks = []
        self._recording = False

    def open(self):
        if self._stream is None:
            stream = sd.InputStream(
                samplerate=self.sample_rate, channels=1, dtype="float32",
                callback=self._callback,
            )
            stream.start()
            self._stream = stream

    def close(self):
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None

    def start(self):
        with self._lock:
            self._chunks = []
            self._recording = True

    def stop(self):
        """Stop keeping audio and return what was captured, possibly empty."""
        with self._lock:
            self._recording = False
            chunks, self._chunks = self._chunks, []
        if not chunks:
            return np.zeros(0, dtype=np.float32)
        return np.concatenate(chunks).flatten()

    def _callback(self, indata, frames, time_info, status):
        if status:
            print(status)
        with self._lock:
            if self._recording:
                self._chunks.append(indata.copy())
