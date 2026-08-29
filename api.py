"""FastAPI API for the local private ledger index and party selector."""

import os
import secrets
from pathlib import Path
from typing import Annotated

from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import database


app = FastAPI(
    title="Canton Private Scanner",
    description="Private off-ledger index for the configured Canton parties.",
    version="1.0.0",
)

database.create_tables()

MAX_SELECTED_PARTIES = int(os.environ.get("SCANNER_MAX_PARTIES", "50"))
ADMIN_TOKEN = os.environ.get("SCANNER_ADMIN_TOKEN")
FRONTEND_DIR = Path(__file__).resolve().parent / "frontend"

app.mount(
    "/assets",
    StaticFiles(directory=str(FRONTEND_DIR)),
    name="frontend-assets",
)


class PartySelectionRequest(BaseModel):
    parties: list[str]


@app.get("/", include_in_schema=False)
def frontend():
    """Serve the scanner dashboard from the same origin as the API."""

    return FileResponse(FRONTEND_DIR / "index.html")


def get_party_or_404(party):
    """Resolve a full party ID or an unambiguous indexed prefix."""

    try:
        full_party = database.resolve_party(party)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    if full_party is None:
        raise HTTPException(
            status_code=404,
            detail="Party not found in scanner database.",
        )
    return full_party


@app.get("/health")
def health():
    last_offset = database.get_saved_offset()
    catalog = database.get_party_catalog_state()
    selection = database.get_selection_status()
    return {
        "status": "ok" if last_offset is not None else "bootstrap_required",
        "last_offset": last_offset,
        "catalog": catalog,
        "active_party_count": selection["active_count"],
        "desired_party_count": selection["desired_count"],
        "restart_required": selection["restart_required"],
    }


@app.get("/parties")
def parties(
    q: str = "",
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
):
    """Search only the cached authorized-party catalog."""

    response = database.list_parties(q, limit, offset)
    catalog = database.get_party_catalog_state()
    selection = database.get_selection_status()
    response.update(
        {
            "catalog": catalog,
            "active_count": selection["active_count"],
            "desired_count": selection["desired_count"],
            "active_revision": selection["active_revision"],
            "desired_revision": selection["desired_revision"],
            "restart_required": selection["restart_required"],
        }
    )
    return response


@app.get("/parties/selection")
def party_selection():
    """Return the complete selection state needed by the browser editor."""

    return {
        **database.get_selection_status(),
        "max_parties": MAX_SELECTED_PARTIES,
        "selection_management_enabled": bool(ADMIN_TOKEN),
    }


def require_selection_admin(authorization):
    """Validate the hosted selection token without exposing its value."""

    if not ADMIN_TOKEN:
        raise HTTPException(
            status_code=503,
            detail="Party selection management is disabled.",
        )
    if not isinstance(authorization, str):
        authorization = ""
    scheme, separator, credential = (authorization or "").partition(" ")
    if (
        not separator
        or scheme.lower() != "bearer"
        or not credential
        or not secrets.compare_digest(credential, ADMIN_TOKEN)
    ):
        raise HTTPException(
            status_code=403,
            detail="A valid scanner admin token is required.",
        )


@app.put("/parties/selection")
def update_party_selection(
    request: PartySelectionRequest,
    authorization: str | None = Header(default=None),
):
    """Persist a desired selection; scanner.py activates it later."""

    require_selection_admin(authorization)
    try:
        return database.set_desired_parties(
            request.parties,
            max_parties=MAX_SELECTED_PARTIES,
        )
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@app.get("/balance/{party}")
def balance(party: str):
    full_party = get_party_or_404(party)
    tracking = database.get_party_tracking(full_party)
    if tracking is None or not tracking["active"]:
        raise HTTPException(
            status_code=409,
            detail="Party is not actively indexed. Run scanner.py after selecting it.",
        )
    return {
        "party": full_party,
        "balances": database.get_balance_for_party(full_party),
        "last_offset": database.get_saved_offset(),
        "active": True,
    }


def _add_party_perspective(transfer, party):
    if transfer["sender"] == transfer["receiver"] == party:
        transfer["direction"] = "self"
        transfer["counterparty"] = party
    elif transfer["sender"] == party:
        transfer["direction"] = "sent"
        transfer["counterparty"] = transfer["receiver"]
    else:
        transfer["direction"] = "received"
        transfer["counterparty"] = transfer["sender"]
    return transfer


@app.get("/history/{party}")
def history(
    party: str,
    limit: int = Query(default=100, ge=1, le=1000),
    offset: Annotated[int, Query(ge=0)] = 0,
):
    """Return only confidently reconstructed semantic transfers."""

    full_party = get_party_or_404(party)
    transfers = [
        _add_party_perspective(transfer, full_party)
        for transfer in database.get_transfers_for_party(full_party, limit, offset)
    ]
    return {
        "party": full_party,
        "transfers": transfers,
        "count": len(transfers),
        "limit": limit,
        "offset": offset,
        "total": database.count_transfers_for_party(full_party),
        "last_offset": database.get_saved_offset(),
        "active": bool(
            (database.get_party_tracking(full_party) or {}).get("active")
        ),
    }


@app.get("/debug/holding-history/{party}")
def holding_history(
    party: str,
    limit: int = Query(default=100, ge=1, le=1000),
):
    """Return low-level Holding effects for debugging and reconciliation."""

    full_party = get_party_or_404(party)
    events = database.get_history_for_party(full_party, limit)
    return {
        "party": full_party,
        "events": events,
        "count": len(events),
        "last_offset": database.get_saved_offset(),
    }
