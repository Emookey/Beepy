from __future__ import annotations

import logging
import time

from sqlalchemy import select

from .db import SessionLocal, initialize_database
from .email_graph import email_indexer_configured
from .email_sync import sync_email_all
from .models import EmailFolder

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

INTERVAL_SECONDS = 900


def main():
    initialize_database()
    while True:
        try:
            if not email_indexer_configured():
                logger.info("Email Intelligence credentials are not configured yet; worker is idle.")
            else:
                from .models import SyncState
                with SessionLocal() as db:
                    sync_state = db.get(SyncState, "email")
                    backfill_complete = bool((sync_state.value or {}).get("backfillComplete")) if sync_state else False
                if not backfill_complete:
                    logger.info("Email Intelligence is awaiting a complete initial backfill; worker will not start it automatically.")
                else:
                    result = sync_email_all(require_existing_delta=True)
                    logger.info("Email incremental synchronization complete: %s", result)
        except Exception:
            logger.exception("Email incremental synchronization failed")
        time.sleep(INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
