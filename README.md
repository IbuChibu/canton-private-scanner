# Canton private ledger scanner

This is a small, resumable indexer for the Cantor8 A1 scanner challenge. It
builds a local view of Canton Coin Holdings and semantic transfers for a
user-selected set of authorized parties by reading the **private Canton Ledger
API**. The original three demo parties remain the fresh-database defaults. The
public Scan API is not used as the source of scanner history.

## Why a private scanner?

Canton data is disclosed on a need-to-know basis. A participant sees ledger
events for parties it hosts or has rights to, rather than a network-wide public
transaction log. An application that needs fast balances, history, or reports
therefore maintains an off-ledger index of its own authorized view.

This scanner makes that boundary explicit: `/v2/updates` is filtered separately
for each tracked party. A wildcard means every template visible **to that
party**; it does not mean every party or event on the participant.

## Architecture

```mermaid
flowchart LR
    R["Authenticated Canton user rights"]
    C["Cached local-party catalog"]
    S["Desired party selection"]
    L["Private Canton Ledger API"]
    A["scanner.py<br/>ACS reconciliation at fixed offset"]
    U["updates.py<br/>party-scoped live stream"]
    P["Conservative event parser"]
    D[("SQLite<br/>holdings + events + transfers + offset")]
    F["FastAPI"]

    R --> C --> S --> A
    L -->|"active contracts at offset N"| A
    A -->|"atomic snapshot + N"| D
    L -->|"beginExclusive N"| U
    U --> P
    P -->|"one SQLite transaction per ledger transaction"| D
    D --> F
```

On an empty catalog, `scanner.py` identifies the authenticated Canton user,
lists that user's rights, and caches the readable party catalog. `CanActAs` and
`CanReadAs` add explicit parties. `CanReadAsAnyParty` enables paged party
directory discovery, restricted to `isLocal` parties. Discovery never probes
Holdings.

The bootstrap order is intentional:

1. The desired selection is loaded from SQLite.
2. `scanner.py` reads the ledger end once.
3. It reads every selected party's Active Contract Set at that exact offset.
4. It commits all current Holdings, active parties, revision, and the same
   offset atomically.
5. `updates.py` streams from `beginExclusive` at the saved offset.

Starting the stream with a zero balance would be incorrect. Re-running
`scanner.py` against an initialized database with no pending selection is a
safe no-op; normal restarts go directly to `updates.py`.

Selection changes are controlled restarts. The API updates only the desired
set and revision. `updates.py` notices that pending revision after a stream
message or checkpoint, closes the socket, and asks the operator to run
`scanner.py`. At the existing saved offset, the scanner reads ACS only for
additions, removes current Holdings for deselections, and atomically activates
the new revision. Historical events and transfers are retained. Any ACS error,
pruned offset, or revision race rolls the reconciliation back.

## Persistence and correctness

`scanner.db` contains:

- `holdings`: current active Holding contracts.
- `scanner_state`: the latest processed private ledger offset.
- `holding_history`: low-level create/archive accounting effects.
- `private_events`: durable raw private events useful after participant pruning.
- `transfers`: only confidently reconstructed semantic transfers.
- `party_catalog` and `party_catalog_state`: the cached rights-aware selector.
- `party_selection`: the desired set.
- `tracked_parties`: current/inactive indexing state and transition offsets.
- `scanner_config`: desired and active selection revisions.

For each live ledger transaction, Holding changes, raw events, semantic
transfers, and the offset commit together. A failure rolls all of them back.
Offsets only move forward, so reconnect replay cannot resurrect an older
Holding state.

The live request combines two filters per tracked party:

- the token `Holding` interface with its interface view, used to maintain
  balances;
- a party-scoped wildcard, used to see other authorized private exercises.

A consumed contract is handled whether Canton represents it as an
`ArchivedEvent` or as a consuming `ExercisedEvent` under
`TRANSACTION_SHAPE_LEDGER_EFFECTS`.

## Transfer reconstruction

The scanner does **not** infer a transfer from “Holding archived + Holding
created.” Those effects can also be minting, burning, locking, fees, or another
token operation.

A row enters `transfers` only when a private `TransferFactory_Transfer` exercise
contains an explicit, valid sender, receiver, positive amount, and optional
instrument. Every visible event is still preserved in `private_events`, so the
parser can be extended later without depending on already-pruned participant
history.

## Setup

Python 3.10+ is sufficient. The Ledger HTTP client in `c8lab.py` uses the
standard library; the live scanner and API need three small packages.

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

DevNet credentials are expected in the existing ignored `.env`. Load them into
the shell without printing them:

```bash
set -a
source .env
set +a
```

