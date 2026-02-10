#!/usr/bin/env bash
#
# provisioning.sh — Provision a new OpenClaw instance on Hetzner Cloud
#
# Usage:
#   ./provisioning.sh <customer_id> [setup_password]
#
# Environment variables required:
#   HETZNER_API_TOKEN  — Hetzner Cloud API token
#   SSH_KEY_ID         — Hetzner SSH key ID (pre-uploaded via API)
#   FIREWALL_ID        — Hetzner Firewall ID (pre-created, allows 22+18789)
#
# Output (JSON on stdout):
#   { "server_id": 12345, "server_ip": "x.x.x.x", "setup_url": "http://x.x.x.x:18789", "status": "success" }
#
# Exit codes:
#   0 — success
#   1 — missing dependencies/env vars
#   2 — Hetzner API error
#   3 — SSH/install error
#   4 — health check failed after install

set -euo pipefail

# ──────────────────────────────────────────────
# Config
# ──────────────────────────────────────────────
CUSTOMER_ID="${1:?Usage: $0 <customer_id> [setup_password]}"
SETUP_PASSWORD="${2:-$(openssl rand -hex 16)}"

SERVER_TYPE="cax11"                          # 2 ARM cores, 4GB RAM, 40GB SSD
IMAGE="ubuntu-24.04"                         # Ubuntu 24.04 ARM
LOCATION="fsn1"                              # Falkenstein, cheapest
SERVER_NAME="oc-${CUSTOMER_ID}"              # e.g., oc-cust_a1b2c3

HETZNER_API="https://api.hetzner.cloud/v1"
MAX_WAIT_SECONDS=180                         # Max wait for server to become SSH-able
HEALTH_CHECK_RETRIES=12                      # 12 retries × 10s = 2 minutes
HEALTH_CHECK_INTERVAL=10

# ──────────────────────────────────────────────
# Validation
# ──────────────────────────────────────────────
log() { echo "[$(date -u '+%Y-%m-%d %H:%M:%S')] $*" >&2; }
die() { log "ERROR: $*"; exit "${2:-1}"; }

command -v curl >/dev/null   || die "curl not found"
command -v jq >/dev/null     || die "jq not found"
command -v ssh >/dev/null    || die "ssh not found"

[[ -n "${HETZNER_API_TOKEN:-}" ]] || die "HETZNER_API_TOKEN not set"
[[ -n "${SSH_KEY_ID:-}" ]]        || die "SSH_KEY_ID not set"

# ──────────────────────────────────────────────
# Helper: Hetzner API call
# ──────────────────────────────────────────────
hetzner() {
    local method="$1" endpoint="$2" body="${3:-}"
    local args=(
        -s -w "\n%{http_code}"
        -H "Authorization: Bearer ${HETZNER_API_TOKEN}"
        -H "Content-Type: application/json"
        -X "$method"
        "${HETZNER_API}${endpoint}"
    )
    [[ -n "$body" ]] && args+=(-d "$body")

    local response
    response=$(curl "${args[@]}")

    local http_code
    http_code=$(echo "$response" | tail -1)
    local body_content
    body_content=$(echo "$response" | sed '$d')

    if [[ "$http_code" -ge 400 ]]; then
        log "Hetzner API error (HTTP $http_code): $body_content"
        return 1
    fi

    echo "$body_content"
}

# ──────────────────────────────────────────────
# Step 1: Check if server already exists (idempotency)
# ──────────────────────────────────────────────
log "Checking if server '${SERVER_NAME}' already exists..."

EXISTING=$(hetzner GET "/servers?name=${SERVER_NAME}")
EXISTING_COUNT=$(echo "$EXISTING" | jq '.servers | length')

if [[ "$EXISTING_COUNT" -gt 0 ]]; then
    SERVER_ID=$(echo "$EXISTING" | jq -r '.servers[0].id')
    SERVER_IP=$(echo "$EXISTING" | jq -r '.servers[0].public_net.ipv4.ip')
    log "Server already exists: ID=${SERVER_ID}, IP=${SERVER_IP}"
