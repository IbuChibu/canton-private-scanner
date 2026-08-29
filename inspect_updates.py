import json
import websocket

import c8lab
import database


def get_saved_offset():
    conn = database.get_connection()

    row = conn.execute(
        "SELECT last_offset FROM scanner_state WHERE id = 1"
    ).fetchone()

    conn.close()

    return row[0]


# Inspection only:
# watch more local parties so we're more likely to find a transaction.
parties = c8lab.local_parties()[:50]

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


saved_offset = get_saved_offset()

# The server told us this is the earliest available offset.
inspection_offset = 2907915


print("scanner offset:", saved_offset)
print("inspection starting offset:", inspection_offset)
print("watching parties:", len(parties))


request_body = {
    "beginExclusive": inspection_offset,

    "updateFormat": {
        "includeTransactions": {

            "transactionShape":
                "TRANSACTION_SHAPE_LEDGER_EFFECTS",

            "eventFormat": {
                "filtersByParty": filters_by_party,
                "verbose": True
            }
        }
    }
}


WS_URL = (
    "wss://api.validator.dev.digik.cantor8.tech"
    "/api/ledger/v2/updates"
)


token = c8lab.token()


ws = websocket.create_connection(
    WS_URL,
    header=[
        f"Authorization: Bearer {token}"
    ],
    suppress_origin=True
)


ws.send(
    json.dumps(request_body)
)


print("connected")
print("looking for first real transaction...")
print()


try:

    while True:

        message = ws.recv()

        if not message:
            continue

        data = json.loads(message)


        # -------------------------
        # API ERROR
        # -------------------------

        if "code" in data:
            print("API ERROR:")
            print(json.dumps(data, indent=2))
            break


        update = data.get("update", {})


        # -------------------------
        # CHECKPOINT
        # -------------------------

        if "OffsetCheckpoint" in update:
            continue


        # -------------------------
        # REAL LEDGER UPDATE
        # -------------------------

        if update:

            print("=" * 80)
            print("REAL UPDATE FOUND")
            print("=" * 80)

            print(
                json.dumps(
                    data,
                    indent=2
                )
            )

            break


except KeyboardInterrupt:

    print()
    print("Stopped.")


finally:

    ws.close()