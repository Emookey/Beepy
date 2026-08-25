#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

if [[ ! -f .env ]]; then
  cp .env.example .env
  echo "Created .env. Fill it in, then rerun this script."
  exit 1
fi

if grep -qE 'PASTE_|CHANGE_TO_' .env; then
  echo "The .env file still contains placeholder values."
  echo "Edit: $(pwd)/.env"
  exit 1
fi

echo "Building and starting Beepy..."
sudo docker compose up -d --build

echo "Waiting for the application..."
for i in {1..60}; do
  if curl -fsS http://127.0.0.1:9080/api/health >/dev/null; then
    break
  fi
  sleep 2
done
curl -fsS http://127.0.0.1:9080/api/health | jq .

echo "Configuring private Tailscale HTTPS..."
sudo tailscale serve --bg http://127.0.0.1:9080

DNS_NAME="$(tailscale status --json | jq -r '.Self.DNSName' | sed 's/\.$//')"
echo
echo "Beepy is available privately at:"
echo "  https://${DNS_NAME}/"
echo
echo "Add this exact SPA redirect URI in Microsoft Entra:"
echo "  https://${DNS_NAME}/"
echo
echo "Tailscale Serve status:"
sudo tailscale serve status
