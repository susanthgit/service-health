# atlas-gw watchdog

> Counter-trigger automation for `atlas-gw` (Sush's OpenClaw gateway on Azure CDX). Runs every 5 minutes via GitHub Actions. **Cloud-side, free, survives laptop sleep.**

## Why this exists

CDX subscriptions are governed by `MCAPSGov-AutomationApp` (Microsoft Customer & Partner Solutions Governance SP, appId `eadea216-1d5c-4a4b-beaf-4f145e6b1cb4`, MS corp tenant `72f988bf-...`). It nightly-deallocates VMs in CDX subs to control demo-sub spend. It is **not configurable from inside the subscription** — you can't see the policy or opt out, only counter-trigger.

This watchdog does that.

Discovered 2026-06-06 ~07:00 NZST when the `atlas-gw` VM (in `rg-atlas-gateway-prod`, sub `96879ea6-...`) was found deallocated at `2026-06-05T12:48:31Z` (12:48 UTC = 00:48 NZST) by the MCAPSGov SP. Companion + WhatsApp both lost route. See session journal entry 2026-06-06.

## What it does

Every 5 min:

1. **Check VM power state** via `az vm get-instance-view`.
   - If not `VM running` → `az vm start --no-wait` + ntfy push (priority: high) + exit. Next run handles health.
2. **Health probe** `https://atlas-gw.aguidetocloud.com/health` (the OpenClaw `/health` endpoint that returns `{"ok":true,"status":"live"}`).
   - If HTTP 200 → done.
3. **Self-heal** via `az vm run-command` → `systemctl restart cloudflared openclaw-gateway.service`.
4. **Re-probe** — if 200 → ntfy push (default) "self-healed".
5. **Escalate** — if still failing → ntfy push (urgent) "STILL DOWN" + GHA job goes red.

Worst-case downtime: **≤5 min** (the cron interval).

## Setup (one-time, already wired)

### 1. Subscribe to ntfy push notifications on your phone

Install <https://ntfy.sh/> Android/iOS app. Subscribe to the topic stored locally in `~/.copilot/session-state/.../atlas-gw-watchdog/NTFY-TOPIC.txt` (do NOT commit this file — it's the unguessable secret that gates pushes to this topic).

### 2. GitHub Actions secrets

Already set via `gh secret set`:

| Secret | What |
|---|---|
| `AZURE_CLIENT_ID` | Atlas-Gateway-Deployer SP appId |
| `AZURE_CLIENT_SECRET` | SP password (40 chars, from SecretStore `atlas-gateway-deployer-secret`) |
| `AZURE_TENANT_ID` | CDX tenant `00b98149-...` |
| `AZURE_SUBSCRIPTION_ID` | CDX sub `96879ea6-...` |
| `NTFY_TOPIC` | The unguessable ntfy topic |

To rotate or re-issue any secret: `gh secret set <NAME> --body "<value>" --repo susanthgit/atlas-gw-watchdog`

### 3. Companion (manual, when Sush returns)

The Companion was paired to the old token. After the rotation 2026-06-05 the new shared token is `4a4109a5-a7bb-41e2-b9b5-ab5d5a9fee41`. Per playbook §11.6 item 2, paste it into Companion Settings → Connections.

## Cost

- GitHub Actions: free (private repo, ~30 sec per run × 12 runs/hr × 720 hr/mo ≈ 4.3 hrs/mo of the 2000 free minutes).
- ntfy.sh: free public service.
- NAT Gateway (added 2026-06-06): ~$5/mo + Standard PIP ~$5/mo + small data egress ≈ $10-12/mo total. Hard-wired prerequisite for the VM to reach the Internet at all (Azure phased out default outbound).
- Azure VM: variable (~$70/mo for D2as_v5 if running 24/7, less when MCAPSGov knocks it out).

## Operational notes

- **Don't disable the workflow without telling someone.** If watchdog stops running, the next MCAPSGov sweep leaves the gateway down indefinitely.
- **Concurrency group** prevents overlapping runs — if a heal takes > 5 min, the next scheduled run waits rather than racing.
- **GHA cron has up to ~15 min skew** under load. So real worst case = 5 min + skew, not exactly 5 min.
- If `MCAPSGov-AutomationApp` ever changes its schedule (e.g., every hour instead of nightly), this still works because the watchdog re-checks every 5 min regardless. Just be aware of the boot-time churn (~2 min per cycle).
- **Idempotent**: every step is safe to run repeatedly. If 100 runs fire while you're investigating, they don't cascade or pile up.

## Manual test

```bash
gh workflow run watchdog.yml --repo susanthgit/atlas-gw-watchdog
gh run watch --repo susanthgit/atlas-gw-watchdog
```

## Future improvements

- Replace SP client secret with federated OIDC (no rotating secret). Setup: `az ad app federated-credential create` for the Atlas-Gateway-Deployer SP with subject `repo:susanthgit/atlas-gw-watchdog:ref:refs/heads/main`. Then the workflow uses `azure/login@v2` with `client-id: ${{ secrets.AZURE_CLIENT_ID }}` and no secret.
- Add a SECOND deeper probe: actually open a WebSocket to `wss://atlas-gw.aguidetocloud.com/` (Companion's real path), not just HTTP `/health`. Currently /health is enough because cloudflared serves both via the same tunnel.
- Track MCAPSGov deallocate timestamps in a long-running issue, so you can see if their schedule changes.