else
    # ──────────────────────────────────────────
    # Step 2: Create the server
    # ──────────────────────────────────────────
    log "Creating server '${SERVER_NAME}' (${SERVER_TYPE} in ${LOCATION})..."

    CREATE_BODY=$(jq -n \
        --arg name "$SERVER_NAME" \
        --arg server_type "$SERVER_TYPE" \
        --arg image "$IMAGE" \
        --arg location "$LOCATION" \
        --argjson ssh_keys "[${SSH_KEY_ID}]" \
        --argjson firewalls "${FIREWALL_ID:+[{\"firewall\": ${FIREWALL_ID}}]}" \
        '{
            name: $name,
            server_type: $server_type,
            image: $image,
            location: $location,
            ssh_keys: $ssh_keys,
            start_after_create: true,
            labels: { "managed-by": "openclaw-hosted", "customer": $name }
        } + (if $firewalls != null then { firewalls: $firewalls } else {} end)'
    )

    CREATE_RESPONSE=$(hetzner POST "/servers" "$CREATE_BODY") || die "Failed to create server" 2

    SERVER_ID=$(echo "$CREATE_RESPONSE" | jq -r '.server.id')
    SERVER_IP=$(echo "$CREATE_RESPONSE" | jq -r '.server.public_net.ipv4.ip')

    log "Server created: ID=${SERVER_ID}, IP=${SERVER_IP}"

    # If IP is null, server is still initializing — poll until we get it
    if [[ "$SERVER_IP" == "null" || -z "$SERVER_IP" ]]; then
        log "Waiting for server IP assignment..."
        for i in $(seq 1 30); do
            sleep 5
            SERVER_INFO=$(hetzner GET "/servers/${SERVER_ID}")
            SERVER_IP=$(echo "$SERVER_INFO" | jq -r '.server.public_net.ipv4.ip')
            STATUS=$(echo "$SERVER_INFO" | jq -r '.server.status')
            [[ "$SERVER_IP" != "null" && -n "$SERVER_IP" && "$STATUS" == "running" ]] && break
            log "  Attempt $i: status=$STATUS, ip=$SERVER_IP"
        done

        if [[ "$SERVER_IP" == "null" || -z "$SERVER_IP" ]]; then
            die "Server failed to get an IP after 150 seconds" 2
        fi
    fi
fi

# ──────────────────────────────────────────────
# Step 3: Wait for SSH to become available
# ──────────────────────────────────────────────
log "Waiting for SSH on ${SERVER_IP}..."

SSH_OPTS="-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=5 -o LogLevel=ERROR"
SECONDS_WAITED=0

while ! ssh $SSH_OPTS root@"$SERVER_IP" "echo ok" &>/dev/null; do
    sleep 5
    SECONDS_WAITED=$((SECONDS_WAITED + 5))
    if [[ $SECONDS_WAITED -ge $MAX_WAIT_SECONDS ]]; then
        die "SSH not available after ${MAX_WAIT_SECONDS}s" 3
    fi
    log "  Waiting for SSH... (${SECONDS_WAITED}s)"
done

log "SSH is ready (took ${SECONDS_WAITED}s)"

# ──────────────────────────────────────────────
# Step 4: Install OpenClaw
# ──────────────────────────────────────────────
log "Installing OpenClaw on ${SERVER_IP}..."

# The install script runs on the remote server
INSTALL_SCRIPT=$(cat <<'REMOTE_SCRIPT'
#!/bin/bash
set -euo pipefail

export DEBIAN_FRONTEND=noninteractive

# Update system
apt-get update -qq && apt-get upgrade -y -qq

# Install Node.js 22 (OpenClaw requirement)
if ! command -v node &>/dev/null; then
    curl -fsSL https://deb.nodesource.com/setup_22.x | bash -
    apt-get install -y -qq nodejs
fi

# Install OpenClaw via official installer
if ! command -v openclaw &>/dev/null; then
    curl -fsSL https://get.openclaw.com | bash
fi

# Verify installation
openclaw --version || echo "openclaw installed (version check may not work yet)"

echo "INSTALL_DONE"
REMOTE_SCRIPT
)

ssh $SSH_OPTS root@"$SERVER_IP" "$INSTALL_SCRIPT" 2>&1 | while read -r line; do
    log "  [remote] $line"
done

if [[ ${PIPESTATUS[0]} -ne 0 ]]; then
    die "OpenClaw installation failed" 3
fi

# ──────────────────────────────────────────────
# Step 5: Configure OpenClaw Gateway
# ──────────────────────────────────────────────
log "Configuring OpenClaw Gateway..."

