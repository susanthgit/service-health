#!/usr/bin/env bash
# =============================================================================
# atlas-gw watchdog — counter-trigger against MCAPSGov-AutomationApp nightly
# deallocate of the CDX-hosted gateway VM.
#
# Flow:
#   1. Check VM power state. If deallocated/stopping -> az vm start (no-wait) +
#      ntfy "starting" + exit 0 (next run handles health).
#   2. Probe /health on the public CF tunnel. If 200 -> done.
#   3. Self-heal via az vm run-command -> restart cloudflared + openclaw-gateway.
#   4. Re-probe. If 200 -> ntfy "self-healed".
#   5. Else -> ntfy URGENT "STILL DOWN" + exit non-zero so the GHA job is red.
# =============================================================================
set -euo pipefail

RG="${RG:-rg-atlas-gateway-prod}"
VM="${VM:-vm-atlas-gw-01}"
HEALTH_URL="${HEALTH_URL:-https://atlas-gw.aguidetocloud.com/health}"
NTFY_TOPIC="${NTFY_TOPIC:-}"
RUN_ID="${GITHUB_RUN_ID:-local}"

log() { printf '[%s] %s\n' "$(date -u +%FT%TZ)" "$*"; }

ntfy() {
  local title="$1" body="$2" priority="${3:-default}" tags="${4:-gear}"
  if [ -z "$NTFY_TOPIC" ]; then
    log "  (ntfy: NTFY_TOPIC not set, skipping push)"
    return 0
  fi
  curl -sS -X POST "https://ntfy.sh/$NTFY_TOPIC" \
    -H "Title: $title" \
    -H "Priority: $priority" \
    -H "Tags: $tags" \
    -d "$body" >/dev/null 2>&1 \
    && log "  ntfy -> $title" \
    || log "  ntfy push FAILED"
}

# -------------- step 1: VM power state --------------
log "Checking VM power state ($VM in $RG)..."
STATE=$(az vm get-instance-view -g "$RG" -n "$VM" \
  --query "instanceView.statuses[?starts_with(code,'PowerState/')].displayStatus" \
  -o tsv 2>/dev/null || echo "unknown")
log "  PowerState: $STATE"

if [ "$STATE" != "VM running" ]; then
  log "VM not running -- starting (no-wait)..."
  ntfy "atlas-gw: VM was '$STATE' -- starting" \
       "Watchdog run $RUN_ID detected VM in '$STATE'. Issuing az vm start. Next run (<=5min) will verify /health." \
       high "warning,fast_forward"
  az vm start -g "$RG" -n "$VM" --no-wait
  log "  start command issued; exiting (next run does the health check)."
  exit 0
fi

# -------------- step 2: health probe --------------
log "Probing $HEALTH_URL..."
HEALTH_CODE=$(curl -sS -o /dev/null -w "%{http_code}" --max-time 10 "$HEALTH_URL" 2>/dev/null || echo "000")
log "  /health -> HTTP $HEALTH_CODE"

if [ "$HEALTH_CODE" = "200" ]; then
  log "  Healthy."
  exit 0
fi

# -------------- step 3: self-heal --------------
log "Health failed (HTTP $HEALTH_CODE). Attempting self-heal via run-command..."
cat > /tmp/heal.sh <<'HEAL_EOF'
set +e
systemctl reset-failed cloudflared openclaw-gateway.service 2>/dev/null
systemctl restart cloudflared
sleep 6
systemctl restart openclaw-gateway.service
sleep 10
echo "cloudflared: $(systemctl is-active cloudflared)"
echo "openclaw-gateway: $(systemctl is-active openclaw-gateway.service)"
ss -tln 2>/dev/null | grep -E ':18789|:20241' | head -2
HEAL_EOF

az vm run-command invoke -g "$RG" -n "$VM" \
  --command-id RunShellScript --scripts "@/tmp/heal.sh" -o json 2>&1 \
  | grep -oE '"message"[^"]*"[^"]*"' | head -2 || true

# -------------- step 4: re-probe --------------
sleep 15
HEALTH_CODE=$(curl -sS -o /dev/null -w "%{http_code}" --max-time 10 "$HEALTH_URL" 2>/dev/null || echo "000")
log "  /health (post-heal) -> HTTP $HEALTH_CODE"

if [ "$HEALTH_CODE" = "200" ]; then
  log "  Recovered via self-heal."
  ntfy "atlas-gw: self-healed" \
       "Watchdog run $RUN_ID restored gateway: services restarted, /health 200." \
       default "white_check_mark"
  exit 0
fi

# -------------- step 5: escalate --------------
log "  STILL DOWN after self-heal."
ntfy "atlas-gw: STILL DOWN" \
     "Watchdog run $RUN_ID couldn't restore /health (HTTP $HEALTH_CODE) after restart. Manual investigation needed. See GHA: https://github.com/$GITHUB_REPOSITORY/actions/runs/$RUN_ID" \
     urgent "rotating_light"
exit 1
