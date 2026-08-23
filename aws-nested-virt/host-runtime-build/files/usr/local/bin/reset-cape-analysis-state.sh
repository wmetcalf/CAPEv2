#!/bin/bash
set -euo pipefail

stop_if_present() {
  local unit="$1"
  local state=""

  if systemctl list-unit-files "${unit}" >/dev/null 2>&1; then
    systemctl stop --no-block "${unit}" || true

    for _ in $(seq 1 30); do
      state="$(systemctl is-active "${unit}" 2>/dev/null || true)"
      case "${state}" in
        inactive|failed|unknown)
          return 0
          ;;
      esac
      sleep 1
    done

    systemctl kill --kill-who=all "${unit}" >/dev/null 2>&1 || true
    for _ in $(seq 1 10); do
      state="$(systemctl is-active "${unit}" 2>/dev/null || true)"
      case "${state}" in
        inactive|failed|unknown)
          return 0
          ;;
      esac
      sleep 1
    done
  fi
}

start_if_present() {
  local unit="$1"
  if systemctl list-unit-files "${unit}" >/dev/null 2>&1; then
    systemctl start "${unit}" || true
  fi
}

mkdir -p /opt/CAPEv2/storage/analyses /opt/CAPEv2/storage/binaries /opt/CAPEv2/storage/guacrecordings /opt/CAPEv2/storage/guacamole

stop_if_present cape.service
stop_if_present cape-processor.service
stop_if_present cape-web.service
stop_if_present guac-web.service

sudo -u postgres psql -d cape -c "TRUNCATE TABLE tasks, tasks_tags, errors, sample_associations, samples RESTART IDENTITY CASCADE;"
sudo -u postgres psql -d cape -c "DELETE FROM tags WHERE id NOT IN (SELECT tag_id FROM tasks_tags UNION SELECT tag_id FROM machines_tags);"

rm -rf /opt/CAPEv2/storage/analyses/*
rm -rf /opt/CAPEv2/storage/binaries/*
rm -rf /opt/CAPEv2/storage/guacrecordings/*
rm -rf /opt/CAPEv2/storage/guacamole/*

if command -v mongosh >/dev/null 2>&1; then
  mongosh --quiet --eval 'db.analysis.deleteMany({}); db.calls.deleteMany({}); db.files.deleteMany({});' cuckoo >/dev/null
fi

chown -R cape:cape /opt/CAPEv2/storage

start_if_present guac-web.service
start_if_present cape-web.service
start_if_present cape-processor.service
start_if_present cape.service
