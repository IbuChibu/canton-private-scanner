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


WS_URL = (
    "wss://api.validator.dev.digik.cantor8.tech"
    "/api/ledger/v2/updates"
)


# =========================================================
# EVENT UNWRAPPING
# =========================================================

def unwrap_event(event_wrapper):
    """
    Canton JSON events are wrapped by their event type.

    Example:

        {
            "CreatedEvent": {
                "value": {...}
            }
        }
    """

    event_types = [
        "CreatedEvent",
        "CreatedTreeEvent",
        "ArchivedEvent",
        "ExercisedEvent"
    ]

    for event_type in event_types:

        wrapper = event_wrapper.get(
            event_type
        )

        if wrapper is None:
            continue

        if isinstance(wrapper, dict):

            event = wrapper.get(
                "value",
                wrapper
            )

        else:
            event = wrapper

        return event_type, event

    return None, None


# =========================================================
# HOLDING EXTRACTION
# =========================================================

def holding_from_created_event(event):
    """
    Extract the Holding interface view from a CreatedEvent.

    We deliberately continue using the Holding interface
    rather than guessing from template names.
    """

    for interface_view in event.get(
        "interfaceViews",
        []
    ):

        interface_id = interface_view.get(
            "interfaceId"
        )

        # Ignore interface views that clearly are not
        # the token Holding interface.
        if (
            interface_id is not None
            and "Holding" not in str(interface_id)
        ):
            continue

        value = interface_view.get(
            "viewValue",
            {}
        )

        if not isinstance(value, dict):
            continue

        owner = value.get(
            "owner"
        )

        amount = value.get(
            "amount"
        )

        instrument_id = value.get(
            "instrumentId",
            {}
        )

        if (
            owner is None
            or amount is None
            or not isinstance(
                instrument_id,
                dict
            )
        ):
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
                value.get("lock") is not None
        }

    return None


# =========================================================
# TRANSFER EXTRACTION
# =========================================================

def find_transfer_payload(value):
    """
    Search recursively for a structure containing:

        sender
        receiver
        amount

    We intentionally do NOT infer transfers merely from
    Holding destruction/creation. Minting, fees and burns
    can also change Holdings.
    """

    if isinstance(value, dict):

        if (
            "sender" in value
            and "receiver" in value
            and "amount" in value
        ):
            return value

        for child in value.values():

            result = find_transfer_payload(
                child
            )

            if result is not None:
                return result

    elif isinstance(value, list):

        for child in value:

            result = find_transfer_payload(
                child
            )

            if result is not None:
                return result

    return None


def transfer_from_exercised_event(
    event,
    event_id
):
    """
    Extract a transfer only when the exercise clearly
    represents a transfer and its arguments explicitly
    contain sender, receiver and amount.
    """

    choice = event.get(
        "choice",
        ""
    )

    if "transfer" not in choice.lower():
        return None

    choice_argument = event.get(
        "choiceArgument",
        {}
    )

    transfer = find_transfer_payload(
        choice_argument
    )

    if transfer is None:
        return None

    sender = transfer.get(
        "sender"
    )

    receiver = transfer.get(
        "receiver"
    )

    amount = transfer.get(
        "amount"
    )

    if (
        sender is None
        or receiver is None
        or amount is None
    ):
        return None

    instrument = None

    instrument_id = transfer.get(
        "instrumentId"
    )

    if isinstance(
        instrument_id,
        dict
    ):
        instrument = instrument_id.get(
            "id"
        )

    return {
        "event_id": event_id,
        "sender": sender,
        "receiver": receiver,
        "amount": str(amount),
        "instrument": instrument,
        "choice": choice
    }


# =========================================================
# TRANSACTION EVENT LIST
# =========================================================

