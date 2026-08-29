"""FastAPI read API for the local private ledger index."""

from fastapi import FastAPI, HTTPException, Query

import database


app = FastAPI(
    title="Canton Private Scanner",
    description="Private off-ledger index for the configured Canton parties.",
    version="1.0.0",
)

database.create_tables()


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
    return {
        "status": "ok" if last_offset is not None else "bootstrap_required",
        "last_offset": last_offset,
    }


@app.get("/balance/{party}")
def balance(party: str):
    full_party = get_party_or_404(party)
    return {
        "party": full_party,
        "balances": database.get_balance_for_party(full_party),
        "last_offset": database.get_saved_offset(),
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
):
    """Return only confidently reconstructed semantic transfers."""

    full_party = get_party_or_404(party)
    transfers = [
        _add_party_perspective(transfer, full_party)
        for transfer in database.get_transfers_for_party(full_party, limit)
    ]
    return {
        "party": full_party,
        "transfers": transfers,
        "count": len(transfers),
        "last_offset": database.get_saved_offset(),
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
