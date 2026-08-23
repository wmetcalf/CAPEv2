#!/usr/bin/env bash
# /usr/local/bin/cape-post-deploy-smoke.sh
#
# Post-deploy functional smoke test for the CAPE host.  Submits a
# representative URL task, waits for it to reach status=reported,
# then asserts the report has the expected shape (suricata alerts > 0,
# DNS records > 0, capemon hooks present).  Writes a JSON summary to
# /var/log/cape-smoke.json that operators / terraform can poll.
#
# Why this exists (per feedback_automate_post_deploy_validation.md):
# multiple regressions this past week (2026-05-22→05-26) shipped to
# operators because the infra-layer health signals (ALB targets healthy,
# cape services active, HTTPS 200) all stayed green while the analysis
# pipeline was broken — suricata symlink missing, evtx pip missing,
# guacd not installed, etc.  A functional smoke task catches each.
#
# Designed to be invoked by cape-post-deploy-smoke.service (one-shot
# systemd unit, runs once after cape.service comes up on first boot).
# Re-invocation is safe — the script handles existing /var/log/cape-
# smoke.json by overwriting.
#
# Exit codes:
#   0  smoke passed
#   1  smoke failed (details in /var/log/cape-smoke.json)
#   2  infrastructure-level failure (cape services not up, db
#      unreachable, etc.) — fail-fast before submitting

set -euo pipefail

LOG=/var/log/cape-smoke.log
RESULT=/var/log/cape-smoke.json
TARGET_URL="http://ipv4.icanhazip.com"
# 15 min default.  URL task on a healthy host runs ~10 min end-to-end:
# VM boot (~2 min), analyzer warmup (~30s), Edge launch + URL fetch
# (~30s), evtx wipe + result upload (~1 min), CAPE processing pipeline
# (suricata + behavior + signatures + reporting, ~6-7 min).  Caught on
# a validation deploy 2026-05-27 against the baked AMI where the task
# reported in 10m33s but the 10-min TIMEOUT_SECS expired at 10m02s,
# producing pass=false 31s before the genuine pass.  Bumping to 900s
# gives ~5 min of headroom; terraform_data.post_deploy_smoke's outer
# 20-min SSM poll comfortably covers the new bound.
TIMEOUT_SECS="${SMOKE_TIMEOUT:-900}"

exec > >(tee -a "$LOG") 2>&1
echo "[$(date -Iseconds)] cape-post-deploy-smoke starting"

write_result() {
    # Args: <pass=true|false> <message> [extras-json]
    local pass="$1" msg="$2" extras="${3:-{\}}"
    # Atomically rename so terraform's SSM-poll never sees a half-
    # written file.
    cat > "${RESULT}.tmp" <<JSON
{
  "pass": $pass,
  "message": "$msg",
  "timestamp": "$(date -Iseconds)",
  "target": "$TARGET_URL",
  "details": $extras
}
JSON
    mv -f "${RESULT}.tmp" "$RESULT"
    cat "$RESULT"
}

require_service() {
    # Wait up to SERVICE_WAIT_SECS (default 300s) for the unit to be
    # active.  cape.service can flap during initial boot — PIA tunnel
    # takes 60-90s, cape.service ExecStartPre fails-and-restarts if
    # it polls tun0 before openvpn-pia is up, then succeeds.  A
    # point-in-time check at script start can catch cape mid-flap
    # even though the system is healthy a few seconds later.
    #
    # Caught on a validation deploy 2026-05-26: smoke unit's ExecStartPre saw
    # cape.service active at 21:06:21, slept 10s, this function ran
    # at 21:06:31 — but cape had cycled in between (final start at
    # 21:09:08, 2.5 min later).  Active-wait here recovers from those
    # in-flight cycles without lowering the gate's standard.
    local svc="$1"
    local deadline=$(( SECONDS + ${SERVICE_WAIT_SECS:-300} ))
    while [ $SECONDS -lt $deadline ]; do
        systemctl is-active --quiet "$svc" && return 0
        sleep 5
    done
    write_result false "service $svc not active" \
        "{\"failing_service\": \"$svc\"}"
    exit 2
}

require_service cape.service
require_service cape-web.service
require_service cape-processor.service
require_service cape-rooter.service

# ---- Submit ---------------------------------------------------------
# Use the CAPE DRF REST API (POST /apiv2/tasks/create/url/) instead of
# the local submit.py CLI.  Two reasons:
#   1. Exercises the full request path operators + the broker stack
#      will use: Django middleware → DRF auth → submission view → DB.
#      submit.py talks to Python objects directly and skips all of that.
#   2. The token is the same one published by userdata.sh to
#      /etc/cape/api-token + Secrets Manager, so any regression in the
#      install-time token-provisioning path also surfaces here.
#
# Hit http://localhost:8000 (gunicorn directly).  The ALB layer has
# its own health check; smoke is testing CAPE, not the load balancer.
#
# Routing nuance: the API's `route` parameter defaults to whatever
# /etc/cape/routing.conf says when omitted.  We're explicit and pass
# `route=vpn0` so a regression in the routing.conf default (e.g.
# someone changes it to `none` while debugging) doesn't silently make
# the gate test the wrong configuration.
TOKEN_PATH=/etc/cape/api-token
if [ ! -s "$TOKEN_PATH" ]; then
    write_result false "API token not provisioned" \
        "{\"path\": \"$TOKEN_PATH\", \"hint\": \"userdata.sh writes this on first boot; rebake the AMI off main if it's missing.\"}"
    exit 2
