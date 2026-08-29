from fastapi import FastAPI, HTTPException, Query

import database


app = FastAPI(
    title="Canton Private Scanner",
    description=(
        "Private off-ledger index of Canton Holdings "
        "for tracked parties."
    ),
    version="1.0.0"
)


database.create_tables()


def get_party_or_404(party):
    """
    Resolve a short party prefix to the complete
    Canton party identifier.
    """

    try:

        full_party = database.resolve_party(
            party
        )

    except ValueError as error:

        raise HTTPException(
            status_code=400,
            detail=str(error)
        )


    if full_party is None:

        raise HTTPException(
            status_code=404,
            detail="Party not found in scanner database."
        )


    return full_party


# =========================================================
# HEALTH
# =========================================================

@app.get("/health")
def health():

    return {
        "status": "ok",
        "last_offset": database.get_saved_offset()
    }


# =========================================================
# BALANCE
# =========================================================

@app.get("/balance/{party}")
def balance(party: str):

    full_party = get_party_or_404(
        party
    )


    balances = database.get_balance_for_party(
        full_party
    )


    return {
        "party": full_party,
        "balances": balances,
        "last_offset": database.get_saved_offset()
    }


# =========================================================
# HISTORY
# =========================================================

@app.get("/history/{party}")
def history(
    party: str,

    limit: int = Query(
        default=100,
        ge=1,
        le=1000
    )
):

    full_party = get_party_or_404(
        party
    )


    events = database.get_history_for_party(
        full_party,
        limit
    )


    return {
        "party": full_party,
        "events": events,
        "count": len(events),
        "last_offset": database.get_saved_offset()
    }