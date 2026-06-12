#!/usr/bin/env bash
# Локальное/VPS-развёртывание Uzum_tools в едином окружении .venv на Python 3.12.
#
#   ./setup_bot.sh
#
# Для хостинга в контейнере (Pterodactyl/WispByte) этот скрипт НЕ нужен —
# см. DEPLOY.md: панель сама ставит requirements.txt и запускает `python bot.py`.
set -euo pipefail

VENV_DIR=".venv"
ENV_FILE=".env"
cd "$(dirname "$0")"

# --- 1. Жёстко требуем python3.12 ------------------------------------------- #
if ! command -v python3.12 >/dev/null 2>&1; then
    echo "❌ Не найден python3.12. Установите его (проект ориентирован строго на 3.12)." >&2
    echo "   Ubuntu/Debian:  sudo apt install python3.12 python3.12-venv" >&2
    exit 1
fi
PYBIN="python3.12"
echo "✅ Интерпретатор: $PYBIN ($($PYBIN --version 2>&1))"

# --- 2. Убираем старое окружение .venv_bot ---------------------------------- #
if [[ -d ".venv_bot" ]]; then
    echo "🧹 Удаляю устаревшее окружение .venv_bot…"
    rm -rf ".venv_bot"
fi

# --- 3. Создаём/пересоздаём единый .venv на python3.12 ----------------------- #
recreate=false
if [[ -d "$VENV_DIR" ]]; then
    if "$VENV_DIR/bin/python" --version 2>&1 | grep -q "Python 3.12"; then
        echo "📦 $VENV_DIR уже на Python 3.12 — переиспользую."
    else
        echo "♻️  $VENV_DIR не на 3.12 — пересоздаю."
        rm -rf "$VENV_DIR"
        recreate=true
    fi
else
    recreate=true
fi
if [[ "$recreate" == true ]]; then
    echo "📦 Создаю $VENV_DIR ($PYBIN)…"
    "$PYBIN" -m venv "$VENV_DIR"
fi

# --- 4. Активация + зависимости --------------------------------------------- #
# shellcheck disable=SC1091
source "$VENV_DIR/bin/activate"
echo "⬆️  Обновляю pip…"
python -m pip install --no-cache-dir --upgrade pip >/dev/null
echo "📥 Ставлю зависимости (aiogram, cryptography, openpyxl, SQLAlchemy, httpx)…"
# --no-cache-dir: не забиваем 1 ГБ SSD кэшем колёс и экономим RAM при сборке.
if [[ -f requirements.txt ]]; then
    pip install --no-cache-dir -r requirements.txt
else
    pip install --no-cache-dir "aiogram>=3.4" "cryptography>=42.0" "openpyxl>=3.1" \
        "SQLAlchemy>=2.0" "httpx>=0.27" "python-dotenv>=1.0"
fi

# --- 5. .env: бережно дописываем недостающее -------------------------------- #
touch "$ENV_FILE"

if ! grep -q '^TELEGRAM_BOT_TOKEN=' "$ENV_FILE"; then
    printf '\nTELEGRAM_BOT_TOKEN=\n' >> "$ENV_FILE"
    echo "⚠️  Добавлена пустая TELEGRAM_BOT_TOKEN= — впишите токен от @BotFather."
else
    echo "✅ TELEGRAM_BOT_TOKEN присутствует в .env"
fi

if ! grep -q '^ENCRYPTION_KEY=' "$ENV_FILE"; then
    KEY="$(python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')"
    echo "ENCRYPTION_KEY=$KEY" >> "$ENV_FILE"
    echo "🔐 Сгенерирован ENCRYPTION_KEY и записан в .env"
else
    echo "✅ ENCRYPTION_KEY присутствует в .env"
fi

if ! grep -q '^ADMIN_IDS=' "$ENV_FILE"; then
    printf 'ADMIN_IDS=\n' >> "$ENV_FILE"
    echo "ℹ️  Добавлена пустая ADMIN_IDS= — впишите свой telegram_id (через запятую) для /stats."
else
    echo "✅ ADMIN_IDS присутствует в .env"
fi

# --- 6. Dry-run сборки бота (импорт + роутеры, без polling) ------------------ #
echo "🧪 Dry-run диспетчера aiogram…"
python bot.py --dry-run

echo ""
echo "🎉 Готово. Единое окружение: $VENV_DIR"
echo "   Бот:  source $VENV_DIR/bin/activate && python bot.py"
echo "   CLI:  source $VENV_DIR/bin/activate && python main.py --user <id> --sync"