fi
API_TOKEN=$(cat "$TOKEN_PATH")

echo "[$(date -Iseconds)] submitting URL task: $TARGET_URL (via REST API)"
submit_out=$(curl -sS --fail-with-body \
    -H "Authorization: Token $API_TOKEN" \
    -F "url=$TARGET_URL" \
    -F "package=edge" \
    -F "route=vpn0" \
    "http://localhost:8000/apiv2/tasks/create/url/" 2>&1 || true)
echo "$submit_out"

# Response is JSON.  Successful submission shape (CAPEv2 DRF view):
#   {"error": false, "data": "Task added with ID(s): [N]"}  (newer)
# or
#   {"data": {"task_ids": [N]}}                              (older)
# Both shapes encode the task id, just in different fields.  Parse
# with python3 (already a hard dep above for json output).
task_id=$(printf '%s' "$submit_out" | python3 -c '
import json, re, sys
raw = sys.stdin.read()
try:
    j = json.loads(raw)
except Exception:
    sys.exit(0)
if isinstance(j.get("data"), dict):
    ids = j["data"].get("task_ids") or []
    if ids:
        print(ids[0]); sys.exit(0)
if isinstance(j.get("data"), str):
    m = re.search(r"\b(\d+)\b", j["data"])
    if m:
        print(m.group(1)); sys.exit(0)
' 2>/dev/null)
if [ -z "$task_id" ]; then
    write_result false "failed to parse task id from API response" \
        "{\"http_response\": $(printf '%s' "$submit_out" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()))')}"
    exit 1
fi
echo "[$(date -Iseconds)] submitted task #$task_id"

# ---- Poll -----------------------------------------------------------
deadline=$(( $(date +%s) + TIMEOUT_SECS ))
status=""
while [ "$(date +%s)" -lt "$deadline" ]; do
    status=$(sudo -u postgres psql -d cape -tA -c \
        "SELECT status FROM tasks WHERE id=$task_id" 2>/dev/null | tr -d '[:space:]')
    case "$status" in
        reported)               echo "[$(date -Iseconds)] task $task_id reported"; break ;;
        failed_analysis|failed_processing|failed_reporting)
            write_result false "task $task_id terminal-failed: $status" \
                "{\"task_id\": $task_id, \"task_status\": \"$status\"}"
            exit 1 ;;
        *)                      echo "[$(date -Iseconds)] task $task_id status=$status (waiting)"; sleep 15 ;;
    esac
done

if [ "$status" != "reported" ]; then
    write_result false "task $task_id timed out (last status: ${status:-unknown})" \
        "{\"task_id\": $task_id, \"task_status\": \"${status:-unknown}\", \"timeout_secs\": $TIMEOUT_SECS}"
    exit 1
fi

# ---- Assert ---------------------------------------------------------
report=/opt/CAPEv2/storage/analyses/$task_id/reports/report.json
if [ ! -f "$report" ]; then
    write_result false "task $task_id reported but report.json missing" \
        "{\"task_id\": $task_id, \"expected_path\": \"$report\"}"
    exit 1
fi

# Pull the metrics we actually care about.  Using a single jq call so
# the JSON we embed in the result file is always well-formed.
metrics=$(jq '{
  alerts:       (.suricata.alerts // [] | length),
  dns:          (.suricata.dns    // [] | length),
  http:         (.suricata.http   // [] | length),
  tls:          (.suricata.tls    // [] | length),
  signatures:   (.signatures      // [] | length),
  processes:    (.behavior.processes // [] | length),
  yara_hits:    (.target.file.yara   // [] | length),
  clamav_hits:  (.target.file.clamav // [] | length)
}' "$report" 2>&1)

# Hard gates — caught every regression this past week:
#
#   alerts == 0      → suricata rule loading broken (missing rules symlink)
#                      OR ruleset filter broke detections (bad ruleset revert)
#   dns   == 0       → in-VM analyzer not uploading network data OR
#                      VM not reaching the result server
#
# Behavior + signatures are NOT hard-gated because the `edge` package
# runs the browser without capemon injection and benign URLs don't
# trigger detection signatures — both 0 are expected for this sample.
# Operators can add their own malware sample submission for those.
alerts=$(jq -r '.alerts'    <<<"$metrics")
dns=$(   jq -r '.dns'       <<<"$metrics")

if [ "${alerts:-0}" -lt 1 ] || [ "${dns:-0}" -lt 1 ]; then
    write_result false "metrics below threshold (alerts=$alerts dns=$dns)" \
        "{\"task_id\": $task_id, \"metrics\": $metrics}"
    exit 1
fi

write_result true "URL task passed smoke gates" \
    "{\"task_id\": $task_id, \"metrics\": $metrics}"
echo "[$(date -Iseconds)] smoke OK"
exit 0
