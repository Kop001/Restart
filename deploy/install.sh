#!/bin/bash
# Ставит Джарвиса службой: работает всегда, поднимается после перезагрузки,
# перезапускается сам при падении.
#
# Панель слушает только 127.0.0.1 — с этой машины и ниоткуда больше. Открыть её
# в локальную сеть можно флагом --lan, но это осознанное решение владельца:
# по умолчанию наружу не смотрит ничего.
set -euo pipefail

cd "$(dirname "$0")/.."
JARVIS="$(command -v jarvis || echo "$HOME/.local/bin/jarvis")"

if [ ! -x "$JARVIS" ]; then
  echo "Сначала установите пакет: pip install --user -e ." >&2
  exit 1
fi

if [ -z "${ANTHROPIC_API_KEY:-}" ]; then
  echo "Задайте ANTHROPIC_API_KEY перед установкой:" >&2
  echo "  ANTHROPIC_API_KEY=sk-ant-... $0" >&2
  exit 1
fi

case "$(uname -s)" in
  Linux)
    mkdir -p "$HOME/.config/systemd/user" "$HOME/.config/jarvis"
    # Ключ отдельным файлом, читать может только владелец.
    printf 'ANTHROPIC_API_KEY=%s\n' "$ANTHROPIC_API_KEY" > "$HOME/.config/jarvis/env"
    chmod 600 "$HOME/.config/jarvis/env"
    cp deploy/jarvis.service "$HOME/.config/systemd/user/jarvis.service"
    systemctl --user daemon-reload
    systemctl --user enable --now jarvis.service
    # Без этого служба умирает при выходе из сессии.
    loginctl enable-linger "$USER" 2>/dev/null || true
    echo "Готово. Логи: journalctl --user -u jarvis -f"
    ;;
  Darwin)
    PLIST="$HOME/Library/LaunchAgents/com.jarvis.panel.plist"
    mkdir -p "$HOME/Library/LaunchAgents"
    sed -e "s|__JARVIS__|$JARVIS|" \
        -e "s|__KEY__|$ANTHROPIC_API_KEY|" \
        -e "s|__HOME__|$HOME|g" \
        deploy/com.jarvis.panel.plist > "$PLIST"
    chmod 600 "$PLIST"
    launchctl unload "$PLIST" 2>/dev/null || true
    launchctl load "$PLIST"
    echo "Готово. Логи: tail -f ~/Library/Logs/jarvis.log"
    ;;
  *)
    echo "Автоустановка есть для Linux и macOS." >&2
    echo "На Windows: запустите 'jarvis panel --lan' и добавьте в автозагрузку" >&2
    echo "через Планировщик заданий (триггер «При входе в систему»)." >&2
    exit 1
    ;;
esac

echo "Ссылку с токеном покажет: jarvis panel --no-open"
