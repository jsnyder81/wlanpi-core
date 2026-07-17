# Unified Authentication for wlanpi-core (MCP-ready)

## Context

A new MCP server (`wlanpi-mcp`) will consume wlanpi-core, mostly from off-box. Today core runs two schemes: HMAC-SHA256 with a machine-wide root-owned secret for loopback clients, and DB-backed HS256 "JWTs" for everyone else, with nginx choosing the path by rewriting `X-Real-IP` off an `X-Wlanpi-Client: mcp` header. Friction is already visible: the MCP service can't read the HMAC secret (hence the header hack), JWT expiry is not enforced (`time_validation_enabled = False`, `wlanpi_core/core/token.py:121`), tokens are HS256-symmetric so only core can verify them, and every new service needs bespoke integration.

**Decision (user-confirmed):** evolve core into an OAuth-shaped device token service — short-lived-ish, asymmetrically signed bearer JWTs with scopes, JWKS discovery, and an on-device pairing flow — while **keeping the HMAC loopback path untouched** for legacy on-box clients (touch UI, getjwt, lhapitest, FPMS). Threat model: LAN + occasional remote. Full OAuth 2.1 is deferred (Phase 3) but nothing may block layering it on later.

## No-RTC clock constraint (governs the whole design)

WLAN Pi platforms (RPi4/CM4 etc.) have **no hardware RTC**; the clock is restored by fake-hwclock at boot and is only accurate after NTP sync — which may never happen on isolated test networks. This is why `time_validation_enabled=False` exists today. Rules that follow:

- **DB-backed validation (token row exists + not revoked) is the permanent, primary control on core.** It is clock-independent. `exp`/`iat` are defense-in-depth, never the sole gate.
- **Sync-gated time enforcement:** enforce time claims only when the clock is known-synced. Detection: `/run/systemd/timesync/synchronized` exists (systemd-timesyncd), fallback `timedatectl show -p NTPSynchronized --value == "yes"`. Cache result ~60 s.
- **Leeway:** ±600 s on all time-claim checks; document the same for off-box verifiers.
- **Access-token TTL: 24 h** (config default), not minutes. Immediate revocation comes from the DB check, not TTL.
- **Issuance while unsynced:** still issue tokens (never strand offline users); log a warning; pairing CLI prints a clock warning.
- Off-box verifiers (MCP server) get offline signature/claim verification via JWKS; authoritative revocation stays with core (every real API call lands on core anyway).

## Current-code map (for implementers)

| Concern | Location |
|---|---|
| Auth dispatch (HMAC vs JWT by client IP) | `wlanpi_core/core/auth.py` — `verify_auth_wrapper` (:20), `verify_hmac` (:57), `verify_jwt_token` (:38), `is_localhost_request` (:106) |
| Token issue/verify/revoke, HS256, caches | `wlanpi_core/core/token.py` — `TokenManager.create_token` (:283), `verify_token` (:376), `revoke_token` (:489), `rotate_key` (:588), `purge_expired_tokens` (:537); `TokenValidationResult.is_expired` hardcoded `False` (:86) |
| Secrets (HMAC shared secret, Fernet key) | `wlanpi_core/core/security.py` — `SecurityManager`; files under `SECRETS_DIR` (`wlanpi_core/constants.py`: `/home/wlanpi/.local/share/wlanpi-core/secrets`) |
| DB models | `wlanpi_core/core/models.py` — `SigningKey`, `Token`, `APIDevice` (+ activity/stats) |
| Repositories | `wlanpi_core/core/repositories.py` — `DeviceRepository`, `TokenRepository` |
| Auth endpoints | `wlanpi_core/api/api_v1/endpoints/auth_api.py` — `POST/DELETE /auth/token`, HMAC-gated `/auth/signing_key(s)`, debug endpoints |
| App wiring | `wlanpi_core/app.py` — security manager (~:452), db manager (~:482), token manager + purge task (~:494), routers (~:610) |
| Config | `wlanpi_core/core/config.py` — `ACCESS_TOKEN_EXPIRE_DAYS = 7` |
| nginx routing / header hack | `install/etc/wlanpi-core/nginx/wlanpi_core.conf` (+ mirrored `debian/wlanpi-core/etc/wlanpi-core/nginx/sites-enabled/wlanpi_core.conf`) — `map $http_x_wlanpi_client` → `X-Real-IP` |
| Legacy HMAC clients | `wlanpi_core/cli/getjwt.py`, `install/usr/bin/lhapitest`, `backup-install/usr/bin/getjwt` |
| Unauthenticated surface | `WS /api/v1/streaming/capture` (no auth today) |
| Docs | `docs/API-INTEGRATION-GUIDE.md` |

