#!/bin/bash
# Обёртка для ЕЖЕМЕСЯЧНОГО автоматического обновления афиши (TimePad, все
# 12 городов бота) - запускается launchd-таском com.taxihelper.timepad-monthly
# (см. AFISHA_MONTHLY_SETUP.md рядом с этим файлом - там разовая настройка).
#
# ДОБАВЛЕНО 24.09.2026 (прямая просьба пользователя - "раз в месяц собирать...
# настроить это чтоб повторно было без моего участия"). Делает всё сама, без
# участия человека:
#   1. Запускает fetch_timepad_data.py (пишет timepad_data.json).
#   2. Если файл реально изменился - git add + commit + push (Railway
#      подхватит новый файл через авто-деплой, как обычно).
#   3. Шлёт тебе в Telegram короткое уведомление - успех (сколько событий
#      по городам) или ошибка (что именно пошло не так) - чтобы не нужно
#      было самому проверять лог, но при желании лог тоже есть (см. ниже).
#
# ВАЖНО: секреты (TIMEPAD_TOKEN/BOT_TOKEN/ADMIN_TELEGRAM_ID) сюда НЕ
# зашиты - читаются из отдельного файла $HOME/.taxi-bot-secrets/afisha_monthly.env
# (создаётся один раз вручную при настройке, в git не попадает).

set -uo pipefail

SECRETS_FILE="$HOME/.taxi-bot-secrets/afisha_monthly.env"
REPO_DIR="$HOME/Desktop/taxi-bot-deploy"
LOG_FILE="$HOME/Library/Logs/taxi-bot-afisha-monthly.log"

mkdir -p "$(dirname "$LOG_FILE")"
log() { echo "$(date '+%Y-%m-%d %H:%M:%S') $1" >> "$LOG_FILE"; }

log "=== Старт ежемесячного обновления афиши ==="

if [ ! -f "$SECRETS_FILE" ]; then
    log "❌ Не найден $SECRETS_FILE - см. AFISHA_MONTHLY_SETUP.md, разовая настройка не завершена."
    exit 1
fi
# shellcheck disable=SC1090
source "$SECRETS_FILE"

if [ -z "${TIMEPAD_TOKEN:-}" ]; then
    log "❌ TIMEPAD_TOKEN пуст в $SECRETS_FILE"
    exit 1
fi

notify_telegram() {
    # $1 - текст сообщения. Тихо пропускает, если BOT_TOKEN/ADMIN_TELEGRAM_ID
    # не заданы (уведомление - бонус, не должно ронять сам процесс).
    if [ -z "${BOT_TOKEN:-}" ] || [ -z "${ADMIN_TELEGRAM_ID:-}" ]; then
        log "⚠️ BOT_TOKEN/ADMIN_TELEGRAM_ID не заданы - уведомление в Telegram пропущено"
        return
    fi
    curl -s -X POST "https://api.telegram.org/bot${BOT_TOKEN}/sendMessage" \
        -d chat_id="${ADMIN_TELEGRAM_ID}" \
        -d parse_mode="Markdown" \
        --data-urlencode text="$1" \
        > /dev/null 2>>"$LOG_FILE"
}

cd "$REPO_DIR" || { log "❌ Не удалось перейти в $REPO_DIR"; notify_telegram "❌ *Афиша (TimePad)*: не найдена папка бота на Mac ($REPO_DIR) - ежемесячное обновление не выполнено."; exit 1; }

BEFORE_HASH=$(shasum timepad_data.json 2>/dev/null | awk '{print $1}')

log "🔄 Запускаю fetch_timepad_data.py..."
FETCH_OUTPUT=$(TIMEPAD_TOKEN="$TIMEPAD_TOKEN" python3 fetch_timepad_data.py 2>&1)
FETCH_STATUS=$?
echo "$FETCH_OUTPUT" >> "$LOG_FILE"

if [ $FETCH_STATUS -ne 0 ]; then
    log "❌ fetch_timepad_data.py завершился с ошибкой (код $FETCH_STATUS)"
    notify_telegram "❌ *Афиша (TimePad)*: ежемесячный сбор данных упал с ошибкой. Подробности в логе на Mac: \`$LOG_FILE\`"
    exit 1
fi

AFTER_HASH=$(shasum timepad_data.json 2>/dev/null | awk '{print $1}')

if [ "$BEFORE_HASH" == "$AFTER_HASH" ]; then
    log "ℹ️ timepad_data.json не изменился - коммит не нужен"
    notify_telegram "ℹ️ *Афиша (TimePad)*: ежемесячное обновление прошло, но данные не изменились (файл идентичен предыдущему)."
    exit 0
fi

# Короткая сводка по городам для уведомления - "сколько событий сейчас в
# каждом городе" (без завязки на main.py - простым grep/python по JSON).
SUMMARY=$(python3 -c "
import json
try:
    with open('timepad_data.json', encoding='utf-8') as f:
        data = json.load(f)
    cities = data.get('cities', {})
    lines = [f'{c}: {len(v)}' for c, v in sorted(cities.items())]
    print(', '.join(lines))
except Exception as e:
    print(f'не удалось прочитать сводку ({e})')
")

log "📝 Коммичу изменения..."
git add timepad_data.json geocode_cache.json 2>>"$LOG_FILE"
git commit -m "$(cat <<EOF
Автообновление афиши TimePad (ежемесячно, launchd)

Автоматический ежемесячный сбор (см. run_monthly_afisha_update.sh) -
$(date '+%d.%m.%Y'). События по городам: $SUMMARY
EOF
)" >> "$LOG_FILE" 2>&1
COMMIT_STATUS=$?

if [ $COMMIT_STATUS -ne 0 ]; then
    log "❌ git commit завершился с ошибкой (код $COMMIT_STATUS)"
    notify_telegram "❌ *Афиша (TimePad)*: данные собраны, но git commit не удался. Проверь на Mac вручную (\`$REPO_DIR\`), лог: \`$LOG_FILE\`"
    exit 1
fi

log "🚀 Пушу в git..."
git push >> "$LOG_FILE" 2>&1
PUSH_STATUS=$?

if [ $PUSH_STATUS -ne 0 ]; then
    log "❌ git push завершился с ошибкой (код $PUSH_STATUS)"
    notify_telegram "❌ *Афиша (TimePad)*: данные собраны и закоммичены локально, но git push не удался (нет сети/конфликт?). Запушь вручную на Mac (\`$REPO_DIR\`), лог: \`$LOG_FILE\`"
    exit 1
fi

log "✅ Готово - запушено, Railway задеплоит автоматически"
notify_telegram "✅ *Афиша (TimePad)*: ежемесячное обновление прошло автоматически и запушено. События по городам: $SUMMARY"
