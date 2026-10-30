#!/usr/bin/env bash
# Deploy the Switchyard public demo to an Amazon Linux EC2 instance (Docker), served over HTTPS.
#
#   deploy/ec2.sh <public-ip-or-dns> [path/to/key.pem] [--with-key]
#
# HTTPS: Caddy fronts the app and gets a Let's Encrypt certificate automatically. With a bare IP the site name
# is <ip-with-dashes>.sslip.io (e.g. 16-178-179-90.sslip.io), a free DNS name that resolves to that IP.
# Set DOMAIN=your.domain to use your own (its DNS A record must point at the instance). HTTP redirects to HTTPS.
#
# The demo serves recorded runs, the judge tour and the free repo audit, none of which need a key.
# --with-key copies NEBIUS_API_KEY (from .env.local / .env) to the server so live runs work, behind
# a random access code that the script prints (SWITCHYARD_RUN_CODE); spend per run is capped.
# Needs: ssh, scp, and inbound rules for ports 22 (your IP), 80 and 443 (anywhere) on the instance.
set -euo pipefail

HOST="${1:?usage: deploy/ec2.sh <public-ip-or-dns> [key.pem] [--with-key]}"
KEY="${2:-$HOME/Downloads/switchyard.pem}"
WITH_KEY="${3:-}"
[ "$KEY" = "--with-key" ] && { WITH_KEY="--with-key"; KEY="$HOME/Downloads/switchyard.pem"; }
USER_AT="ec2-user@$HOST"
if [ -z "${DOMAIN:-}" ]; then
  if [[ "$HOST" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; then DOMAIN="${HOST//./-}.sslip.io"; else DOMAIN="$HOST"; fi
fi
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
"${SSH[@]}" "cd ~/switchyard && sudo docker build -q -t switchyard . >/dev/null && (sudo docker rm -f switchyard >/dev/null 2>&1 || true) && sudo docker run -d --restart unless-stopped --name switchyard -p 127.0.0.1:7860:7860 $ENV_ARGS switchyard >/dev/null"

echo "== HTTPS for $DOMAIN (Caddy + Let's Encrypt)"
# the site on $DOMAIN; anything else on port 80 (e.g. the bare IP) redirects to it
printf '%s {
	reverse_proxy 127.0.0.1:7860
}
http:// {
	redir https://%s{uri} permanent
}
' "$DOMAIN" "$DOMAIN" | "${SSH[@]}" 'cat > ~/Caddyfile'
"${SSH[@]}" "(sudo docker rm -f caddy >/dev/null 2>&1 || true) && sudo docker run -d --restart unless-stopped --name caddy --network host -v /home/ec2-user/Caddyfile:/etc/caddy/Caddyfile:ro -v caddy_data:/data -v caddy_config:/config caddy:2 >/dev/null"

echo "== waiting for it to answer (the first certificate takes up to a minute)"
for i in $(seq 1 45); do
  if curl -fsS "https://$DOMAIN/api/health" >/dev/null 2>&1; then
    echo "Switchyard is live: https://$DOMAIN/   (judge tour: https://$DOMAIN/#tour)"
    exit 0
  fi
  sleep 3
done
echo "Started, but https://$DOMAIN/ didn't answer. Check the security group allows inbound TCP 80 and 443, then:"
echo "  ${SSH[*]} 'sudo docker logs caddy; sudo docker logs switchyard'"
exit 1
