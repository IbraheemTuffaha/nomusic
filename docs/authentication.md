# Backend authentication

M5 protects processing, status, playback media and exports with operator-issued
bearer keys. The backend has no account system: a trusted operator creates a
high-entropy key locally and gives it to an approved extension installation.
Keys are deployment credentials, not per-person private storage. Authorized
key holders share the backend's processed-media cache.

## Create and manage keys

Run these commands as the same operating-system user that runs the backend:

```sh
backend/.venv/bin/nomusic auth generate --label "my Mac"
backend/.venv/bin/nomusic auth list
backend/.venv/bin/nomusic auth revoke KEY_ID
backend/.venv/bin/nomusic auth rotate --revoke KEY_ID --label "replacement"
```

`generate` and `rotate` print the raw key once. Store it in the extension's
trusted settings; it is never written to the key file. The file defaults to
`~/.config/nomusic/operator-keys.json` and can be moved with
`NOMUSIC_AUTH_FILE=/absolute/path/to/operator-keys.json`. The backend refuses a
missing key file, a file with group/other permissions, or a file with no active
key. It creates new files as owner-only (`0600`) inside an owner-only directory.

Start the backend only after at least one key exists:

```sh
backend/.venv/bin/nomusic serve
```

Every protected request sends:

```http
Authorization: Bearer nm_<64 lowercase hexadecimal characters>
```

The key is not accepted in a URL, query parameter, page event, DOM attribute,
or ordinary log message. The extension keeps it in a trusted service-worker
context and sends it only to the configured backend.

## Route boundary

`GET /healthz` is the only unauthenticated route. It reports only reachability.
Readiness, capabilities, processing, client-interest leases, status, progress,
chunks, full audio and every export operation require an
active key. Missing and invalid credentials return `401`; a revoked key returns
`401` with a distinct `credential_revoked` code. A missing or invalid server
key configuration returns `503` without starting work.

The processing queue, GPU limits, disk budgets and export reader limits remain
deployment-wide admission controls. Authentication identifies an authorized
operator; it does not grant a separate unlimited quota.

## Revocation and open work

The server rereads the key file for authentication, so a local revoke or rotate
takes effect for the next request without restarting the backend. Revocation
does not rewind an HTTP response that has already been admitted: one bounded
chunk, status response, or already-open export download may finish. New status,
media, interest, cancellation and export requests are rejected. Existing worker
and cache lifetimes continue to use their normal bounded leases and TTL cleanup.

Cache administration is local too: `nomusic cache stats` and `nomusic cache clear`.
Stop the backend before a full clear; in-use namespaces remain leased.

Key management is deliberately local CLI administration. Do not add a remote
endpoint that creates, lists or revokes keys. Keep the key file outside the
repository and back it up only through the operating system's trusted storage.
