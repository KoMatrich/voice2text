"""Whisper model lifecycle: loaded in the background on demand, dropped when
idle. large-v3-turbo holds ~2.1 GB of a 4 GB card, so never load eagerly."""
import gc
import threading

from faster_whisper import WhisperModel


def _new_model(size, device, compute_type):
    # The cached copy if there is one; the network only when there isn't.
    try:
        return WhisperModel(
            size, device=device, compute_type=compute_type, local_files_only=True,
        )
    except Exception:
        return WhisperModel(size, device=device, compute_type=compute_type)


class ModelLoader:
    """Owns the one WhisperModel.

    The load runs outside the lock, so nothing blocks on it except `wait`.
    `_idle` is set whenever no load is in flight -- including after a failed
    one, or a waiter would hang forever.
    """

    def __init__(self, size):
        self.size = size
        self._lock = threading.Lock()
        self._model = None
        self._error = None
        self._loading = False
        self._idle = threading.Event()
        self._idle.set()

    @property
    def ready(self):
        with self._lock:
            return self._model is not None

    def preload(self):
        """Start loading in the background, unless loaded or already loading."""
        with self._lock:
            if self._model is not None or self._loading:
                return
            self._loading = True
            self._error = None
            self._idle.clear()
        threading.Thread(target=self._load, daemon=True).start()

    def _load(self):
        model, error = None, None
        try:
            try:
                print("Attempting to load model on GPU...")
                model = _new_model(self.size, "cuda", "float16")
                print("GPU loaded successfully.")
            except Exception as e:
                print(f"GPU load failed ({e}). Falling back to CPU...")
                model = _new_model(self.size, "cpu", "int8")
                print("CPU loaded successfully.")
        except Exception as e:
            print(f"Model load failed: {e}")
            error = str(e)
        finally:
            with self._lock:
                self._model, self._error, self._loading = model, error, False
                self._idle.set()

    def wait(self):
        """Block until no load is in flight; returns (model, error)."""
        self.preload()
        self._idle.wait()
        with self._lock:
            return self._model, self._error

    def release(self):
        """Drop the model. Returns False, dropping nothing, mid-load."""
        with self._lock:
            if self._loading:
                return False
            model, self._model = self._model, None
        if model is not None:
            del model
            gc.collect()
        return True
