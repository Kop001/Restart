#!/bin/bash
# Готовит облачную сессию Claude Code: ставит зависимости, чтобы тесты и
# линтер работали сразу, без «сначала pip install».
set -euo pipefail

# На локальной машине окружение своё — не трогаем его.
if [ "${CLAUDE_CODE_REMOTE:-}" != "true" ]; then
  exit 0
fi

cd "$CLAUDE_PROJECT_DIR"

# -e '.[dev]' ставит и сам пакет, и anthropic, pytest, ruff.
# Именно install (не ci/--force): состояние контейнера кэшируется,
# и повторный запуск отрабатывает почти мгновенно.
# Сам pip не обновляем: в системном Python он от пакетного менеджера,
# и --upgrade ломается на попытке его снести.
python3 -m pip install --quiet -e '.[dev]'

# Чтобы `python3 -m jarvis` работал из любого каталога.
echo "export PYTHONPATH=\"$CLAUDE_PROJECT_DIR\"" >> "$CLAUDE_ENV_FILE"
