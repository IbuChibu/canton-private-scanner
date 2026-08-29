# Canton Private Scanner — Local Demo Plan

## Goal

Turn the working private-ledger scanner into a polished local web application
while preserving the existing correctness guarantees:

- balances start from an exact-offset Active Contract Set;
- live updates resume from the saved offset;
- all subscriptions remain scoped to authorized, actively tracked parties;
- SQLite state survives service restarts;
- the browser never receives Canton credentials or secrets.

The frontend uses plain HTML, CSS, and JavaScript. FastAPI serves the frontend
and API from one local origin and can supervise one scanner worker against the
existing persistent SQLite database. Cloud deployment is intentionally out of
scope for this demo.

## Product Experience

The application will be a responsive private-ledger operations dashboard with
four primary areas:

1. **Scanner status** — stream state, last offset, catalog freshness, active and
   desired party counts, and a clear restart/reconciliation state.
2. **Party explorer** — searchable, paginated authorized-party catalog with
   readable, selected, and actively indexed states.
3. **Balance view** — current balances for the focused active party, grouped by
   instrument and showing the index offset.
4. **Transfer activity** — semantic transfers for the focused party with
   direction, counterparty, amount, instrument, record time, and offset.

Use a modern dark operations-console visual style with strong contrast, a
compact desktop layout, a single-column mobile layout, visible focus states,
and reduced-motion support. All ledger-provided strings must be inserted with
`textContent`, never raw HTML.

## Architecture Decisions

- Add `frontend/index.html`, `frontend/styles.css`, and `frontend/app.js`; do
  not introduce Node, a bundler, or a JavaScript framework.
- Serve `/` and `/assets/*` from FastAPI. Keep `/health`, `/parties`,
  `/balance/*`, `/history/*`, `/debug/*`, and `/docs` as API routes.
- Use same-origin browser requests, so production CORS configuration is not
  required.
- Keep the current manual `scanner.py` and `updates.py` commands for debugging.
  Add an opt-in local worker enabled by `SCANNER_RUN_WORKER=1`.
- Run exactly one Uvicorn worker and one scanner worker. Multiple service
  instances are forbidden while SQLite is the database.
- Keep `SCANNER_DB` on the local filesystem and preserve it between demo runs.
- Make the Ledger WebSocket URL configurable with `C8_WS_URL`, retaining the
  current DevNet URL as the default.
- Public read endpoints remain accessible on the local demo service. Protect
  party-selection mutation with `SCANNER_ADMIN_TOKEN`; if it is unset,
  mutation is disabled.
  The frontend may accept the token in an admin-unlock dialog and retain it in
  memory for the current tab only. It must never be embedded in static files or
  written to local storage.

## Backend Interface Additions

Preserve every existing response field and add only backward-compatible data:

- Add `GET /parties/selection` returning desired and active party arrays,
  counts, revisions, and `restart_required`. This lets the browser edit a
  selection across multiple catalog pages without losing off-page choices.
- Keep `PUT /parties/selection`, but require
  `Authorization: Bearer <SCANNER_ADMIN_TOKEN>`. Continue validating
  full readable catalog IDs, deduplication, non-empty input, and the configured
  maximum.
- Extend `GET /health` with a `stream` object containing `status`,
  `last_heartbeat`, `connected_at`, and the latest bounded error message.
- Add an SQLite singleton runtime-state row so the scanner subprocess can
  report `starting`, `discovering`, `reconciling`, `connected`, `retrying`, and
  `stopped` to the API process.
- Extend `GET /history/{party}` with optional `offset=0`, and return `limit`,
  `offset`, and `total` while retaining the existing `transfers` and `count`
  fields.

The browser will poll `/health` every 5 seconds. It will refresh the focused
party's balance and first history page every 10 seconds while the tab is
visible, immediately after a party change, and when the saved offset advances.
Only one request of each type may be in flight; stale requests must be aborted.

## Milestones

### Milestone 1 — Frontend shell and same-origin serving

**Status: complete.**

- Create semantic HTML for the header, status strip, party explorer, balance
  cards, transfer table, empty states, error banner, and admin dialog.
- Build a small CSS design system using custom properties for color, spacing,
  typography, borders, and responsive breakpoints.
- Mount static assets in FastAPI and return `index.html` from `/` without
  intercepting API or `/docs` routes.
- Add loading skeletons and useful no-JavaScript content.

**Acceptance:** `/` loads from FastAPI with no console errors; all API and docs
routes still resolve; layout works at 375 px, tablet, and desktop widths.

### Milestone 2 — Browser data layer and scanner status

**Status: complete.**

- Implement one fetch wrapper with JSON parsing, timeouts, abort support, and
  consistent handling for 400, 403, 404, 409, 422, 500, and network failures.
- Render bootstrap-required, catalog-refreshing, connected, retrying,
  reconciliation-required, and stale-heartbeat states without exposing secret
  error details.
