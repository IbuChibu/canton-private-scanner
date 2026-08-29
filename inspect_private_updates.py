import json
import websocket

import c8lab
import database


PARTY_PREFIXES = [
    "00209eb9a1e8485ba9a7383aa6115ab2",
    "0024bd501a4e4ea2b36125d43107085b",
    "002b2054df5f43b49524971477dfab81",
]


WS_URL = (
    "wss://api.validator.dev.digik.cantor8.tech"
    "/api/ledger/v2/updates"
)


# How far back from our current saved offset to inspect.
# This does NOT alter the real scanner offset.
LOOKBACK = 10_000


def main():

    database.create_tables()

    saved_offset = database.get_saved_offset()

    if saved_offset is None:
        raise RuntimeError(
            "No saved scanner offset found."
        )

    start_offset = max(
        0,
        saved_offset - LOOKBACK
    )

    print("saved scanner offset:", saved_offset)
    print("inspection starts at:", start_offset)

    # --------------------------------------------------
    # RESOLVE THE SAME 3 PARTIES WE ALREADY INDEX
    # --------------------------------------------------

    parties = []

    for prefix in PARTY_PREFIXES:

        party = database.resolve_party(
            prefix
        )

        if party is None:
            raise RuntimeError(
                f"Could not resolve party: {prefix}"
            )

        parties.append(
            party
        )

        print(
            "tracking:",
            party
        )

    # --------------------------------------------------
    # WILDCARD FILTER
    #
    # Same parties as before, but now ask for every
    # contract/event type visible to them.
    # --------------------------------------------------

    filters_by_party = {}

    for party in parties:

        filters_by_party[party] = {
            "cumulative": [
                {
                    "identifierFilter": {
                        "WildcardFilter": {
                            "value": {
                                "includeCreatedEventBlob": False
                            }
                        }
                    }
                }
            ]
        }

    # --------------------------------------------------
    # REQUEST
    # --------------------------------------------------

    request_body = {
        "beginExclusive": start_offset,

        "updateFormat": {
            "includeTransactions": {

                # We want ledger effects rather than only
                # ACS insert/delete changes so we can inspect
                # exercise events as well.
                "transactionShape":
                    "TRANSACTION_SHAPE_LEDGER_EFFECTS",

                "eventFormat": {
                    "filtersByParty": filters_by_party,
                    "verbose": True
                }
            }
        }
    }

    # --------------------------------------------------
    # CONNECT
    # --------------------------------------------------

    token = c8lab.token()

    print()
    print("connecting to private Ledger API...")

    ws = websocket.create_connection(
        WS_URL,
        header=[
            f"Authorization: Bearer {token}"
        ],
        suppress_origin=True,
        timeout=20
    )

    ws.settimeout(None)

    ws.send(
        json.dumps(request_body)
    )

    print("connected")
    print("waiting for first private transaction...")
    print()

    try:

        while True:

            raw = ws.recv()

            if not raw:
                continue

            message = json.loads(raw)

            # --------------------------------------------------
            # CANTON WRAPS REAL UPDATES INSIDE:
            #
            # {
            #     "update": {
            #         ...
            #     }
            # }
            #
            # Normalise that here.
            # --------------------------------------------------

            update = message.get(
                "update",
                message
            )

            # --------------------------------------------------
            # IGNORE ORDINARY OFFSET CHECKPOINTS
            # --------------------------------------------------

            if "OffsetCheckpoint" in update:

                checkpoint = update[
                    "OffsetCheckpoint"
                ]

                value = checkpoint.get(
                    "value",
                    checkpoint
                )

                print(
                    "checkpoint:",
                    value.get("offset")
                )

                continue

            # --------------------------------------------------
            # API ERRORS
            # --------------------------------------------------

            if (
                "code" in message
                or "cause" in message
            ):

                print("API RESPONSE:")

                print(
                    json.dumps(
                        message,
                        indent=2
                    )
                )

                break

            # Sometimes an error could theoretically appear
            # inside the wrapped update too.
            if (
                isinstance(update, dict)
                and (
                    "code" in update
                    or "cause" in update
                )
            ):

                print("API RESPONSE:")

                print(
                    json.dumps(
                        update,
                        indent=2
                    )
                )

                break

            # --------------------------------------------------
            # THIS IS WHAT WE WANT TO INSPECT
            # --------------------------------------------------

            print("=" * 80)
            print("PRIVATE UPDATE RECEIVED")
            print("=" * 80)

            print(
                json.dumps(
                    update,
                    indent=2
                )
            )

            print()
            print(
                "Stopped after first non-checkpoint update."
            )

            break

    finally:

        ws.close()


if __name__ == "__main__":
    main()
