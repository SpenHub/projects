#!/usr/bin/env bash
# Build girthy-dash on the Unraid host and (re)create the container.
# Usage: ./deploy.sh [version]   (default 1.0). History in /data survives redeploys.
set -euo pipefail
cd "$(dirname "$0")"
VER="${1:-1.0}"
APPDATA=/mnt/cache/appdata/girthy-dash

ssh unraid "mkdir -p $APPDATA/src/static $APPDATA/data"
scp -q *.py Dockerfile "unraid:$APPDATA/src/"
ssh unraid "rm -rf $APPDATA/src/static"
scp -qr static "unraid:$APPDATA/src/"
sed "s|<Repository>girthy-dash:[^<]*</Repository>|<Repository>girthy-dash:$VER</Repository>|" my-girthy-dash.xml \
  | ssh unraid "cat > /boot/config/plugins/dockerMan/templates-user/my-girthy-dash.xml"

ssh unraid bash -s -- "$VER" "$APPDATA" <<'REMOTE'
set -euo pipefail
VER=$1 APPDATA=$2
docker build -q -t "girthy-dash:$VER" "$APPDATA/src"
docker rm -f girthy-dash >/dev/null 2>&1 || true
docker run -d --name=girthy-dash --net=host \
  -l net.unraid.docker.managed=dockerman -l "net.unraid.docker.webui=http://[IP]:7879/" \
  --restart unless-stopped --cap-drop ALL --cap-add PERFMON --cap-add NET_RAW --cap-add DAC_READ_SEARCH \
  --security-opt no-new-privileges --device /dev/dri:/dev/dri \
  -e NET_IFACE=br0 -e PING_TARGETS=1.1.1.1,8.8.8.8 \
  -v "$APPDATA/data:/data" -v "$APPDATA/api_key:/config/api_key:ro" \
  -v /dev/shm:/host/transcode:ro -v /var/lib/docker:/host/docker:ro \
  -v /mnt/user:/host/user:ro -v "$APPDATA/arr.json:/config/arr.json:ro" -v "$APPDATA/docker_key:/config/docker_key:ro" -v /etc/localtime:/etc/localtime:ro \
  "girthy-dash:$VER" >/dev/null
docker image prune -f --filter "label!=keep" --filter "dangling=true" >/dev/null
sleep 3
curl -fsS -o /dev/null http://127.0.0.1:7879/healthz && echo "girthy-dash $VER is up on :7879"
REMOTE
