#!/usr/bin/env bash
cd "$(dirname "$0")"
sudo docker compose logs -f --tail=200 app worker