- Display the saved offset, catalog refresh time, readable count, active count,
  desired count, and stream heartbeat.
- Pause polling when the document is hidden and refresh immediately on return.

**Acceptance:** status changes appear without a page reload; transient API
failures leave existing data visible and provide a retry action.

### Milestone 3 — Party explorer and controlled selection

**Status: complete.**

- Add 250 ms debounced server-side search and 50-row previous/next pagination
  using the cached `/parties` API.
- Distinguish readable, inaccessible, desired, active, and pending states with
  text and icons rather than color alone.
- Focus a party by clicking its row; persist only the focused party ID in the
  URL query string so dashboard links are shareable.
- Maintain the full desired set from `GET /parties/selection`, including parties
  not visible on the current page.
- Require admin unlock before enabling checkboxes. Show the configured maximum,
  validate locally, submit once, then display the pending-reconciliation state.

**Acceptance:** changing search pages never drops a selection; inaccessible
parties cannot be added; a successful update is reflected in both counts and
row badges.

### Milestone 4 — Balances and transfer activity

**Status: complete.**

- Load balances only for the focused actively indexed party and format Decimal
  strings without converting them through floating-point arithmetic.
- Display one balance card per instrument, with explicit zero/empty and inactive
  states.
- Render transfer direction, counterparty, amount, instrument, record time, and
  offset. Abbreviate parties visually while keeping the full ID available via
  copy action and accessible label.
- Add history pagination and refresh the first page when the ledger offset
  advances.

**Acceptance:** balance values match the API byte-for-byte; sent, received, and
self transfers render correctly; inactive historical parties retain history
but do not present a current balance.

### Milestone 5 — Local scanner worker and reconciliation loop

**Status: complete.**

- Add a worker loop that runs scanner bootstrap/reconciliation and then the live
  updater. When a desired revision becomes pending, it must close the stream,
  reconcile at the saved offset, and reconnect automatically.
- Check for a pending selection at least every 5 seconds even when the WebSocket
  receives no messages.
- Start the worker from FastAPI lifespan only when `SCANNER_RUN_WORKER=1`.
  Monitor the subprocess, restart it with bounded backoff after unexpected
  failure, and terminate it cleanly with the web process.
- Persist runtime heartbeat/status for the frontend. Never erase or reset an
  offset automatically after a pruning or permission error.

**Acceptance:** a browser selection change becomes active without shell access;
the API stays responsive during slow catalog discovery; stopping the web
service also stops the worker; replay remains idempotent.

### Milestone 6 — Local demo launch and operator setup

- Add one documented startup flow for the FastAPI dashboard and managed worker.
- Provide a safe local environment template covering `SCANNER_DB`,
  `SCANNER_RUN_WORKER`, `C8_BASE`, `C8_WS_URL`, `C8_IDP`, `C8_CLIENT_ID`,
  `C8_CLIENT_SECRET`, `SCANNER_ADMIN_TOKEN`, and optional `C8_USER`.
- Add startup checks that clearly report missing credentials, an unavailable
  participant, or an unwritable database without deleting scanner state.
- Document manual recovery commands and the rule that only one API/worker pair
  may use the SQLite database during the demo.

**Acceptance:** one local startup flow loads the dashboard, reaches
`connected`, preserves balances across a stop/start, and resumes from the
persisted offset.

### Milestone 7 — Submission validation and polish

- Add backend tests for selection authentication, runtime health, history
  pagination, worker restart/reconciliation, and static routing.
- Add browser tests for loading, errors, search, pagination, selection, balance
  formatting, history direction, responsive layout, keyboard operation, and
  secret non-persistence.
- Re-run the existing scanner suite to protect ACS, tree traversal, Decimal,
  replay, crash consistency, rights, and revision behavior.
- Perform a recorded DevNet demonstration: show correct ACS balances, observe an
  offset advance, kill/restart the service, and confirm resume from that offset.
- Capture at least one real qualifying transfer if DevNet provides a
  submit-capable party pair; otherwise retain the conservative empty state and
  clearly label fixture-only transfer coverage.

**Acceptance:** all automated checks pass, no browser console errors remain,
the local restart demonstration succeeds, and no credentials appear in the
repository, page source, logs, or API responses.

## Delivery Order

Implement milestones in order. Milestones 1–4 produce the complete local
frontend. Milestone 5 makes selection changes operable without shell access.
Milestone 6 creates the repeatable local demo startup. Milestone 7 is the
submission gate; do not call the project demo-ready before the live restart and
persistence checks pass.

## Out of Scope for This Version

- A framework migration, build pipeline, or component library.
- A global block explorer or `filtersForAnyParty` subscription.
- Transaction submission, wallet functions, or token transfers from the UI.
- Multi-user accounts, durable browser sessions, or role management beyond the
  single admin token.
- Cloud hosting, public deployment, or multiple service instances. Moving
  beyond one local instance requires a separate security and database design.
