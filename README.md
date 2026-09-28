# The Supplies — Enterprise Automation Blueprint

This repository contains an English/Thai enterprise automation blueprint and a **safe webhook-intake starter**. It is not a turnkey company-wide integration, legal certification, data lake, blockchain, or live deployment.

## เอกสาร / Documents

- [พิมพ์เขียวภาษาไทย](docs/blueprint.th.md)
- [English blueprint](docs/blueprint.en.md)
- [คำสั่งต้นแบบภาษาไทย](docs/implementation-prompt.th.md)
- [Reusable implementation prompt (English)](docs/implementation-prompt.en.md)
- [Architecture diagram source (Mermaid)](docs/architecture.mmd)
- [OpenAPI contract](openapi.yaml)

## Source scaffold

The Python standard-library service accepts JSON webhooks only when the per-source HMAC signature and timestamp are valid. It rejects stale requests, deduplicates event IDs, detects reuse of an event ID with a different body, and persists a SHA-256 digest rather than the request payload. It does **not** call any provider API or execute downstream business actions.

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
PYTHONPATH=src AUTOMATION_HOST=127.0.0.1 AUTOMATION_PORT=8080 \
  AUTOMATION_DB_PATH=./data/receipts.sqlite3 \
  WEBHOOK_SECRET_EXAMPLE_SOURCE='use-a-random-local-only-secret' \
  python3 -m supply_automation.server
```

Check readiness with `GET http://127.0.0.1:8080/healthz`. Send signed events to `POST /v1/events/example_source`. The signature covers `timestamp + "." + event_id + "." + raw_body`, encoded as HMAC-SHA256 and sent in `X-Webhook-Signature: sha256=<hex>`, alongside `X-Event-Timestamp` (Unix seconds) and `X-Event-Id`.

For production, use a durable queue and managed database, TLS at a hardened ingress, a secrets manager, workload identity, rate limits, monitoring/alerting, backup and restore tests, and an independently reviewed privacy/security design. Do not expose the demo SQLite store as a company-wide data lake.

## Important boundary

The company domain, exact SaaS/ERP/CRM/finance/supply-chain products, countries, data locations, APIs, owners, credentials, and authorized write scopes have not been provided. Example configuration therefore remains disabled and contains placeholders. DNS changes, live API credentials, broad data migration, financial actions, and legal attestations are intentionally not performed. The 30-day plan is a gated pilot/roadmap, not a promise that every system can safely be integrated in 30 days.
