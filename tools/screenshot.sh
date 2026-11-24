#!/usr/bin/env bash
# Render the current status screen on the Pi and save it as docs/screenshot.png
# Usage: tools/screenshot.sh [host]   (default: root@192.168.2.41)
set -euo pipefail
HOST="${1:-root@192.168.2.41}"
cd "$(dirname "$0")/.."
ssh "$HOST" 'cd /opt/hems && set -a && . /etc/hems/secrets.env && set +a && python3 -c "
import display as d, tibber, weather, time
from datetime import datetime
tibber._load_cache(); weather._current = weather._fetch(); r = d.Res(); time.sleep(0.5)
d.render(d.poll(), r.sample(), datetime.now().astimezone()).save(\"/tmp/screenshot.png\")"'
scp -q "$HOST":/tmp/screenshot.png docs/screenshot.png
echo "saved docs/screenshot.png"
