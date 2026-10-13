#!/usr/bin/env bash
#
# Ставит окружение (через install.sh), создаёт systemd-юнит для worker_max.py,
# включает автозапуск и запускает сервис.
#
# Использование:
#   sudo ./scripts/install_service.sh
#
# Переменные окружения:
#   SERVICE_NAME  — имя юнита без .service (по умолчанию onvif-worker)
#   SERVICE_USER  — от кого запускать сервис (по умолчанию вызвавший sudo пользователь)
#   PYTHON        — интерпретатор для создания окружения (см. install.sh)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
SERVICE_NAME="${SERVICE_NAME:-onvif-worker}"
SERVICE_USER="${SERVICE_USER:-${SUDO_USER:-$(id -un)}}"
UNIT_PATH="/etc/systemd/system/${SERVICE_NAME}.service"

log() { printf '\n==> %s\n' "$*"; }
die() { printf '\nОшибка: %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "запустите с правами root: sudo $0"
command -v systemctl >/dev/null 2>&1 || die "systemd не найден, установка сервиса невозможна"
id "$SERVICE_USER" >/dev/null 2>&1 || die "пользователь '$SERVICE_USER' не существует (задайте SERVICE_USER)"
[ "$SERVICE_USER" != "root" ] || printf '\nВнимание: сервис будет работать от root.\n'

SERVICE_GROUP="$(id -gn "$SERVICE_USER")"

# Окружение создаём от имени пользователя сервиса, чтобы .venv принадлежал ему
log "Устанавливаем окружение от имени пользователя $SERVICE_USER"
if [ "$SERVICE_USER" = "root" ]; then
    env PYTHON="${PYTHON:-python3}" bash "$SCRIPT_DIR/install.sh"
else
    sudo -u "$SERVICE_USER" env PYTHON="${PYTHON:-python3}" bash "$SCRIPT_DIR/install.sh"
fi

if [ -f "$UNIT_PATH" ]; then
    BACKUP="$UNIT_PATH.bak.$(date +%Y%m%d%H%M%S)"
    log "Юнит уже есть, сохраняем прежний в $BACKUP"
    cp -p "$UNIT_PATH" "$BACKUP"
fi

log "Пишем юнит $UNIT_PATH"
cat > "$UNIT_PATH" <<UNIT
[Unit]
Description=ONVIF camera worker ($SERVICE_NAME)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$SERVICE_USER
Group=$SERVICE_GROUP
WorkingDirectory=$PROJECT_DIR
Environment=PYTHONUNBUFFERED=1
ExecStart=$PROJECT_DIR/.venv/bin/python worker.py
# worker.py корректно завершается по KeyboardInterrupt: убирает клавиатуру у бота
KillSignal=SIGINT
TimeoutStopSec=30
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
UNIT

log "Перечитываем конфигурацию systemd и запускаем сервис"
systemctl daemon-reload
systemctl enable "$SERVICE_NAME"
systemctl restart "$SERVICE_NAME"

sleep 2
systemctl --no-pager --full status "$SERVICE_NAME" || true

cat <<INFO

==> Готово. Полезные команды:
    systemctl status $SERVICE_NAME
    systemctl restart $SERVICE_NAME
    systemctl stop $SERVICE_NAME
    journalctl -u $SERVICE_NAME -f
INFO
