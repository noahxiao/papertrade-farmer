#!/usr/bin/env bash
# Minimal supervisor for hosts without systemd (run it under tmux/screen/nohup).
#  - restarts the farmer if it exits or its heartbeat goes stale
#  - forwards SIGTERM/SIGINT so the farmer can unwind unhedged legs first
set -u
cd "$(dirname "$0")/.."

PYTHON="${PYTHON:-.venv/bin/python}"
CHECK_EVERY_S="${CHECK_EVERY_S:-60}"
RESTART_DELAY_S="${RESTART_DELAY_S:-15}"
STOP_TIMEOUT_S="${STOP_TIMEOUT_S:-720}"
child=""
stopping=0

stop_child() {
  [ -n "$child" ] || return 0
  kill -TERM "$child" 2>/dev/null || return 0
  for _ in $(seq "$STOP_TIMEOUT_S"); do
    kill -0 "$child" 2>/dev/null || return 0
    sleep 1
  done
  echo "supervise: farmer did not stop within ${STOP_TIMEOUT_S}s; killing" >&2
  kill -KILL "$child" 2>/dev/null
}

on_signal() {
  stopping=1
  stop_child
  exit 0
}
trap on_signal TERM INT

while [ "$stopping" -eq 0 ]; do
  "$PYTHON" -m ptfarmer run &
  child=$!
  sleep "$CHECK_EVERY_S"
  while kill -0 "$child" 2>/dev/null; do
    if ! "$PYTHON" -m ptfarmer healthcheck >/dev/null; then
      echo "supervise: heartbeat stale; restarting farmer" >&2
      stop_child
      break
    fi
    sleep "$CHECK_EVERY_S" &
    wait $!
  done
  wait "$child" 2>/dev/null
  echo "supervise: farmer exited ($?); restarting in ${RESTART_DELAY_S}s" >&2
  sleep "$RESTART_DELAY_S"
done
