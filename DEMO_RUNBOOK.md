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

The launcher reads `.env` itself, so it does not need to be sourced into the
terminal. Validate everything without starting the service:

```bash
source .venv/bin/activate
python demo.py --check-only
```

The check is read-only with respect to Canton. It validates the Python
environment, URLs, credentials, local port, SQLite integrity/write access, and
Ledger API reachability. It never clears Holdings, history, or the saved offset.

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

## 4. Recovery without data loss

- **Missing package:** activate `.venv` and run
  `python -m pip install -r requirements.txt`.
- **Missing DevNet secret:** add `C8_CLIENT_SECRET` to the ignored `.env`; never
  place it in `.env.example` or source code.
- **Port already in use:** stop the earlier demo or start with
  `python demo.py --port 8010`.
- **Another managed demo owns the database:** stop that launcher. Do not delete
  SQLite lock, WAL, or database files while it is running.
- **Catalog refresh needed:** stop the demo, run
  `python scanner.py --refresh-parties --catalog-only`, then restart it.
- **Permission or pruned-offset reconciliation error:** keep the database and
  offset unchanged. Correct the right/participant problem, then restart. Never
  erase the offset as an automatic recovery step.
- **Selection controls are read-only:** set `SCANNER_ADMIN_TOKEN` in `.env` and
  restart the demo.

Manual debugging remains available with `python scanner.py`,
`python updates.py`, and `python -m uvicorn api:app` only when
`SCANNER_RUN_WORKER` is not enabled.

## 5. Local security boundary

- Bind to `127.0.0.1`; this project is not configured for public deployment.
- The browser keeps the admin token in memory only and clears the input after
  unlock. It never uses cookies, local storage, or session storage.
- Canton credentials stay in `.env` and the worker process. They are never
  returned by the API or embedded in frontend assets.
- Keep exactly one API process and one managed worker while SQLite is in use.