Never commit `.env`, `scanner.db`, tokens, or `C8_CLIENT_SECRET`. Do not use
`c8lab.py check` on DevNet; it reads Holdings for thousands of parties. Catalog
discovery pages the native party directory only when the user has
`CanReadAsAnyParty`, filters it to local parties, and caches the result.
Set `C8_USER` to override the Canton user explicitly. Otherwise DevNet uses the
bearer token's `sub`; LocalNet keeps the toolkit's `ledger-api-user` default.

## Run the demo

For the default demo, the first run discovers parties and selects the original
three only if all three remain readable:

```bash
source .venv/bin/activate
set -a; source .env; set +a
python scanner.py
python updates.py
```

For an explicit selection, populate the catalog without bootstrapping, start
the API, inspect the cache, and save full party IDs:

```bash
python scanner.py --catalog-only
export SCANNER_ADMIN_TOKEN='choose-a-long-random-demo-token'
uvicorn api:app --reload
curl 'http://127.0.0.1:8000/parties?q=00209&limit=50&offset=0'
curl -X PUT http://127.0.0.1:8000/parties/selection \
  -H "Authorization: Bearer $SCANNER_ADMIN_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"parties":["full::party-id"]}'
python scanner.py
python updates.py
```

Refresh the slow party catalog only when needed:

```bash
python scanner.py --refresh-parties --catalog-only
```

The default selection limit is 50; set `SCANNER_MAX_PARTIES` before starting
the API to change it. Party browsing is public, but selection mutation is
disabled unless `SCANNER_ADMIN_TOKEN` is configured. The browser keeps an
entered admin token in memory for the current tab only.

After the ACS has been bootstrapped, restart directly from the saved offset:

```bash
source .venv/bin/activate
set -a; source .env; set +a
python updates.py
```

Terminal 2:

```bash
source .venv/bin/activate
uvicorn api:app --reload
```

Open `http://127.0.0.1:8000/` for the responsive scanner dashboard shell.
The status rail polls the local health API, pauses in background tabs, keeps the
last good state during transient failures, and supports the richer hosted-worker
heartbeat planned for deployment. Party browsing, balances, and transfer-table
data are connected in later frontend milestones.

Then query a full party ID or an unambiguous prefix:

```bash
curl http://127.0.0.1:8000/health
curl http://127.0.0.1:8000/balance/00209eb9a1e8485ba9a7383aa6115ab2
curl http://127.0.0.1:8000/history/00209eb9a1e8485ba9a7383aa6115ab2
```

Endpoints:

- `GET /health` — readiness and latest indexed offset.
- `GET /parties?q=&limit=50&offset=0` — cached searchable authorized parties,
  selection state, and catalog metadata; it performs no live Canton calls.
- `GET /parties/selection` — complete desired/active sets and selection limits.
- `PUT /parties/selection` — persist a validated desired set of full party IDs;
  requires the configured scanner admin bearer token.
- `GET /balance/{party}` — current Decimal-aggregated balances.
- `GET /history/{party}` — semantic transfers with `sent`, `received`, or
  `self` direction and counterparty, including current indexing status.
- `GET /debug/holding-history/{party}` — low-level Holding effects.
- `GET /docs` — generated interactive API documentation.

## Tests

The tests use temporary SQLite databases and synthetic private Ledger API JSON;
they never need DevNet credentials or modify the real `scanner.db`.

```bash
python -m unittest discover -s tests -v
node --test tests/test_frontend_status.mjs
python -m py_compile database.py scanner.py updates.py api.py c8lab.py
```

Coverage includes authenticated-user resolution, rights parsing, paged
local-only discovery, atomic cache replacement, legacy database migration,
selection validation and revisions, exact-offset reconciliation and rollback,
active-only update filters, Holding create/archive, semantic history, replay,
Decimal balances, crash consistency, and restart request construction.

## Known limitations

- The participant reported private history pruned before approximately offset
  `2909305`. Transfers before the scanner's starting point cannot be recovered
  from this participant.
- The current local database was bootstrapped around offset `2920767`; schema
  migration seeds its existing Holdings parties as both desired and active
  without changing that offset or rereading ACS.
- A prior transfer attempt between tracked parties failed with
  `NO_SYNCHRONIZER_ON_WHICH_ALL_SUBMITTERS_CAN_SUBMIT`. Read visibility does not
  imply those parties can submit together.
- The parser intentionally recognizes only the confirmed transfer-factory
  choice. Other token-standard flows should be added only after their private
  event arguments are captured and understood.
- DevNet can be slow during the hackathon. A connection timeout is not, by
  itself, evidence of a parser or persistence failure.

The highest-value next step is to capture one real qualifying private transfer
event for a submit-capable party pair, add it as a redacted fixture, and compare
the resulting API history with the transaction submission response.
