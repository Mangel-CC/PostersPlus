# Resilience Roadmap

Tracking document for the ElfHosted fork's hosting-mode work. Each phase is a self-contained branch off the previous phase. After implementation, each phase is reviewed by Codex (cross-model second opinion) and the findings are noted here before the branch is considered ready to merge.

**Design constraints (apply to every phase):**

1. **Opt-in, not replacement.** Every new backend (Postgres, Redis, S3) must be selectable by env var. The upstream defaults (SQLite, local disk, in-process dicts) must keep working unchanged.
2. **Narrow, cherry-pickable commits.** Prefer thin adapters over invasive refactors so upstream can pull anything they want, and we can pull from upstream without merge hell.
3. **No behaviour change at the upstream defaults.** A private user pulling this fork must see identical behaviour to upstream unless they explicitly flip a flag.
4. **No third-party services baked into the default path.** Sentry, Prometheus, etc. are opt-in only.

---

## Phase 0 — Fork setup

**Branch:** `phase-0-fork-setup`
**Status:** in progress

- [x] README updated to identify this as ElfHosted's fork
- [x] RESILIENCE.md (this doc) created
- [ ] Codex review

**Notes:** no code changes. Repo structure remains upstream's. The eventual GitHub remote (`origin`) will be ElfHosted's fork; the current `origin` (UmbraProjects) should become `upstream` once the fork exists on GitHub.

---

## Phase 1 — Pluggable storage backend

**Branch:** `phase-1-storage` (off `phase-0-fork-setup`)
**Status:** implementation complete, codex review pending

**Goal:** allow swapping SQLite for PostgreSQL via `DATABASE_URL`. SQLite remains the default.

**Approach:**

- Introduce a thin `Storage` protocol in `cache.py` covering the operations the rest of the app uses (`get_rating`, `set_rating`, `get_composite`, etc.) — no ORM. All current callers go through this.
- Two implementations: `SQLiteStorage` (current behaviour, default) and `PostgresStorage` (psycopg3, opt-in when `DATABASE_URL` is set).
- Schema bootstrap moves to a tiny migrations module (`db/migrations/`). Numbered SQL files, idempotent. For SQLite we keep `CREATE IF NOT EXISTS`; for Postgres we run migrations on startup with an advisory lock so multiple replicas don't race.
- TTLs become SQL-side `WHERE cached_at > ?` filters (already are for SQLite — keep the same shape).
- Connection pooling: SQLite stays single-connection (as today); Postgres uses `psycopg_pool`.
- All blob storage (composite JPEG bytes) still goes through this layer — moving bytes to object storage is Phase 3.

**Migration story:** none required at first deploy. New installs pick a backend; existing installs stay on SQLite. A future one-shot copy tool (`scripts/migrate_sqlite_to_postgres.py`) can be added if needed but is not in scope for Phase 1.

**Acceptance:**

- Default `docker compose up` still works with no Postgres.
- Setting `DATABASE_URL=postgresql://...` and starting fresh works end-to-end.
- All existing endpoints produce identical JSON/image responses on both backends.

---

## Phase 2 — Pluggable coordination layer

**Branch:** `phase-2-coordination`
**Status:** implementation complete, codex review pending

**Scope (narrowed from original plan):**

The original plan included cross-replica render coalescing. On closer inspection that was the wrong abstraction — `asyncio.Future` and `asyncio.Event` aren't shareable across processes, and the shared metadata cache (Phase 1) already deduplicates the *result-storage* across replicas. The remaining problem is that **side-effect state** (rate-limit backoff, background-fetch claims) stays per-process. Phase 2 fixes only that.

**What moved:**

- **MDBList backoff** — when one replica hits a 429 from MDBList, every replica now sees the backoff window. Previously each replica re-discovered the backoff independently and burned an extra MDBList call per replica per failed title.
- **Background quality-fetch single-flight** — atomic `claim_inflight` (SETNX + EX) prevents two replicas scheduling the same imdb_id simultaneously.

**What stayed per-process (deliberately):**

