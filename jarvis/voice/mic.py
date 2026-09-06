"""Захват звука с микрофона с простым детектором тишины.

Логика записи фразы: ждём, пока громкость превысит порог, пишем, и как только
тишина держится дольше silence_duration — считаем фразу законченной.
"""

from __future__ import annotations

SAMPLE_RATE = 16000
BLOCK = 1024


class MicrophoneUnavailable(RuntimeError):
    pass


class Microphone:
    def __init__(
        self,
        device: str = "",
        silence_threshold: float = 0.012,
        silence_duration: float = 1.2,
        max_phrase_seconds: float = 30.0,
    ) -> None:
        self.device = device or None
        self.silence_threshold = silence_threshold
        self.silence_duration = silence_duration
        self.max_phrase_seconds = max_phrase_seconds
        self._sd = None
        self._np = None

    def _backend(self):
        if self._sd is None:
            try:
                import numpy as np
                import sounddevice as sd
            except ImportError as exc:
                raise MicrophoneUnavailable(
                    "нужны sounddevice и numpy: pip install 'jarvis[voice]'"
                ) from exc
            self._sd, self._np = sd, np
        return self._sd, self._np

    @property
    def available(self) -> bool:
        try:
            self._backend()
            return True
        except MicrophoneUnavailable:
            return False

    def record_phrase(self) -> "object | None":
        """Пишет одну фразу и возвращает моно float32 или None, если была тишина."""
        sd, np = self._backend()
        chunks: list = []
        speaking = False
        silent_blocks = 0
        block_seconds = BLOCK / SAMPLE_RATE
        silence_limit = max(1, int(self.silence_duration / block_seconds))
        max_blocks = int(self.max_phrase_seconds / block_seconds)

        with sd.InputStream(
            samplerate=SAMPLE_RATE, channels=1, dtype="float32",
            blocksize=BLOCK, device=self.device,
        ) as stream:
            for _ in range(max_blocks):
                block, _overflow = stream.read(BLOCK)
                mono = block.reshape(-1)
                level = float(np.sqrt(np.mean(mono ** 2)))

                if level >= self.silence_threshold:
                    speaking = True
                    silent_blocks = 0
                    chunks.append(mono.copy())
                elif speaking:
                    silent_blocks += 1
                    chunks.append(mono.copy())
                    if silent_blocks >= silence_limit:
                        break

        if not speaking or not chunks:
            return None
        return np.concatenate(chunks)

    def listen_window(self, seconds: float = 2.0):
        """Пишет короткое окно фиксированной длины — для ловли слова-активатора."""
        sd, np = self._backend()
        frames = int(seconds * SAMPLE_RATE)
        audio = sd.rec(frames, samplerate=SAMPLE_RATE, channels=1,
                       dtype="float32", device=self.device)
        sd.wait()
        return audio.reshape(-1)
