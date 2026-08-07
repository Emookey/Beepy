#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
mkdir -p backups
STAMP="$(date +%Y%m%d_%H%M%S)"
set -a
source .env
set +a
sudo docker compose exec -T db pg_dump -U "${POSTGRES_USER:-mbc}" "${POSTGRES_DB:-mbc_intelligence}" \
  | gzip > "backups/mbc_intelligence_${STAMP}.sql.gz"
echo "Created backups/mbc_intelligence_${STAMP}.sql.gz"
