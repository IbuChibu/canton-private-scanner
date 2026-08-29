import json
import time

import websocket

import c8lab
import database


PARTY_PREFIXES = [
    "00209eb9a1e8485ba9a7383aa6115ab2",
    "0024bd501a4e4ea2b36125d43107085b",
    "002b2054df5f43b49524971477dfab81",
]


def get_event(event_wrapper):
    """
    Canton wraps events as CreatedEvent / ArchivedEvent.

    Handle both:

        {"CreatedEvent": {...}}

    and:

        {"CreatedEvent": {"value": {...}}}
    """

    if "CreatedEvent" in event_wrapper:

        event = event_wrapper["CreatedEvent"]

        if isinstance(event, dict):
            event = event.get("value", event)

        return "created", event


    if "ArchivedEvent" in event_wrapper:

        event = event_wrapper["ArchivedEvent"]

        if isinstance(event, dict):
            event = event.get("value", event)

        return "archived", event


    return None, None


def holding_from_created_event(event):
    """
    If this CreatedEvent contains a Holding interface view,
    convert it into the format our SQLite database expects.
    """

    for view in event.get("interfaceViews", []):

        value = view.get(
            "viewValue",
            {}
        )

        owner = value.get("owner")
        amount = value.get("amount")

        instrument_id = value.get(
            "instrumentId",
            {}
        )

        if owner is None or amount is None:
            continue

        return {
            "contractId":
                event.get("contractId"),

            "party":
                owner,

            "amount":
                amount,

            "instrument":
                instrument_id.get("id"),

            "admin":
                instrument_id.get("admin"),

            "locked":
                value.get("lock") is not None,
        }

    return None


def process_transaction(transaction):
    """
    Convert one Canton ACS-delta transaction into:

        Holdings to create
        Holdings to archive

    Then update:

        holdings
        holding_history
        scanner_state

    in one SQLite transaction.
    """

    offset = transaction.get("offset")

    update_id = transaction.get("updateId")

    record_time = transaction.get("recordTime")

    events = transaction.get(
        "events",
        []
    )

    print()
    print("=" * 70)
    print("TRANSACTION")
    print("=" * 70)

    print(
        "update id:",
        update_id
    )

    print(
        "offset:",
        offset
    )

    print(
        "record time:",
        record_time
    )

    print(
        "events:",
        len(events)
    )


    if offset is None:
        print("No transaction offset - skipping.")
        return


    if update_id is None:
        print("No update ID - skipping.")
        return


    created_holdings = []

    archived_contract_ids = []


    for event_wrapper in events:

        event_type, event = get_event(
            event_wrapper
        )

        if event is None:
            continue


        # =================================================
        # CREATED HOLDING
        # =================================================

        if event_type == "created":

            holding = holding_from_created_event(
                event
            )

            if holding is None:
                continue


            created_holdings.append(
                holding
            )


            print(
                "CREATE",
                holding["party"].split("::")[0],
                holding["amount"],
                holding["instrument"]
            )


        # =================================================
        # ARCHIVED HOLDING
        # =================================================

        elif event_type == "archived":

            contract_id = event.get(
                "contractId"
            )

            if contract_id is None:
                continue


            archived_contract_ids.append(
                contract_id
            )


            print(
                "ARCHIVE",
                contract_id[:20] + "..."
            )


    # =====================================================
    # UPDATE SQLITE
    # =====================================================

    # This one database function:
    #
    # 1. records archived Holdings in history
    # 2. deletes archived Holdings
    # 3. inserts created Holdings
    # 4. records created Holdings in history
    # 5. saves the transaction offset
    #
    # All in ONE SQLite transaction.

    database.apply_holding_changes(
        created_holdings,
        archived_contract_ids,
        offset,
        update_id,
        record_time
    )


    print(
        "SQLite updated to offset:",
        offset
    )

    print(
        "created holdings:",
        len(created_holdings)
    )

    print(
        "archived holdings:",
        len(archived_contract_ids)
    )


    database.print_balances()


