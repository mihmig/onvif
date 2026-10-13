#!/usr/bin/env bash
#
# Создаёт виртуальное окружение .venv в корне проекта и ставит в него
# зависимости из requirements.txt.
#
# Использование:
#   ./scripts/install.sh
#
# Переменные окружения:
#   PYTHON  — интерпретатор для создания окружения (по умолчанию python3)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
VENV_DIR="$PROJECT_DIR/.venv"
PYTHON="${PYTHON:-python3}"

# Минимальная версия: в коде используется синтаксис `bytes | str` (PEP 604)
MIN_PY_MAJOR=3
MIN_PY_MINOR=10

log() { printf '\n==> %s\n' "$*"; }
die() { printf '\nОшибка: %s\n' "$*" >&2; exit 1; }

command -v "$PYTHON" >/dev/null 2>&1 \
    || die "не найден интерпретатор '$PYTHON'. Установите Python ${MIN_PY_MAJOR}.${MIN_PY_MINOR}+ или задайте PYTHON=/путь/к/python3"

"$PYTHON" - "$MIN_PY_MAJOR" "$MIN_PY_MINOR" <<'PY' || die "нужен Python ${MIN_PY_MAJOR}.${MIN_PY_MINOR} или новее"
import sys
major, minor = int(sys.argv[1]), int(sys.argv[2])
sys.exit(0 if sys.version_info[:2] >= (major, minor) else 1)
PY

log "Проект: $PROJECT_DIR"
log "Интерпретатор: $("$PYTHON" -V 2>&1) ($(command -v "$PYTHON"))"

if [ -x "$VENV_DIR/bin/python" ]; then
    log "Окружение $VENV_DIR уже существует, используем его"
else
    log "Создаём окружение $VENV_DIR"
    "$PYTHON" -m venv "$VENV_DIR" \
        || die "не удалось создать venv. На Debian/Ubuntu поставьте пакет python3-venv"
fi

log "Обновляем pip"
"$VENV_DIR/bin/python" -m pip install --upgrade pip

log "Устанавливаем зависимости из requirements.txt"
"$VENV_DIR/bin/python" -m pip install -r "$PROJECT_DIR/requirements.txt"

if [ ! -f "$PROJECT_DIR/config/config.yml" ]; then
    printf '\nВнимание: нет файла config/config.yml — worker_max.py без него не запустится.\n'
    printf 'Создайте его по образцу из README/config.sample.yml.\n'
fi

log "Готово. Запуск: cd \"$PROJECT_DIR\" && .venv/bin/python worker.py"