def get_transaction_events(transaction):
    """
    Support both:

        events: [...]

    and a tree-style:

        rootEventIds
        eventsById

    so the scanner is not tied to one representation.
    """

    events = transaction.get(
        "events"
    )

    if isinstance(events, list):
        return events

    events_by_id = (
        transaction.get("eventsById")
        or transaction.get("events_by_id")
    )

    if not isinstance(
        events_by_id,
        dict
    ):
        return []

    roots = (
        transaction.get("rootEventIds")
        or transaction.get("root_event_ids")
        or []
    )

    ordered_events = []
    visited = set()

    def walk(event_id):

        if event_id in visited:
            return

        visited.add(
            event_id
        )

        event_wrapper = events_by_id.get(
            event_id
        )

        if event_wrapper is None:
            return

        ordered_events.append(
            event_wrapper
        )

        event_type, event = unwrap_event(
            event_wrapper
        )

        # Public-style tree data may not use wrappers.
        if event is None:
            event = event_wrapper

        child_ids = (
            event.get("childEventIds")
            or event.get("child_event_ids")
            or []
        )

        for child_id in child_ids:
            walk(child_id)

    for root_id in roots:
        walk(root_id)

    # If roots were absent, still preserve all visible events.
    if not ordered_events:

        ordered_events.extend(
            events_by_id.values()
        )

    return ordered_events


# =========================================================
# PROCESS ONE PRIVATE TRANSACTION
# =========================================================

def process_transaction(transaction):

    offset = transaction.get(
        "offset"
    )

    update_id = (
        transaction.get("updateId")
        or transaction.get("update_id")
    )

    record_time = (
        transaction.get("recordTime")
        or transaction.get("record_time")
    )

    if offset is None:
        print(
            "transaction missing offset - ignoring"
        )
        return

    if update_id is None:

        # This should be unusual, but offset still gives us
        # a deterministic identifier rather than inventing
        # random data.
        update_id = f"offset-{offset}"

    print()
    print("=" * 70)
    print("PRIVATE TRANSACTION")
    print("offset:", offset)
    print("update:", update_id)
    print("time:", record_time)

    created_holdings = []
    archived_contract_ids = []

    private_events = []
    transfers = []

    events = get_transaction_events(
        transaction
    )

    print(
        "events:",
        len(events)
    )

    for index, event_wrapper in enumerate(
        events
    ):

        event_type, event = unwrap_event(
            event_wrapper
        )

        # Some tree representations may already contain
        # the unwrapped event.
        if event is None:

            event = event_wrapper

            event_type = (
                event.get("event_type")
                or "UnknownEvent"
            )

        if not isinstance(
            event,
            dict
        ):
            continue

        event_id = (
            event.get("eventId")
            or event.get("event_id")
            or event.get("contractId")
            or event.get("contract_id")
            or f"{update_id}:{index}"
        )

        template_id = (
            event.get("templateId")
            or event.get("template_id")
        )

        choice = event.get(
            "choice"
        )

        private_events.append({
            "event_id": str(event_id),
            "event_type": event_type,
            "template_id":
                str(template_id)
                if template_id is not None
                else None,
            "choice": choice,
            "raw": event_wrapper
        })

        # -----------------------------------------------
        # CREATED HOLDING
        # -----------------------------------------------

        if event_type in (
            "CreatedEvent",
            "CreatedTreeEvent"
        ):

            holding = holding_from_created_event(
                event
            )

            if holding is not None:

                created_holdings.append(
                    holding
                )

                print(
                    "holding created:",
                    holding["party"].split("::")[0],
                    holding["amount"],
                    holding["instrument"]
                )

        # -----------------------------------------------
        # ARCHIVED CONTRACT
        #
        # We pass all archived IDs to the database.
        # It only removes one if that contract is actually
        # present in our current Holding table.
        # -----------------------------------------------

        elif event_type == "ArchivedEvent":

            contract_id = event.get(
                "contractId"
            )

            if contract_id is not None:

                archived_contract_ids.append(
                    contract_id
                )

        # -----------------------------------------------
        # SEMANTIC TRANSFER
        # -----------------------------------------------

        elif event_type == "ExercisedEvent":

            transfer = transfer_from_exercised_event(
                event,
                str(event_id)
            )

            if transfer is not None:

                transfers.append(
                    transfer
                )

                print(
                    "TRANSFER:",
                    transfer["sender"].split("::")[0],
                    "->",
                    transfer["receiver"].split("::")[0],
                    transfer["amount"],
                    transfer["instrument"],
                    "|",
                    transfer["choice"]
                )

    # --------------------------------------------------
    # ONE ATOMIC SQLITE TRANSACTION
    #
    # Holdings + raw history + transfers + offset
    # all commit together.
    # --------------------------------------------------

    database.apply_holding_changes(
        created_holdings=
            created_holdings,

        archived_contract_ids=
            archived_contract_ids,

        offset=
            int(offset),

        update_id=
            update_id,

        record_time=
            record_time,

        private_events=
            private_events,

        transfers=
            transfers
    )

    print(
        "saved:",
        len(created_holdings),
        "created holdings,",
        len(archived_contract_ids),
        "archives,",
        len(private_events),
        "events,",
        len(transfers),
        "transfers"
    )

    database.print_balances()


