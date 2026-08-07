from hashlib import sha256
from sqlalchemy import select

from .db import SessionLocal
from .models import Ticket
from .ollama import embed


BATCH_SIZE = 8


def main():
    completed = 0

    while True:
        with SessionLocal() as db:
            tickets = db.scalars(
                select(Ticket)
                .where(Ticket.embedding.is_(None))
                .where(Ticket.document_text != "")
                .order_by(Ticket.id)
                .limit(BATCH_SIZE)
            ).all()

            if not tickets:
                print("Embedding backfill complete.", flush=True)
                break

            vectors = embed([
                ticket.document_text
                for ticket in tickets
            ])

            for ticket, vector in zip(tickets, vectors):
                ticket.embedding = vector
                ticket.embedding_hash = sha256(
                    ticket.document_text.encode("utf-8")
                ).hexdigest()

            db.commit()
            completed += len(tickets)

            print(
                f"Embedded {completed:,} tickets.",
                flush=True,
            )


if __name__ == "__main__":
    main()
