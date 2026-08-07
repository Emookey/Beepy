# MBC Intelligence

This commit preserves a sanitized historical application snapshot of the
project that later became Beepy. MBC Intelligence was originally developed by
Jerry Sandy.

## Historical application architecture

- FastAPI application with a browser frontend
- PostgreSQL with pgvector-backed ticket retrieval
- background synchronization workers
- Microsoft identity integration
- Autotask ticket synchronization and citations
- local model inference and embedding support

Later historical snapshots add the Odysseus technical-assistance adapter and
email intelligence modules. Names such as MBC and Odysseus remain where they
describe historical behavior or compatibility interfaces.

## Configuration and deployment

The tracked `.env.example` contains placeholders only. Real credentials,
tenant identifiers, business email domains, private endpoints, allow-lists,
and machine-specific topology do not belong in Git.

Legacy deployment scripts are retained as historical application evidence.
They must not be treated as a current production runbook. Host installation,
networking, secrets injection, backup/restore, and production orchestration are
platform responsibilities.
