#!/usr/bin/env bash
set -euo pipefail
echo "=== Containers ==="
sudo docker compose ps
echo "=== API ==="
curl -fsS http://127.0.0.1:9080/api/health | jq .
echo "=== Ollama ==="
curl -fsS http://127.0.0.1:11434/api/tags | jq '.models[].name'
echo "=== Tailscale ==="
tailscale status
sudo tailscale serve status
echo "=== GPU ==="
nvidia-smi || true
ollama ps || true
