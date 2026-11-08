# PostgreSQL shared trial store — 2026-11-08

## Purpose

SQLite remains the default single-host trial store. Active-active receiver
replicas must not point at separate SQLite files and claim one shared history.

PostgresTrialStore is an opt-in implementation of the same receiver API. One
PostgreSQL primary and its transactional unique constraints provide the shared
identity boundary for multiple AVRONE receiver processes and hosts.

## Configuration

Install the optional driver:

    python -m pip install '.[postgres]'

Set these deployment variables:

    AVRONE_TRIAL_STORE=postgres
    AVRONE_TRIAL_DATABASE_URL=postgresql://USER:PASSWORD@DB_HOST:5432/DB_NAME
    AVRONE_TRIAL_RECEIPT_SECRET=<independent secret of at least 32 characters>

The receipt secret must differ from both current and previous Fleet HMAC tokens.
All replicas sharing one history must use the same receipt secret. Keep DSNs and
secrets in a deployment secret manager, never in source control or PR metadata.

Selecting PostgreSQL without its DSN, driver, or reachable database fails
closed. The service does not silently fall back to per-host SQLite.

## Transaction and retry semantics

- trial_records.trial_id is a PostgreSQL primary key.
- INSERT ... ON CONFLICT DO NOTHING arbitrates concurrent first deliveries.
- The winner is read in a subsequent READ COMMITTED statement.
- Same digest returns the original signed receipt as a duplicate.
- Different digest for the same identity is quarantined; the first durable
  payload remains immutable.
- Delivery and quarantine writes share the acceptance transaction.
- Each batch item has its own transaction, matching the existing SQLite
  receiver: a partially committed batch can be retried with a new Fleet nonce.
- Schema bootstrap is serialized by a transaction-scoped advisory lock.

This is one-primary transactional idempotency, not multi-primary consensus,
global exactly-once side effects, or proof of statistical independence. A
disconnected receiver must return an error; the sender must retry. A retry after
an ambiguous commit is safe because identity uniqueness and receipts live in
the shared database.

## Operational constraints

Use a properly operated shared PostgreSQL service with TLS, backups, monitoring,
and tested failover. Do not direct writes to a read replica. Connection timeout
is 5 seconds, statement timeout is 10 seconds, lock timeout is 5 seconds.
Capacity planning must include one connection per active store operation.

Fleet HMAC still proves shared-secret group membership plus allowlisting. It is
not unique per-node identity or mTLS. The authorization boundary is unchanged:
evidence ingestion cannot authorize agent execution.
