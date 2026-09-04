#!/usr/bin/env bash
# Deploy the built addon zip to a LibreELEC box over SSH and smoke-test it.
#
#   ./scripts/deploy_libreelec.sh [host] [zip]
#
# Defaults: host 192.168.31.133, user root, password "libreelec"
# (LIBREELEC_PASSWORD overrides), zip = newest dist/*linux_arm64.zip.
# Installs into /storage/.kodi/addons (userdata/addon_data is preserved),
# restarts Kodi, then verifies the service, the engine listener and a
# real proxied request from the box itself.
set -euo pipefail

HOST="${1:-192.168.31.133}"
ZIP="${2:-$(ls -t "$(dirname "$0")"/../dist/service.advancedproxy-*.linux_arm64.zip | head -1)}"
PASS="${LIBREELEC_PASSWORD:-libreelec}"
SSH="sshpass -p $PASS ssh -F /dev/null -o StrictHostKeyChecking=no -o UserKnownHostsFile=/tmp/known_hosts_le -o ConnectTimeout=10"
SCP="sshpass -p $PASS scp -F /dev/null -o StrictHostKeyChecking=no -o UserKnownHostsFile=/tmp/known_hosts_le"

echo ">> deploying $(basename "$ZIP") to root@$HOST"
$SCP "$ZIP" "root@$HOST:/storage/" 

$SSH "root@$HOST" bash -s <<'EOF'
set -euo pipefail
cd /storage
ZIP=$(ls -t /storage/service.advancedproxy-*.linux_arm64.zip | head -1)
echo ">> installing $ZIP"
# Kodi must not hold the old addon files while we replace them.
systemctl stop kodi
rm -rf /storage/.kodi/addons/service.advancedproxy
unzip -q -o "$ZIP" -d /storage/.kodi/addons/
# Drop the stale runtime snapshot so the wait loop below sees a FRESH one.
rm -f /storage/.kodi/userdata/addon_data/service.advancedproxy/state.json
# Make sure the addon stays enabled (fresh install lands disabled on some skins).
python3 - <<'PY' || true
import sqlite3, glob, os
db = sorted(glob.glob(os.path.expanduser("/storage/.kodi/userdata/Database/Addons*.db")))[-1]
conn = sqlite3.connect(db)
conn.execute("DELETE FROM disabled WHERE addonID='service.advancedproxy'")
conn.commit()
PY
rm -f "$ZIP"
systemctl start kodi
echo ">> waiting for the service to come up"
for i in $(seq 1 30); do
  [ -f /storage/.kodi/userdata/addon_data/service.advancedproxy/state.json ] && break
  sleep 2
done
echo "--- state.json:"
cat /storage/.kodi/userdata/addon_data/service.advancedproxy/state.json || echo "NO STATE"
echo
echo "--- engine.json dns block:"
python3 -c "import json;print(json.dumps(json.load(open('/storage/.kodi/userdata/addon_data/service.advancedproxy/engine.json')).get('dns',{}),indent=1))" || true
echo "--- listeners:"
netstat -tlnp 2>/dev/null | grep -E "sing-box|xray" || ss -tlnp | grep -E "sing-box|xray" || echo "no engine listener"
echo "--- recent addon log:"
grep -i advancedproxy /storage/.kodi/temp/kodi.log | tail -15 || true
echo "--- proxied request through the addon:"
PORT=$(python3 -c "import json;print(json.load(open('/storage/.kodi/userdata/addon_data/service.advancedproxy/state.json'))['port'])" 2>/dev/null || echo 1080)
curl -s --max-time 15 -x "http://127.0.0.1:$PORT" -o /dev/null -w "via proxy: %{http_code}\n" https://www.gstatic.com/generate_204 || echo "proxy request FAILED"
EOF
echo ">> done"
