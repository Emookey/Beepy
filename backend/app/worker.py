import logging
import time
from .config import get_settings
from .db import initialize_database
from .sync import sync_all

settings = get_settings()
logging.basicConfig(level=settings.log_level)

def main():
    initialize_database()
    while True:
        try:
            result = sync_all(force_full=False)
            logging.info("Autotask synchronization complete: %s", result)
        except Exception:
            logging.exception("Autotask synchronization failed")
        time.sleep(settings.sync_interval_seconds)

if __name__ == "__main__":
    main()