**Schema-migration constraint:** the SQLite DB (`tokens.db`) is created via SQLAlchemy `create_all` on device; there is no Alembic. Prefer **new tables** over altering existing ones. The design below adds no columns to existing tables.

---

# Phased implementation plan

Phases are independently shippable. Within a phase, items are ordered by dependency. Each item lists files, concrete changes, and acceptance criteria (AC).

## Phase 0 — Groundwork (small, no behavior change)

### 0.1 Config knobs
- **File:** `wlanpi_core/core/config.py`
- Add: `ACCESS_TOKEN_EXPIRE_HOURS: int = 24`, `TOKEN_CLOCK_LEEWAY_SECONDS: int = 600`, `TOKEN_ISSUER: str = "wlanpi-core"`, `TOKEN_AUDIENCE: str = "wlanpi-core"`. Keep `ACCESS_TOKEN_EXPIRE_DAYS` (legacy path still uses it until Phase 2).
- **AC:** app boots; existing tests pass.

### 0.2 Clock-sync helper
- **New file:** `wlanpi_core/core/clock.py`
- `def is_clock_synced() -> bool`: return `True` if `/run/systemd/timesync/synchronized` exists; else fall back to `timedatectl show -p NTPSynchronized --value` == `yes`; on any error return `False`. Module-level cache with ~60 s TTL (monotonic clock — `time.monotonic()`, never wall clock).
- **AC:** unit test with `tmp_path`/mocked subprocess covering: file present, file absent + timedatectl yes/no, command failure → `False`, cache honored.

### 0.3 Scope vocabulary
- **New file:** `wlanpi_core/core/scopes.py`
- Define scope constants and a router→required-scope map. Initial vocabulary (adjust to router list in `wlanpi_core/api/api_v1/api.py`): `system:read`, `system:write`, `network:read`, `network:write`, `wifi:read`, `wifi:write`, `bluetooth:read`, `bluetooth:write`, `utils:read`, `profiler:*`, `capture:read`, `auth:manage`. Include `SCOPE_ALL = "*"` for admin/legacy credentials.
- **AC:** importable; no wiring yet.

## Phase 1 — Token hardening (asymmetric signing, JWKS, clock-aware claims, WS auth)

