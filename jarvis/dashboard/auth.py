"""Доступ к панели по токену.

Пока панель слушала только 127.0.0.1, пароль был не нужен. Как только её
открывают в локальную сеть — чтобы зайти с телефона, — она превращается в
терминал для всех, кто в той же сети. Поэтому токен.

Токен постоянный: он лежит в каталоге состояния и переживает перезапуск,
иначе после каждого рестарта службы пришлось бы заново открывать ссылку.
"""

from __future__ import annotations

import hmac
import secrets
from http.cookies import SimpleCookie
from pathlib import Path
from urllib.parse import parse_qs

COOKIE = "jarvis_token"


def load_or_create_token(path: Path) -> str:
    """Читает токен из файла, а если его нет — заводит новый."""
    try:
        token = path.read_text(encoding="utf-8").strip()
        if token:
            return token
    except OSError:
        pass

    token = secrets.token_urlsafe(24)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(token, encoding="utf-8")
    # Читать токен может только владелец: файл лежит в домашнем каталоге,
    # но права по умолчанию слишком щедрые.
    path.chmod(0o600)
    return token


def token_from_request(query: str, cookie_header: str | None) -> str | None:
    """Достаёт токен из строки запроса (?token=...) или из cookie."""
    values = parse_qs(query).get("token")
    if values and values[0]:
        return values[0]
    if cookie_header:
        cookie = SimpleCookie()
        try:
            cookie.load(cookie_header)
        except Exception:
            return None
        morsel = cookie.get(COOKIE)
        if morsel is not None:
            return morsel.value
    return None


def matches(expected: str, given: str | None) -> bool:
    """Сравнение постоянного времени: обычное == подсказывает длину префикса."""
    if not given:
        return False
    return hmac.compare_digest(expected, given)