def make_filters(parties):
    """
    Ask Canton only for Holding interface events
    visible to our tracked parties.
    """

    filters = {}


    for party in parties:

        filters[party] = {
            "cumulative": [
                {
                    "identifierFilter": {
                        "InterfaceFilter": {
                            "value": {
                                "interfaceId":
                                    c8lab.HOLDING,

                                "includeInterfaceView":
                                    True,

                                "includeCreatedEventBlob":
                                    False
                            }
                        }
                    }
                }
            ]
        }


    return filters


# =========================================================
# STARTUP
# =========================================================

database.create_tables()


# Resolve our three short party prefixes into their full
# Canton party identifiers.

parties = [
    c8lab.find_party(prefix)
    for prefix in PARTY_PREFIXES
]


saved_offset = database.get_saved_offset()


if saved_offset is None:

    raise RuntimeError(
        "No ACS snapshot found. Run scanner.py first."
    )


print(
    "starting from offset:",
    saved_offset
)

print(
    "watching parties:",
    len(parties)
)


filters_by_party = make_filters(
    parties
)


# =========================================================
# WEBSOCKET REQUEST
# =========================================================

request_body = {

    "beginExclusive":
        saved_offset,

    "updateFormat": {

        "includeTransactions": {

            # ACS_DELTA gives us:
            #
            #     CreatedEvent
            #     ArchivedEvent
            #
            # which maps directly onto the current
            # holdings table.

            "transactionShape":
                "TRANSACTION_SHAPE_ACS_DELTA",

            "eventFormat": {

                "filtersByParty":
                    filters_by_party,

                "verbose":
                    True
            }
        }
    }
}


WS_URL = (
    "wss://api.validator.dev.digik.cantor8.tech"
    "/api/ledger/v2/updates"
)


# =========================================================
# AUTHENTICATION
# =========================================================

token = c8lab.token()


# =========================================================
# CONNECT
# =========================================================

while True:

    try:

        ws = websocket.create_connection(
            WS_URL,

            header=[
                f"Authorization: Bearer {token}"
            ],

            suppress_origin=True,

            timeout=20
        )


        # The 20-second timeout is only useful while
        # establishing the connection.
        #
        # Once connected, the ledger may legitimately be
        # quiet for longer than 20 seconds.

        ws.settimeout(None)


        break


    except (
        TimeoutError,
        websocket.WebSocketTimeoutException
    ):

        print(
            "WebSocket connection timed out. Retrying..."
        )

        time.sleep(2)


# =========================================================
# SUBSCRIBE
# =========================================================

ws.send(
    json.dumps(
        request_body
    )
)


print("connected")
print("listening for Holding changes...")
print()


# =========================================================
# STREAM
# =========================================================

try:

    while True:

        message = ws.recv()


        if not message:
            continue


        data = json.loads(
            message
        )


        # =================================================
        # API ERROR
        # =================================================

        if "code" in data:

            print()
            print("API ERROR")

            print(
                json.dumps(
                    data,
                    indent=2
                )
            )

            break


        update = data.get(
            "update",
            {}
        )


        # =================================================
        # OFFSET CHECKPOINT
        # =================================================

        if "OffsetCheckpoint" in update:

            checkpoint = (
                update["OffsetCheckpoint"]
                .get("value", {})
            )


            checkpoint_offset = (
                checkpoint.get("offset")
            )


            print(
                "checkpoint:",
                checkpoint_offset
            )


            if checkpoint_offset is not None:

                # No matching Holding transaction occurred
                # between our previous position and this
                # checkpoint.
                #
                # Therefore it is safe to advance our
                # resume position.

                database.save_offset(
                    checkpoint_offset
                )


            continue


        # =================================================
        # TRANSACTION
        # =================================================

        if "Transaction" in update:

            transaction = (
                update["Transaction"]
                .get("value", {})
            )


            process_transaction(
                transaction
            )


            continue


        # =================================================
        # UNKNOWN UPDATE
        # =================================================

        print(
            "OTHER UPDATE:",
            json.dumps(
                data,
                indent=2
            )
        )


# =========================================================
# MANUAL STOP
# =========================================================

except KeyboardInterrupt:

    print()
    print("Stopping scanner...")


# =========================================================
# CLEANUP
# =========================================================

finally:

    ws.close()

