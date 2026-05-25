#!/usr/bin/env bash
set -Eeuo pipefail

MAESTRO_BIN="${MAESTRO_BIN:-/root/projects/Symphony/Maestro/.venv/bin/maestro}"
SYSTEMCTL_BIN="${SYSTEMCTL_BIN:-systemctl}"
TELEGRAM_OPERATOR_SERVICE="${MAESTRO_TELEGRAM_OPERATOR_SERVICE:-maestro-telegram-operator.service}"
STOP_TELEGRAM_OPERATOR="${MAESTRO_STOP_TELEGRAM_OPERATOR:-1}"
LOCK_PATH="${MAESTRO_SIGNAL_LOCK_PATH:-/tmp/maestro-symphony-signal.lock}"

: "${MAESTRO_SIGNAL_CONFIG:?MAESTRO_SIGNAL_CONFIG is required}"
: "${MAESTRO_APPROVAL_CONFIG:?MAESTRO_APPROVAL_CONFIG is required}"

mkdir -p "$(dirname "$LOCK_PATH")"
exec 9>"$LOCK_PATH"
if ! flock -n 9; then
  echo "symphony_signal status=locked lock_path=$LOCK_PATH"
  exit 75
fi

extract_field() {
  local key="$1"
  awk -v key="$key" '{
    for (i = 1; i <= NF; i++) {
      split($i, parts, "=")
      if (parts[1] == key) {
        print parts[2]
        exit
      }
    }
  }'
}

echo "symphony_signal status=starting config=$MAESTRO_SIGNAL_CONFIG"
if ! signal_output="$("$MAESTRO_BIN" run-signal --config "$MAESTRO_SIGNAL_CONFIG" 2>&1)"; then
  printf '%s\n' "$signal_output"
  echo "symphony_signal status=fail reason=run_signal_failed"
  exit 1
fi
printf '%s\n' "$signal_output"

signal_run_id="$(printf '%s\n' "$signal_output" | extract_field "signal_run_id")"
action_required="$(printf '%s\n' "$signal_output" | extract_field "action_required")"

if [[ -z "$signal_run_id" || "$action_required" != "true" && "$action_required" != "false" ]]; then
  echo "symphony_signal status=fail reason=unparseable_run_signal_output"
  exit 1
fi

if [[ "$action_required" == "false" ]]; then
  echo "symphony_signal status=no_action signal_run_id=$signal_run_id"
  exit 0
fi

echo "symphony_signal status=approval_required signal_run_id=$signal_run_id"
telegram_stopped=0
restart_telegram_operator() {
  if [[ "$telegram_stopped" == "1" ]]; then
    "$SYSTEMCTL_BIN" start "$TELEGRAM_OPERATOR_SERVICE" || {
      echo "symphony_signal status=warn reason=telegram_operator_restart_failed service=$TELEGRAM_OPERATOR_SERVICE"
    }
  fi
}
trap restart_telegram_operator EXIT

if [[ "$STOP_TELEGRAM_OPERATOR" == "1" ]]; then
  "$SYSTEMCTL_BIN" stop "$TELEGRAM_OPERATOR_SERVICE"
  telegram_stopped=1
fi

if ! approval_output="$(
  "$MAESTRO_BIN" approve-signal \
    --config "$MAESTRO_APPROVAL_CONFIG" \
    --signal-run-id "$signal_run_id" 2>&1
)"; then
  printf '%s\n' "$approval_output"
  echo "symphony_signal status=fail reason=approve_signal_failed signal_run_id=$signal_run_id"
  exit 1
fi

printf '%s\n' "$approval_output"
echo "symphony_signal status=completed signal_run_id=$signal_run_id"