CONFIGURE_SCRIPT=$(cat <<REMOTE_CONFIG
#!/bin/bash
set -euo pipefail

# Create openclaw user if not exists
if ! id -u openclaw &>/dev/null; then
    useradd -m -s /bin/bash openclaw
fi

OPENCLAW_HOME="/home/openclaw"
OPENCLAW_DIR="\${OPENCLAW_HOME}/.openclaw"
mkdir -p "\${OPENCLAW_DIR}"

# Write gateway config with setup password
cat > "\${OPENCLAW_DIR}/gateway.json" <<GWEOF
{
  "setupPassword": "${SETUP_PASSWORD}",
  "port": 18789,
  "host": "0.0.0.0"
}
GWEOF

chown -R openclaw:openclaw "\${OPENCLAW_HOME}"

# Create systemd service
cat > /etc/systemd/system/openclaw-gateway.service <<SVCEOF
[Unit]
Description=OpenClaw Gateway
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=openclaw
WorkingDirectory=/home/openclaw
ExecStart=/usr/bin/env openclaw gateway start --foreground
Restart=always
RestartSec=5
StartLimitIntervalSec=60
StartLimitBurst=5
Environment=HOME=/home/openclaw
Environment=NODE_ENV=production

# Resource limits
MemoryMax=3G
CPUQuota=180%

# Security hardening
NoNewPrivileges=true
ProtectSystem=strict
ReadWritePaths=/home/openclaw

[Install]
WantedBy=multi-user.target
SVCEOF

# Enable and start
systemctl daemon-reload
systemctl enable openclaw-gateway
systemctl start openclaw-gateway

# Wait a moment for startup
sleep 3

# Check if it's running
if systemctl is-active --quiet openclaw-gateway; then
    echo "CONFIGURE_DONE"
else
    echo "CONFIGURE_FAILED"
    journalctl -u openclaw-gateway --no-pager -n 20
    exit 1
fi
REMOTE_CONFIG
)

ssh $SSH_OPTS root@"$SERVER_IP" "$CONFIGURE_SCRIPT" 2>&1 | while read -r line; do
    log "  [remote] $line"
done

if [[ ${PIPESTATUS[0]} -ne 0 ]]; then
    die "Gateway configuration failed" 3
fi

# ──────────────────────────────────────────────
# Step 6: Health check
# ──────────────────────────────────────────────
log "Running health check..."

HEALTHY=false
for i in $(seq 1 $HEALTH_CHECK_RETRIES); do
    HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" --connect-timeout 5 \
        "http://${SERVER_IP}:18789/" 2>/dev/null || echo "000")

    if [[ "$HTTP_CODE" -ge 200 && "$HTTP_CODE" -lt 500 ]]; then
        HEALTHY=true
        log "Health check passed (HTTP ${HTTP_CODE}) on attempt $i"
        break
    fi

    log "  Health check attempt $i/${HEALTH_CHECK_RETRIES}: HTTP ${HTTP_CODE}"
    sleep "$HEALTH_CHECK_INTERVAL"
done

if [[ "$HEALTHY" != "true" ]]; then
    # Output partial success — server exists but gateway isn't responding
    jq -n \
        --argjson server_id "$SERVER_ID" \
        --arg server_ip "$SERVER_IP" \
        --arg setup_password "$SETUP_PASSWORD" \
        --arg status "unhealthy" \
        '{
            server_id: $server_id,
            server_ip: $server_ip,
            setup_url: ("http://" + $server_ip + ":18789"),
            setup_password: $setup_password,
            status: $status,
            note: "Server provisioned but gateway health check failed. May need manual intervention."
        }'
    exit 4
fi

# ──────────────────────────────────────────────
# Step 7: Output result
# ──────────────────────────────────────────────
SETUP_URL="http://${SERVER_IP}:18789"

jq -n \
    --argjson server_id "$SERVER_ID" \
    --arg server_ip "$SERVER_IP" \
    --arg setup_url "$SETUP_URL" \
    --arg setup_password "$SETUP_PASSWORD" \
    --arg server_name "$SERVER_NAME" \
    --arg status "success" \
    '{
        server_id: $server_id,
        server_ip: $server_ip,
        server_name: $server_name,
        setup_url: $setup_url,
        setup_password: $setup_password,
        status: $status
    }'

log "✅ Provisioning complete! Setup URL: ${SETUP_URL}"
