#!/usr/bin/env bash
# Deploy the Switchyard public demo to an Amazon Linux EC2 instance (Docker, port 80).
#
#   deploy/ec2.sh <public-ip-or-dns> [path/to/key.pem] [--with-key]
#
# The demo serves recorded runs, the judge tour and the free repo audit, none of which need a key.
# --with-key copies NEBIUS_API_KEY (from .env.local / .env) to the server so live runs work, behind
# a random access code that the script prints (SWITCHYARD_RUN_CODE); spend per run is capped.
# Needs: ssh, scp, and an inbound rule for ports 22 (your IP) and 80 (anywhere) on the instance.
set -euo pipefail

HOST="${1:?usage: deploy/ec2.sh <public-ip-or-dns> [key.pem] [--with-key]}"
KEY="${2:-$HOME/Downloads/switchyard.pem}"
WITH_KEY="${3:-}"
[ "$KEY" = "--with-key" ] && { WITH_KEY="--with-key"; KEY="$HOME/Downloads/switchyard.pem"; }
USER_AT="ec2-user@$HOST"
REPO="https://github.com/ayushagarwalhere/switchyard"

# ssh refuses keys other users can read; keep a private copy
mkdir -p "$HOME/.ssh"
cp "$KEY" "$HOME/.ssh/switchyard.pem"
chmod 600 "$HOME/.ssh/switchyard.pem"
SSH=(ssh -i "$HOME/.ssh/switchyard.pem" -o StrictHostKeyChecking=accept-new -o ConnectTimeout=15 "$USER_AT")

echo "== installing Docker and git on $HOST"
"${SSH[@]}" 'sudo dnf install -y -q docker git >/dev/null && sudo systemctl enable --now docker >/dev/null && sudo usermod -aG docker ec2-user'

echo "== fetching Switchyard"
"${SSH[@]}" "rm -rf ~/switchyard && git clone -q --depth 1 $REPO ~/switchyard"
# recorded runs aren't in git: copy the featured ones
if [ -d runs/demo ] && [ -n "$(ls -A runs/demo 2>/dev/null | grep -v .gitkeep)" ]; then
  tar -czf - runs/demo | "${SSH[@]}" "tar -xzf - -C ~/switchyard"
fi

ENV_ARGS="-e SWITCHYARD_PUBLIC=1"
if [ "$WITH_KEY" = "--with-key" ]; then
  KEYVAL="$(python -c 'from switchyard import config; print(config.api_key() or "")')"
  [ -n "$KEYVAL" ] || { echo "no Token Factory key found locally"; exit 1; }
  CODE="$(python -c 'import secrets; print(secrets.token_urlsafe(9))')"
  printf 'NEBIUS_API_KEY=%s\nSWITCHYARD_RUN_CODE=%s\nSWITCHYARD_RUN_BUDGET=0.50\n' "$KEYVAL" "$CODE" | "${SSH[@]}" 'umask 077 && cat > ~/switchyard.env'
  ENV_ARGS="$ENV_ARGS --env-file /home/ec2-user/switchyard.env"
  echo "== live runs enabled; access code: $CODE"
fi

echo "== building and starting the container"
"${SSH[@]}" "cd ~/switchyard && sudo docker build -q -t switchyard . >/dev/null && (sudo docker rm -f switchyard >/dev/null 2>&1 || true) && sudo docker run -d --restart unless-stopped --name switchyard -p 80:7860 $ENV_ARGS switchyard >/dev/null"

echo "== waiting for it to answer"
for i in $(seq 1 30); do
  if curl -fsS "http://$HOST/api/health" >/dev/null 2>&1; then
    echo "Switchyard is live: http://$HOST/   (judge tour: http://$HOST/#tour)"
    exit 0
  fi
  sleep 2
done
echo "Started, but http://$HOST/ didn't answer. Check the security group allows inbound TCP 80, then: ${SSH[*]} 'sudo docker logs switchyard'"
exit 1
