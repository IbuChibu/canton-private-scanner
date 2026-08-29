# Canton Scope local demo runbook

This runbook is the shortest safe path for presenting the scanner locally.
It never resets the database and never prints credentials.

## 1. Prepare once

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

On a fresh checkout, copy `.env.example` to `.env` and replace the placeholder
client secret and admin token. The real `.env` is ignored by git.

Minimum DevNet values:

```dotenv
C8_BASE=https://api.validator.dev.digik.cantor8.tech/api/ledger
C8_WS_URL=wss://api.validator.dev.digik.cantor8.tech/api/ledger/v2/updates
C8_IDP=https://auth.dev.digik.cantor8.tech
C8_CLIENT_ID=hackathon
C8_CLIENT_SECRET=your-issued-secret
SCANNER_DB=scanner.db
```

`SCANNER_ADMIN_TOKEN` is optional. Set it to a long local-only value if the
demo needs browser-based party-selection changes; otherwise the dashboard is
read-only and still shows balances, status, and history.

The launcher reads `.env` itself, so it does not need to be sourced into the
terminal. Validate everything without starting the service:

```bash
source .venv/bin/activate
python demo.py --check-only
```

The check is read-only with respect to Canton. It validates the Python
environment, URLs, credentials, SQLite integrity/write access, and Ledger API
reachability. It never clears Holdings, history, or the saved offset.

## 2. Start the demo

```bash
source .venv/bin/activate
python demo.py
```

Open <http://127.0.0.1:8000/>. Use only this one launcher: do not add Uvicorn
workers and do not run `scanner.py` or `updates.py` in another terminal while
the managed demo is active.

If the participant is temporarily unavailable, the dashboard still starts and
shows `Retrying`; the managed worker reconnects with bounded backoff. Use
`--skip-network-check` when you deliberately want to start before the
participant is reachable.

If the shared DevNet party directory returns 503 but `scanner.db` already has a
saved offset and selection, startup uses a restricted fallback: it re-verifies
the current user's rights, exposes only the persisted desired/active parties,
and starts the live stream. The page then shows `Live stream connected` and
`Verified selection · full refresh pending`. This is expected and safe for a
demo; retry the complete catalog after the presentation.

Use a different port when needed:

```bash
python demo.py --port 8772
```

## 3. Suggested presentation

1. Point out the privacy boundary: the catalog contains only parties readable
   by the authenticated Canton user.
2. Focus an active party and compare its balance cards with `GET /balance`.
3. Show sent, received, and self-transfer semantics in the activity table.
4. Unlock party selection with the local admin token, change the desired set,
   and apply it. The worker reconciles at the saved offset automatically.
5. Stop the launcher with Ctrl-C, record the offset, start `python demo.py`
   again, and show that the same database resumes rather than rereading ACS.

Useful read-only checks:

```bash
curl http://127.0.0.1:8000/health
curl http://127.0.0.1:8000/parties/selection
```

In `/health`, confirm:

- `stream.status` is `connected`;
- `last_offset` is present and advances when checkpoints arrive;
- `active_party_count` and `desired_party_count` are nonzero;
- `restart_required` is `false`.

For a short restart proof, note `last_offset`, press Ctrl-C once, run
`python demo.py` again, and show that the worker prints
`starting private scanner from offset: <saved offset>` before reconnecting.

## 4. Recovery without data loss

- **Missing package:** activate `.venv` and run
  `python -m pip install -r requirements.txt`.
- **Missing DevNet secret:** add `C8_CLIENT_SECRET` to the ignored `.env`; never
  place it in `.env.example` or source code.
- **Port already in use:** stop the earlier demo or start with
  `python demo.py --port 8010`.
- **Another managed demo owns the database:** stop that launcher. Do not delete
  SQLite lock, WAL, or database files while it is running.
- **Catalog refresh needed:** stop the demo, load `.env` with
  `set -a; source .env; set +a`, run
  `python scanner.py --refresh-parties --catalog-only`, then restart it.
- **Full catalog refresh returns 503:** keep the database. An existing index
  will use the rights-verified persisted selection and connect; retry the
  explicit refresh later when DevNet load falls.
- **Permission or pruned-offset reconciliation error:** keep the database and
  offset unchanged. Correct the right/participant problem, then restart. Never
  erase the offset as an automatic recovery step.
- **Selection controls are read-only:** set `SCANNER_ADMIN_TOKEN` in `.env` and
  restart the demo.

Manual debugging remains available with `python scanner.py`,
`python updates.py`, and `python -m uvicorn api:app` only when
`SCANNER_RUN_WORKER` is not enabled.

## 5. Stop and restart

Press Ctrl-C in the launcher terminal and wait for `Application shutdown
complete`. Do not close the terminal mid-write or remove SQLite files. Restart
with the same command; `SCANNER_DB` identifies the persistent database.

If the process was interrupted forcefully, the next launch performs SQLite's
integrity/write-lock checks and resumes from the last atomically committed
offset.

## 6. Local security boundary

- Bind to `127.0.0.1`; this project is not configured for public deployment.
- The browser keeps the admin token in memory only and clears the input after
  unlock. It never uses cookies, local storage, or session storage.
- Canton credentials stay in `.env` and the worker process. They are never
  returned by the API or embedded in frontend assets.
- Keep exactly one API process and one managed worker while SQLite is in use.
