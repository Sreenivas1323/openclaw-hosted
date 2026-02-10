#!/usr/bin/env bash
# Run health checks on all active instances.
# Add to crontab: */5 * * * * /path/to/health_cron.sh
#
# Requires: ADMIN_API_KEY env var or set below
# Backend must be running at BACKEND_URL

BACKEND_URL="${BACKEND_URL:-http://localhost:8000}"
ADMIN_API_KEY="${ADMIN_API_KEY:?Set ADMIN_API_KEY}"

result=$(curl -s -X POST \
    -H "X-API-Key: ${ADMIN_API_KEY}" \
    "${BACKEND_URL}/api/health-check-all")

unhealthy=$(echo "$result" | jq -r '.unhealthy // 0')

if [[ "$unhealthy" -gt 0 ]]; then
    echo "⚠️ UNHEALTHY INSTANCES DETECTED: $result"
    # TODO: Send alert to Telegram/Discord
    # curl -s -X POST "https://api.telegram.org/bot${TG_BOT_TOKEN}/sendMessage" \
    #   -d "chat_id=${TG_CHAT_ID}" \
    #   -d "text=⚠️ OpenClaw Hosted: ${unhealthy} unhealthy instances\n${result}"
fi