# =========================================================
# MESSAGE PROCESSING
# =========================================================

def process_message(message):

    # Canton wraps stream updates inside {"update": ...}.
    update = message.get(
        "update",
        message
    )

    # --------------------------------------------------
    # CHECKPOINT
    # --------------------------------------------------

    if "OffsetCheckpoint" in update:

        checkpoint = update[
            "OffsetCheckpoint"
        ]

        value = checkpoint.get(
            "value",
            checkpoint
        )

        offset = value.get(
            "offset"
        )

        if offset is not None:

            database.save_offset(
                int(offset)
            )

            print(
                "checkpoint:",
                offset
            )

        return

    # --------------------------------------------------
    # TRANSACTION
    # --------------------------------------------------

    for transaction_key in (
        "Transaction",
        "TransactionTree"
    ):

        if transaction_key not in update:
            continue

        wrapper = update[
            transaction_key
        ]

        transaction = (
            wrapper.get(
                "value",
                wrapper
            )
            if isinstance(wrapper, dict)
            else wrapper
        )

        process_transaction(
            transaction
        )

        return

    # --------------------------------------------------
    # ERROR
    # --------------------------------------------------

    if (
        "code" in message
        or "cause" in message
    ):

        raise RuntimeError(
            json.dumps(
                message,
                indent=2
            )
        )

    print(
        "unrecognised stream message:",
        json.dumps(message)[:500]
    )


# =========================================================
# BUILD PRIVATE PARTY FILTERS
# =========================================================

def build_filters():

    filters_by_party = {}

    for prefix in PARTY_PREFIXES:

        party = database.resolve_party(
            prefix
        )

        if party is None:

            raise RuntimeError(
                f"Could not resolve tracked party: {prefix}"
            )

        print(
            "tracking:",
            party
        )

        filters_by_party[party] = {
            "cumulative": [

                # ---------------------------------------
                # HOLDING INTERFACE
                #
                # Required so created Holding events have
                # the interface view used for balances.
                # ---------------------------------------

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
                },

                # ---------------------------------------
                # WILDCARD
                #
                # Gives us all other private events this
                # party is entitled to see.
                # ---------------------------------------

                {
                    "identifierFilter": {
                        "WildcardFilter": {
                            "value": {
                                "includeCreatedEventBlob":
                                    False
                            }
                        }
                    }
                }
            ]
        }

    return filters_by_party


# =========================================================
# STREAM
# =========================================================

def run_stream():

    saved_offset = database.get_saved_offset()

    if saved_offset is None:

        raise RuntimeError(
            "No saved scanner offset. "
            "Run scanner.py first."
        )

    filters_by_party = build_filters()

    request_body = {
        "beginExclusive":
            saved_offset,

        "updateFormat": {
            "includeTransactions": {

                "transactionShape":
                    "TRANSACTION_SHAPE_LEDGER_EFFECTS",

                "eventFormat": {
                    "filtersByParty":
                        filters_by_party,

                    "verbose":
                        True
                }
            }
        }
    }

    token = c8lab.token()

    print()
    print(
        "starting private scanner from offset:",
        saved_offset
    )

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
        json.dumps(
            request_body
        )
    )

    print(
        "connected to Canton private update stream"
    )

    try:

        while True:

            raw = ws.recv()

            if not raw:
                continue

            message = json.loads(
                raw
            )

            process_message(
                message
            )

    finally:

        ws.close()


# =========================================================
# MAIN WITH RECONNECT
# =========================================================

def main():

    database.create_tables()

    while True:

        try:

            run_stream()

        except KeyboardInterrupt:

            print()
            print("scanner stopped")
            break

        except Exception as error:

            print()
            print(
                "stream disconnected:"
            )

            print(
                str(error)[:1000]
            )

            # c8lab caches the bearer token.
            # Clear it so reconnect obtains a fresh token,
            # which fixes stale stream authorization.
            if hasattr(
                c8lab,
                "_tok"
            ):
                c8lab._tok.clear()

            print(
                "retrying..."
            )

            time.sleep(2)


if __name__ == "__main__":
    main()