### 1.1 Ed25519 issuer keypair in SecurityManager
- **File:** `wlanpi_core/core/security.py`
- Add `_setup_token_keypair()` mirroring `_setup_shared_secret()`: generate Ed25519 keypair (`cryptography` lib — confirm it's already a transitive dep of authlib; if not, add to `pyproject`/`debian/control`), store private key PEM at `SECRETS_DIR/token_ed25519.pem` (`root:wlanpi`, `0o640` — same pattern as `shared_secret.bin`), public key PEM at `SECRETS_DIR/token_ed25519.pub` (0644). Expose `token_private_key_pem` / `token_public_key_pem` properties. Compute `kid` as RFC 7638 JWK thumbprint (base64url SHA-256 of the OKP JWK); expose as `token_kid`.
- **Registration row:** on first startup after upgrade, `TokenManager` inserts a `SigningKey` row whose `key` column holds the **public** key PEM with `active=True`, and deactivates prior rows **without revoking existing tokens** (unlike `rotate_key` — legacy HS256 tokens must keep verifying via their own `kid` rows during transition). Implement as a new `_ensure_asymmetric_key(session)` — do not reuse `_get_or_create_signing_key`'s revoke-all behavior.
- **AC:** fresh install and upgrade-in-place both end with exactly one active signing-key row containing a PEM; legacy HS256 rows remain, inactive, tokens on them still valid.

### 1.2 Issue EdDSA tokens with OAuth-shaped claims
- **File:** `wlanpi_core/core/token.py` — `create_token`
- New signature: `create_token(device_id, expires_delta=None, scopes: str = "*")`.
- Header: `{"alg": "EdDSA", "kid": <thumbprint kid>}`. Sign with private key PEM from `SecurityManager`.
- Claims: keep `sub`, `iss` (from `settings.TOKEN_ISSUER`), `did`, `jti`; add `aud` = `settings.TOKEN_AUDIENCE`, `scope` = space-delimited string; `iat`/`exp` as now but default TTL `ACCESS_TOKEN_EXPIRE_HOURS`. If `not is_clock_synced()`: log warning `"issuing token with unsynced clock"`.
- Still insert a `Token` row (DB-backed validation is permanent); `key_id` = the asymmetric key's row id.
- **AC:** issued token verifies against the public key with `authlib`; claims complete; token row present.

### 1.3 Clock-aware verification, dual-algorithm support
- **File:** `wlanpi_core/core/token.py` — `verify_token`, `TokenValidationResult`
- Keep the DB lookup + revoked check exactly as-is (primary control).
- After loading the signing-key row: peek at the JWT header alg. `EdDSA` → verify with PEM public key; `HS256` → legacy base64 secret (transition support). Restrict acceptable algs to exactly these two (`jwt.decode(..., claims_options=...)` / authlib `JsonWebToken(["EdDSA","HS256"])` — never accept `none`).
- Replace the `time_validation_enabled` flag with: **if `is_clock_synced()`**, validate `exp`/`iat` with `settings.TOKEN_CLOCK_LEEWAY_SECONDS` leeway (authlib `claims.validate(leeway=...)`), plus `iss`; validate `aud` only when the claim is present (legacy tokens lack it). **If unsynced**, skip time claims, keep required-claims/issuer checks, log once per interval. Fix `TokenValidationResult.is_expired` (:86) to compute real expiry but only *report* it (validity decision stays in `verify_token`).
- Cache path (`token_cache`) must apply the same sync-gated expiry logic.
- **AC (tests):** valid EdDSA token passes; expired token rejected when synced-mock=True, accepted (DB-valid) when synced-mock=False; revoked token rejected regardless of clock; legacy HS256 token still passes; `alg=none` and HS256-signed-with-public-key-as-secret both rejected.

### 1.4 JWKS + discovery endpoints
- **New file:** `wlanpi_core/api/wellknown.py` (mounted in `app.py` at root, *outside* `API_V1_STR`, no auth dependency)
- `GET /.well-known/jwks.json` → `{"keys": [<OKP JWK of the active public key, with kid, use:"sig", alg:"EdDSA">]}`. Only asymmetric keys are published (never HMAC secrets).
- `GET /.well-known/oauth-authorization-server` → minimal RFC 8414 doc: `issuer`, `token_endpoint` (`/api/v1/auth/token`), `jwks_uri`, `grant_types_supported: ["refresh_token"]` (Phase 2), `scopes_supported` from `scopes.py`. Fine to ship in Phase 2 if preferred; jwks.json ships now.
- **AC:** `curl` both endpoints unauthenticated; a standalone script (simulating the MCP server) verifies a freshly issued token using only jwks.json.

### 1.5 Scope-enforcement dependency (wiring deferred)
- **File:** `wlanpi_core/core/auth.py`
- `def require_scopes(*needed) -> Depends`: runs `verify_auth_wrapper`; HMAC-authenticated callers implicitly have `*`; JWT callers must have `*` or all `needed` scopes in their `scope` claim (missing claim ⇒ legacy token ⇒ treat as `*` during transition; flip to deny in Phase 2 cleanup). 403 with `insufficient_scope` detail on failure.
- Wire it **only** onto the streaming/capture surface in Phase 1 (see 1.6); other routers keep plain `verify_auth_wrapper` until Phase 2.
- **AC:** unit tests for scope math (exact, wildcard, missing-claim transition behavior).

### 1.6 Authenticate the capture WebSocket
- **File:** the streaming endpoint module (`wlanpi_core/api/api_v1/endpoints/` — locate `streaming`/capture WS route)
- WebSockets can't use `HTTPException` deps cleanly: accept the connection, read token from `Authorization` header or `?token=` query param, run `token_manager.verify_token` + scope check (`capture:read`), and `await ws.close(code=1008)` on failure before any data flows. Loopback callers may instead present the HMAC header on the upgrade request (verify with `verify_hmac`-equivalent logic against the upgrade request).
- **AC:** unauthenticated WS connect is closed with 1008 and streams nothing; valid JWT with `capture:read` (or `*`) streams; documented in API guide.

### Phase 1 verification (end-to-end)
1. On a dev box: start service, `getjwt` (HMAC) still returns a token — regression.
2. Issue token → decode header shows `EdDSA` + kid → verify via `/.well-known/jwks.json` from a separate Python process.
3. `date -s` the clock hours backward (or mock): API calls with the token still succeed (DB path), warning logged; restore clock+NTP: expired tokens now 401.
4. `lhapitest` passes; capture WS refuses anonymous connect.

## Phase 2 — Credential lifecycle (clients, refresh, pairing, MCP credential, nginx cleanup)

### 2.1 Client registry model + repository
- **Files:** `wlanpi_core/core/models.py`, `wlanpi_core/core/repositories.py`
- New table `api_clients` (new table only — no ALTERs): `client_id` (str pk, `secrets.token_urlsafe(16)`), `name` (str, human label), `scopes` (Text, space-delimited), `refresh_token_hash` (Text, sha256 hex of the refresh secret), `created_at`, `last_used`, `revoked` (bool). New `ClientRepository` with `create`, `get_by_client_id`, `verify_refresh_token(client_id, presented_secret)` (constant-time compare of sha256), `revoke`, `list`, `rotate_refresh_token`.
- Refresh secret: `secrets.token_urlsafe(48)`, shown once at pairing, stored only hashed.
- **AC:** table auto-creates on upgrade via existing `create_all` path; repo unit tests.

### 2.2 `grant_type=refresh_token` on POST /auth/token
- **Files:** `wlanpi_core/schemas/auth.py`, `wlanpi_core/api/api_v1/endpoints/auth_api.py`, `wlanpi_core/core/token.py`
- Extend `TokenRequest`: optional `grant_type` (`"refresh_token"`), `client_id`, `refresh_token`. **Back-compat:** absent `grant_type` ⇒ existing behavior (HMAC-gated `device_id` issuance) unchanged.
- New branch in `generate_token`: `grant_type=refresh_token` requires **no other auth** (the refresh token is the credential — remove/bypass `verify_auth_wrapper` for this branch by checking grant_type before auth, or split into `POST /auth/token` dependency-free with internal dispatch; keep the HMAC requirement for the legacy `device_id` branch). Validate via `ClientRepository.verify_refresh_token`; reject revoked clients (401, `invalid_grant`); issue access token with the client's stored scopes, `did` = `client_id`; update `last_used`.
- Revoking a client must also revoke its live access tokens: `UPDATE tokens SET revoked=1 WHERE device_id = :client_id` (works because `did`/`device_id` = `client_id` for client-issued tokens; `DeviceRepository.get_or_create_device` keeps FK happy).
- **AC (tests):** refresh grant issues scoped token with no HMAC and no bearer; wrong/revoked refresh secret ⇒ 401; legacy `device_id`+HMAC flow byte-for-byte unchanged; client revocation kills existing access tokens on next request.

### 2.3 Client-management endpoints (HMAC/loopback-gated, hidden)
- **File:** `auth_api.py`
- `POST /auth/clients` (create: name, scopes → returns client_id + refresh_token **once**), `GET /auth/clients` (list, no secrets), `DELETE /auth/clients/{client_id}` (revoke), `POST /auth/clients/{client_id}/rotate` (new refresh secret). All `dependencies=[Depends(verify_hmac)]`, `include_in_schema=False` — pairing authority stays rooted in on-box/admin access, same trust anchor as today.
- **AC:** endpoint tests through the HMAC path.

### 2.4 Pairing CLI
- **Files:** new `wlanpi_core/cli/pair.py`; register console script alongside existing getjwt packaging (`debian/wlanpi-core/opt/wlanpi-core/bin/`, plus `debian/rules`/install lists as done for `getjwt`)
- `wlanpi-core-pair --name <label> [--scopes "..."] [--qr]`: signs an HMAC request (reuse the canonical-string logic — copy the *server's* format `METHOD\npath\nquery\nbody` from `auth.py:79`, not getjwt's divergent one) to `POST /auth/clients`, prints `client_id`, refresh token, token endpoint URL, and — with `--qr` — a QR code (use `qrcode` lib only if already packaged; otherwise print a copy-paste JSON blob). If `not is_clock_synced()`: print a clock warning.
- Keep `getjwt` untouched (legacy contract).
- **AC:** run on-box as root/wlanpi-group member → credentials issue; a laptop uses them to get an access token and call the API.

### 2.5 First-class MCP service credential (removes the nginx hack)
- Core side: pairing CLI is sufficient — the `wlanpi-mcp` package's postinst runs `wlanpi-core-pair --name wlanpi-mcp --scopes "..."` and installs the result via systemd `LoadCredential=`/credential file readable only by the MCP service user. (Document the recipe in `docs/API-INTEGRATION-GUIDE.md`; actual postinst lives in the MCP repo.)
- **nginx cleanup:** in `install/etc/wlanpi-core/nginx/wlanpi_core.conf` **and** the mirrored `debian/.../sites-enabled/wlanpi_core.conf`: the `X-Wlanpi-Client` map becomes unnecessary — but **only remove it after** `verify_auth_wrapper` gains: loopback callers presenting a Bearer token (and no `X-Request-Signature`) are routed to JWT verification instead of failing HMAC. Concretely in `auth.py:verify_auth_wrapper`: `if is_localhost_request(request) and credentials and "X-Request-Signature" not in request.headers: return await verify_jwt_token(...)`. This preserves "HMAC path untouched" for legacy clients while letting on-box JWT clients work without header games.
- **AC:** on-box client with only a Bearer token (no special headers) reaches JWT auth through nginx with the map deleted; legacy HMAC clients unaffected; `X-Wlanpi-Client` header now a no-op (harmless if old MCP builds still send it).

### 2.6 Scope wiring + transition flip
- Apply `require_scopes(...)` per router in `wlanpi_core/api/api_v1/api.py` endpoint modules per the `scopes.py` map (pattern: replace `dependencies=[Depends(verify_auth_wrapper)]` with `dependencies=[require_scopes(SCOPE_X)]`; representative files: `system` and `network_config` endpoint modules — repeat across the rest).
- Flip the 1.5 transition rule: JWT without a `scope` claim now gets **no** scopes (legacy 7-day HS256 tokens will have aged out; document in release notes).
- Deprecate (log-warn, keep working) the `device_id` HMAC issuance branch; slate removal for a later release.
- **AC:** token scoped `system:read` gets 403 on `network:write` routes; `*`-scoped and HMAC callers unaffected.

### 2.7 Docs
- Rewrite the auth section of `docs/API-INTEGRATION-GUIDE.md`: pairing flow, refresh grant, JWKS verification recipe for external services, clock/leeway guidance, scope table, legacy HMAC appendix.

### Phase 2 verification (end-to-end)
1. Pair a client on-box → from a laptop: refresh grant → access token → scoped calls succeed / out-of-scope 403.
2. Verify token signature+claims from a separate process via jwks.json (MCP-server simulation).
3. Revoke the client → in-flight access token 401s immediately (DB check), refresh grant 401s.
4. Regression: `lhapitest`, `getjwt`, touch-UI flows (HMAC) — unchanged. nginx map removed; on-box bearer client works.
5. Clock-skew: repeat Phase 1 test #3 with a refresh-granted token.

## Phase 3 — Later / optional (not in scope now)
- Full OAuth 2.1: authorization-code + PKCE flow and dynamic client registration for MCP-native client auth (claims/endpoints from Phases 1–2 are already compatible; add `authorization_endpoint` to the RFC 8414 doc).
- TLS on :31415 by default (self-signed device cert, trust-on-first-use; nginx `ssl` listener).
- Retire the legacy `device_id` HMAC issuance branch and HS256 verification support.

## Out of scope / must-not-break (all phases)
- `verify_hmac` and the loopback HMAC contract (`METHOD\npath\nquery\nbody`, `X-Request-Signature`) — byte-for-byte stable.
- `getjwt`/`lhapitest` behavior until their consumers migrate.
- No ALTERs to existing SQLite tables; new tables only.
- Never publish symmetric key material via JWKS; never accept `alg=none` or unexpected algs.
