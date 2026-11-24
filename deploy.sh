#!/usr/bin/env bash
# Copy code and service files to the Pi and restart the services.
# Usage: ./deploy.sh [host]   (default: root@192.168.2.41)
set -euo pipefail
HOST="${1:-root@192.168.2.41}"
cd "$(dirname "$0")"

ssh "$HOST" 'mkdir -p /opt/hems /etc/hems'
scp -q hems/*.py "$HOST":/opt/hems/
scp -q systemd/*.service "$HOST":/etc/systemd/system/
scp -q bin/* "$HOST":/usr/local/bin/
scp -q tools/*.py "$HOST":/opt/hems/
ssh "$HOST" '
  chmod +x /opt/hems/logger.py /opt/hems/report.py /opt/hems/planner.py /opt/hems/executor.py
  ln -sf /opt/hems/report.py /usr/local/bin/hems-report
  test -f /etc/hems/secrets.env || echo "WARNING: /etc/hems/secrets.env missing (TIBBER_TOKEN)"
  systemctl daemon-reload
  for s in hems-display hems-logger hems-planner hems-executor; do systemctl enable -q "$s"; systemctl restart "$s"; done
  sleep 3
  systemctl is-active hems-display hems-logger hems-planner hems-executor
'
