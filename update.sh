#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
sudo docker compose up -d --build
sudo docker image prune -f
curl -fsS http://127.0.0.1:8080/api/health | jq .
