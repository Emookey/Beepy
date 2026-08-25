# Beepy

Beepy is Kalvin's business, ticket, project, and email intelligence application. This offline Git repository candidate is derived from the Phase 2 canonical MBC Intelligence source and is not approved for production deployment.

Beepy originated as MBC Intelligence and was originally developed by Jerry Sandy.

## Identity

- **Kalvin** is the overall AI workspace platform and ecosystem.
- **Kal** is the canonical general assistant application and owns technical model/provider selection.
- **Beepy** is the business intelligence application in this repository candidate.
- **Kalvin Core** is the planned primary compute host/deployment profile.

Beepy technical mode uses Kal's scoped, stateless `beepy-kal.v1` service contract. A single externally mounted Beepy service credential can access only Kal's dedicated capability and technical-chat routes. Authenticated Beepy users are carried only as hashed audit context and never select a Kal identity or token.

## Architecture

- Browser frontend served by FastAPI
- PostgreSQL 16 with pgvector
- Application, ticket synchronization worker, and email synchronization worker
- Autotask ticket synchronization and citation links
- Microsoft Entra sign-in
- Microsoft Graph email indexing and search
- Local Ollama functionality retained for Beepy-owned ticket/email retrieval dependencies
- Provider-neutral Kal shared-technical-knowledge integration
- Persistent project uploads

The current Compose file publishes the application on loopback port `9080`. It retains legacy T420 database, volume, secret-mount, network, and service assumptions for compatibility. Those identifiers are not the approved fresh Kalvin Core deployment contract.

## Configuration contract

`.env.example` contains names and placeholders only. Never commit a real `.env`, credentials, token files, private keys, email allow-lists, or Graph configuration containing private material.

Runtime configuration must be externally provisioned. The source currently expects:

- PostgreSQL connection settings
- Entra tenant/client settings and an allowed email domain
- Autotask API settings
- Ollama endpoints, model names, and Beepy-owned retrieval controls
- Kal scoped-service base URL, token-file path, and timeout
- externally mounted Kal service and email integration secret files
- an external persistent project-upload location

## Repository boundaries

The `backend/`, `Dockerfile`, `backend/requirements.txt`, static frontend, placeholder environment template, and source-focused documentation are Beepy repository material.

The following retained files need ownership and safety work before canonical-repository promotion:

- `install_goodwill.sh`
- `deploy.sh`
- `update.sh`
- `backup.sh`
- `logs.sh`
- `scripts/verify_goodwill.sh`
- `tailscale-policy.example.hujson`
- `docker-compose.yml`

They currently mix legacy T420 migration, host provisioning, platform orchestration, destructive update behavior, backup policy, and network configuration. The Phase 2B reports assign their proposed long-term ownership. Do not execute them as part of this offline staging phase.

## Deferred compatibility names

The candidate intentionally retains legacy identifiers when renaming could affect data or integrations, including:

- `/opt/mbc-intelligence`
- `mbc_intelligence` database defaults and `mbc` operating/database users
- the `postgres_data` Compose volume key
- legacy Tailscale groups, tags, and host URL examples
- the inactive `app.odysseus` module and symbols for source compatibility; canonical technical mode does not import or call them
- `ODYSSEUS_BASE_URL` and `ODYSSEUS_TOKEN_PATH` as fail-closed aliases for the corresponding `KAL_*` settings
- `odysseus-rag` as an input/display compatibility identifier mapped to the safe Kal technical path
- `beepy@mbc.local`, pending review of persisted project messages and API consumers
- legacy operational script filenames

See the Phase 3E audit workspace reports and manifests for the compatibility contract, naming impact map, validation evidence, and source hashes.

Project assistant synthesis and generative email summarization are intentionally deferred. Project and email business evidence stays inside Beepy and is never sent to Kal's technical route. Ticket answering remains Beepy-owned. Technical turns send only the current technical question, so persisted ticket, project, and email history cannot cross the Kal boundary.

## Promotion status

Static checks alone do not prove authentication, authorization, schema/data compatibility, email/ticket synchronization, model behavior, upload permissions, backup/restore, Tailscale routing, or external integration behavior. Canonical Git promotion and Kalvin Core deployment remain blocked until the handoff report's review and validation requirements are completed.
