"""Синтез речи. Бэкенд выбирается по тому, что реально есть на машине."""

from __future__ import annotations

import platform
import shutil
import subprocess
import sys


class Speaker:
    """Озвучка текста. Если синтезатора нет — печатает в терминал."""

    def __init__(self, backend: str = "auto", voice: str = "") -> None:
        self.voice = voice
        self.backend = backend if backend != "auto" else detect_backend()
        self._engine = None
        if self.backend == "pyttsx3":
            self._engine = _init_pyttsx3(voice)
            if self._engine is None:
                self.backend = "none"

    def say(self, text: str) -> None:
        text = (text or "").strip()
        if not text:
            return
        print(f"🤖 {text}", flush=True)
        if self.backend == "none":
            return
        try:
            self._speak(text)
        except Exception as exc:  # молчащий Джарвис лучше упавшего
            print(f"[tts: {exc}]", file=sys.stderr)

    def _speak(self, text: str) -> None:
        if self.backend == "say":
            cmd = ["say"] + (["-v", self.voice] if self.voice else []) + [text]
            subprocess.run(cmd, check=False)
        elif self.backend == "espeak":
            cmd = ["espeak-ng", "-v", self.voice or "ru", text]
            subprocess.run(cmd, check=False)
        elif self.backend == "piper":
            _piper(text, self.voice)
        elif self.backend == "pyttsx3" and self._engine is not None:
            self._engine.say(text)
            self._engine.runAndWait()


def detect_backend() -> str:
    if platform.system() == "Darwin" and shutil.which("say"):
        return "say"
    if shutil.which("piper"):
        return "piper"
    if shutil.which("espeak-ng"):
        return "espeak"
    try:
        import pyttsx3  # noqa: F401
        return "pyttsx3"
    except ImportError:
        return "none"


def _piper(text: str, model: str) -> None:
    player = next((p for p in ("aplay", "paplay", "afplay") if shutil.which(p)), None)
    if player is None:
        raise RuntimeError("нет проигрывателя для piper (aplay/paplay/afplay)")
    piper = subprocess.Popen(
        ["piper", "--model", model or "ru_RU-irina-medium", "--output_file", "-"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
    )
    audio, _ = piper.communicate(text.encode("utf-8"))
    subprocess.run([player, "-"], input=audio, check=False)


def _init_pyttsx3(voice: str):
    try:
        import pyttsx3

        engine = pyttsx3.init()
        if voice:
            engine.setProperty("voice", voice)
        return engine
    except Exception:
        return None
