"""Распознавание речи локальной моделью faster-whisper.

Локально — потому что микрофон слушает постоянно, и гонять весь этот поток
в облако ради слова-активатора незачем.
"""

from __future__ import annotations


class Transcriber:
    def __init__(self, backend: str = "auto", model: str = "small", language: str = "ru") -> None:
        self.language = language
        self.backend = backend if backend != "auto" else _detect()
        self._model = None
        if self.backend == "faster-whisper":
            self._model = _load(model)
            if self._model is None:
                self.backend = "none"

    @property
    def available(self) -> bool:
        return self.backend != "none"

    def transcribe(self, audio) -> str:
        """Превращает моно-сигнал float32 (16 кГц) в текст."""
        if self._model is None:
            return ""
        segments, _ = self._model.transcribe(
            audio,
            language=self.language or None,
            vad_filter=True,
            beam_size=1,
        )
        return " ".join(segment.text.strip() for segment in segments).strip()


def _detect() -> str:
    try:
        import faster_whisper  # noqa: F401
        return "faster-whisper"
    except ImportError:
        return "none"


def _load(model: str):
    try:
        from faster_whisper import WhisperModel

        return WhisperModel(model, device="auto", compute_type="int8")
    except Exception:
        return None