- Render coalescing (`_render_inflight` Future dict, [main.py:71](main.py#L71)) — intra-process burst dedup is fine; the shared `final_poster_cache` covers cross-process redundancy at one wasted render per replica per cold poster, which is acceptable.
- Rating fetch coalescing (`_rating_fetch_inflight` Event dict, [main.py:104](main.py#L104)) — same reasoning; the shared rating cache plus the now-shared backoff cover the cross-replica case.

**API (`coordination/__init__.py`):**

- `async init()` / `async close()` — lifecycle, called from FastAPI lifespan.
- `ping()` — sync probe for `/ready` (Phase 4).
- `async is_backoff_active(namespace, key) -> bool`
- `async set_backoff(namespace, key, ttl_seconds)`
- `async clear_backoff(namespace, key)`
- `async claim_inflight(namespace, key, ttl_seconds=300) -> bool`
- `async release_inflight(namespace, key)`
- `async prune_expired()` — no-op on Redis; evicts expired dict entries on inprocess.

Namespaces are constants in `coordination/__init__.py` (`NS_RATING_BACKOFF`, `NS_QUALITY_BG`) so a typo can't silently fork the keyspace.

**Backends:**

- `coordination/inprocess.py` — `dict[(ns, key)] -> expiry_ts`, monotonic loop time. Matches upstream behaviour exactly. Default when `REDIS_URL` is unset.
- `coordination/redis_backend.py` — `redis.asyncio.Redis`, `SET NX EX`, `EXISTS`, `DELETE`. Fail-open on Redis outage (return "no backoff" / "claim ok") so a coord outage degrades to per-process behaviour instead of breaking poster serving.

**Cherry-pick notes:**

- main.py loses two module-level dicts (`_quality_bg_inflight`, `_rating_backoff`). The cherry-pick path: when upstream adds new per-process state, mirror it as a new coord namespace + add `await coord.*` calls at the existing sites. Each change is bounded to one new namespace + a few call sites.
- The rating-coalescing async-Event dict is untouched in this phase — upstream's logic shape there is preserved.

**Test results:**

- In-process coordinator: backoff round-trip, TTL expiry, inflight NX semantics. PASS.
- Redis coordinator: same tests against `redis:7-alpine`. Cross-client visibility verified by reading the key with a separate redis client. PASS.
- Image rebuild with `redis>=5.0` dep added. Boots cleanly in three modes:
  - SQLite + inprocess (upstream default): `Storage backend: sqlite / Coordinator backend: inprocess`
  - Postgres + inprocess: backend logs match.
  - Postgres + Redis (hosted): `Storage backend: postgresql / Coordinator backend: redis`. `/health` 200 in all modes.

**Known follow-ups:**

- Render concurrency cap and split health probes are Phase 4.
- Per-tenant rate-limit hooks will land in Phase 5/8; the coord namespace pattern is already shaped for it.

---

## Phase 3 — Pluggable image-bytes store

**Branch:** `phase-3-object-store`
**Status:** pending

**Goal:** large bytes (composite JPEGs, downloaded TMDB posters/logos) optionally land in S3-compatible storage with a CDN URL returned to clients. Default stays local disk + SQLite blobs.

**Approach:**

- New `BlobStore` protocol: `get(key) -> bytes | None`, `put(key, bytes, content_type)`, `url_for(key) -> str | None`.
- Two impls: `LocalBlobStore` (current behaviour), `S3BlobStore` (via `aioboto3` or `obstore`).
- `/poster` returns a 302 to `url_for(key)` when the backend offers one and `OBJECT_STORE_PUBLIC_URL` is configured; otherwise it serves bytes inline (current behaviour).
- Composite cache table loses the `image_data` BLOB column for the S3 path — replaced by `blob_key`.

**Acceptance:** with no extra env, identical to upstream; with S3 + CDN env, posters served from CDN with no app-CPU per request.

---

## Phase 4 — Bounded render concurrency + split health probes

**Branch:** `phase-4-render-bounds`
**Status:** pending

**Goal:** cap concurrent Pillow renders, return 503 with Retry-After when saturated, split `/health` into `/live` and `/ready`.

**Approach:**

- `RENDER_CONCURRENCY` env var (default = `os.cpu_count()`). `asyncio.Semaphore(N)` around `_composite_and_encode`.
- `/live` keeps current `/health` behaviour (process alive only).
- `/ready` checks DB ping + coordinator ping + (if configured) blob store HEAD. Returns 503 with structured reason on failure.
- `/health` aliases to `/live` for backwards compat.

**Acceptance:** load test shows 503 instead of OOM when saturated; k8s probes wire up cleanly.

---

## Phase 5 — Leader-elected background jobs + per-tenant rate limit

**Branch:** `phase-5-leader-and-ratelimit`
**Status:** pending

**Goal:** cache-prune and digital-release-poll fire from exactly one replica. Per-tenant rate limiting for the heavy `/poster` endpoint.

**Approach:**

- Leader election:
  - Postgres backend: `SELECT pg_try_advisory_lock(<job-specific-int>)` held for the lifetime of the in-process loop. Lock released on shutdown / connection drop.
  - SQLite backend: in-process is always leader (single-replica assumption).
- Per-tenant rate limit:
  - Tenant ID = `sha256(access_key)[:16]`.
  - Sliding-window counter via the Coordinator from Phase 2.
  - `RATE_LIMIT_RPS` env var (per tenant per second). Unset = unlimited (default).

**Acceptance:** scale Postgres-backed deployment to 3 replicas, observe exactly one prune log line per cycle.

---

## Phase 6 — Prometheus metrics + structured logging

**Branch:** `phase-6-observability`
**Status:** pending

**Goal:** `/metrics` endpoint with useful counters/histograms; logs become JSON when `LOG_FORMAT=json`.

**Approach:**

- `prometheus_client` ASGI middleware on `/metrics` (no auth — bind to internal port via reverse-proxy convention, or gate with `METRICS_ACCESS_KEY`).
- Metrics:
  - `postersplus_cache_lookups_total{table, result}` (hit/miss/stale)
  - `postersplus_upstream_calls_total{service, status}` (TMDB/MDBList/AIOStreams)
  - `postersplus_render_duration_seconds` histogram
  - `postersplus_render_inflight` gauge
  - `postersplus_composite_cache_size_bytes` gauge (sampled)
- Logs: standard `logging` config with `python-json-logger` formatter when `LOG_FORMAT=json`. Request ID via `contextvars` injected by ASGI middleware (`X-Request-ID` header pass-through / generation).

**Acceptance:** scrape and visualise on a local Prometheus + Grafana.

---

## Phase 7 — Upstream retries + circuit breaker + serve-stale

**Branch:** `phase-7-upstream-resilience`
**Status:** pending

**Goal:** transient TMDB/MDBList failures don't visibly break posters. Persistent failures degrade gracefully to stale cache.

**Approach:**

- `stamina` library (lighter than `tenacity` for httpx). Decorate upstream call sites with retry on `httpx.TransportError` and 5xx, capped at 3 attempts, jittered exponential backoff.
- Circuit breaker: in-process per-service via `pybreaker` or hand-rolled (small). Threshold 5 failures in 60s → open for 30s → half-open probe.
- Serve-stale: when circuit is open, the cache layer returns rows past their TTL with a `stale=True` flag. Caller decides whether to fall back to stale (yes for ratings, quality, metadata; no for digital release).

**Acceptance:** chaos test (point TMDB at a 5xx stub) — posters keep rendering using last-known data.

---

## Phase 8 — Per-tenant cache namespacing + quota accounting

**Branch:** `phase-8-tenancy`
**Status:** pending

**Goal:** when tenants supply their own TMDB/MDBList keys, cache and quotas are scoped so tenant A can't read or drain tenant B.

**Approach:**

- Cache keys gain a `tenant_id` prefix derived from the user-supplied key (`sha256(key)[:16]`) when keys are user-supplied. Operator-key requests share a global tenant ID.
- Quota tracking in Redis: `INCR` with TTL per `(tenant, upstream, bucket)`. Soft cap warns in logs; hard cap returns 429 with Retry-After.

**Acceptance:** two tenants, two API keys, verify cache isolation in a test.

---

## Phase 9 — Image hardening, secrets, k8s manifests

**Branch:** `phase-9-deploy`
**Status:** pending

**Goal:** production-grade container image and Kubernetes manifests.

**Approach:**

- Multi-stage Dockerfile: builder installs wheels, final image is `python:3.11-slim` (or distroless) without `build-essential`. Pin base image digest.
- `requirements.txt` pinned with hashes (`pip-compile --generate-hashes`).
- Compose hardening: `read_only: true`, `tmpfs: [/tmp]`, `cap_drop: [ALL]`, `security_opt: [no-new-privileges:true]`, non-root user (already done).
- Minimal Helm chart or kustomize base under `deploy/k8s/`:
  - Deployment + HPA + PDB
  - ConfigMap (non-secret env) + ExternalSecret (or SealedSecret) for keys
  - Service + (optional) Ingress
  - NetworkPolicy: egress allowlist to TMDB/MDBList/AIOStreams/S3 endpoints only

**Acceptance:** `helm install` (or `kubectl apply -k`) brings up a working hosted instance against external Postgres + Redis + S3.

---

## Codex review log

Per-phase second opinion via `~/.claude/bin/codex-review`. Findings recorded inline below.

### Phase 0 — reviewed

Codex review (2026-05-23) flagged:

- **[P2]** README hosted-mode quickstart documented env vars (`DATABASE_URL`, `REDIS_URL`, `OBJECT_STORE_*`) the code does not yet read — risk of operators believing hosted backends are live. **Fixed:** added "target state" framing and a callout pointing at this doc for current status.
- **[P3]** README pointed at `.env.example` which doesn't exist (upstream ships `.env` directly). **Fixed:** link now points at `.env`.

No code review since Phase 0 is docs-only.

### Phase 1 — reviewed pending

**Implementation summary:**

- `storage/sqlite_backend.py` — near-verbatim extraction of upstream's cache.py SQLite logic. Adds `ping()` and `close()` helpers for /ready probes and graceful shutdown.
- `storage/postgres_backend.py` — new. Same public surface, psycopg3 + `psycopg_pool.ConnectionPool`. Filesystem-backed TMDB poster/logo helpers re-exported from the SQLite module so Phase 3 only has to touch one place. Schema bootstrap protected by a Postgres advisory lock so concurrent replica startup doesn't race.
- `storage/__init__.py` — backend selector. Reads `config.DATABASE_URL`; falls back to SQLite when unset. Re-exports the public surface as module-level callables; sets `BACKEND_KIND` for callers that need to know.
- `cache.py` — reduced to a thin re-export shim from `storage`. Every `from cache import …` callsite (main.py / tmdb.py / quality.py / digital_release.py) keeps working unchanged.
- `config.py` — adds `DATABASE_URL`, `DB_POOL_MIN_SIZE`, `DB_POOL_MAX_SIZE`.
- `main.py` — adds `close as close_db` to the cache import block and one `close_db()` call at the end of `lifespan()`.
- `requirements.txt` — adds `psycopg[binary,pool]>=3.1`. Always installed; postgres_backend module is only imported when DATABASE_URL is set, so the dep is otherwise inert.
- `compose.yaml` — adds DATABASE_URL/pool env vars; adds an optional `postgres` service behind `--profile hosted`.
- `.env` — documents the new env vars.
- `.dockerignore` — excludes RESILIENCE.md from the image build context (matches LICENSE/README.md pattern).

**Test results:**

- SQLite mode: full round-trip on all cache tables (rating, quality, metadata, trending, composite, digital release) + ping + prune. Container boots, `/health` returns 200. Backend logged at startup.
- Postgres mode: same round-trip against Postgres 16-alpine in a fresh container. All 6 expected tables created. Idempotent re-init verified. Upsert behaviour verified (INSERT ON CONFLICT DO UPDATE). Dedup behaviour verified (INSERT ON CONFLICT DO NOTHING).
- Image builds clean with the new `psycopg[binary,pool]` dependency.

**Known follow-ups (not in scope for Phase 1):**

- `image: PostersPlus` in compose.yaml is uppercase and trips modern Docker — defer to Phase 9 (deploy hardening) where compose.yaml gets a full rework.
- No SQLite→Postgres data migration tool. Operators starting fresh on Postgres get an empty cache that warms on first use. A `scripts/migrate_sqlite_to_postgres.py` can be added later if anyone asks.

### Phase 1 — reviewed

Codex review (2026-05-23): **clean, no correctness issues found**. Verbatim:
> "No discrete correctness issues were found in the current staged, unstaged, and untracked code changes. The SQLite shim/extraction preserves the existing public API, and the Postgres backend appears consistent with the same call surface."

Post-review self-audit found one minor robustness issue (not flagged by Codex):

- The `postgres://` URL scheme (Heroku/legacy alias) was accepted by the selector but psycopg3 only natively parses `postgresql://`. **Fixed:** selector now rewrites `postgres://` → `postgresql://` before the pool is opened.

Phase 1 ready to merge once user signs and commits.

### Phase 2 — reviewed

Codex review (2026-05-23): **clean, no correctness/security/performance/maintainability issues found**. Verbatim:
> "No discrete correctness, security, performance, or maintainability issues were identified in the current staged, unstaged, or untracked changes."

Self-audit notes (not findings, just explicit rationales for future readers):

- `coord.is_backoff_active` and `claim_inflight` fail-open on Redis errors so a transient coord outage degrades to per-process behaviour rather than 500ing the poster endpoint. The cost is one wasted upstream call per replica per affected title until Redis recovers.
- Coordinator init pings Redis at startup; misconfiguration fails fast at lifespan boot rather than silently swallowing every coord call later.
- In-process `claim_inflight` is race-free in asyncio (single-threaded; no `await` between get and set).

Phase 2 ready to merge once user signs and commits.

...
