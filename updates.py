"""Resumable private Canton Ledger API update scanner."""

import json
import time
from decimal import Decimal, InvalidOperation

try:
    import websocket
except ImportError:  # Pure parser/database tests do not need the WS dependency.
    websocket = None

import c8lab
import database

WS_URL = (
    "wss://api.validator.dev.digik.cantor8.tech"
    "/api/ledger/v2/updates"
)

# This is the transfer choice submitted by c8lab.transfer(). Keeping an exact
# allow-list is intentional: a choice merely containing the word "transfer"
# is not enough evidence to create semantic history.
SEMANTIC_TRANSFER_CHOICES = {"TransferFactory_Transfer"}

EVENT_WRAPPERS = (
    "CreatedEvent",
    "CreatedTreeEvent",
    "ArchivedEvent",
    "ExercisedEvent",
)


class SelectionChangePending(RuntimeError):
    """The stream must stop so scanner.py can reconcile party state."""


def unwrap_event(event_wrapper):
    """Return ``(event_type, value)`` for a Canton JSON event wrapper."""

    if not isinstance(event_wrapper, dict):
        return None, None
    for event_type in EVENT_WRAPPERS:
        if event_type not in event_wrapper:
            continue
        wrapper = event_wrapper[event_type]
        if isinstance(wrapper, dict):
            return event_type, wrapper.get("value", wrapper)
        return event_type, wrapper
    return None, None


def holding_from_created_event(event):
    """Extract a token Holding only from its requested interface view."""

    if not isinstance(event, dict) or not event.get("contractId"):
        return None
    interface_views = event.get("interfaceViews") or []
    if isinstance(interface_views, dict):
        interface_views = interface_views.values()

    for interface_view in interface_views:
        if not isinstance(interface_view, dict):
            continue
        interface_id = interface_view.get("interfaceId")
        if interface_id is not None and "HoldingV1:Holding" not in str(interface_id):
            continue
        value = interface_view.get("viewValue")
        if not isinstance(value, dict):
            continue
        instrument_id = value.get("instrumentId")
        owner = value.get("owner")
        amount = value.get("amount")
        if not isinstance(instrument_id, dict) or not owner or amount is None:
            continue
        try:
            decimal_amount = Decimal(str(amount))
        except InvalidOperation:
            continue
        if not decimal_amount.is_finite() or decimal_amount < 0:
            continue
        return {
            "contractId": event["contractId"],
            "party": owner,
            "amount": str(amount),
            "instrument": instrument_id.get("id"),
            "admin": instrument_id.get("admin"),
            "locked": value.get("lock") is not None,
        }
    return None


def find_transfer_payload(value):
    """Find an explicit ``sender/receiver/amount`` record in a choice argument."""

    if isinstance(value, dict):
        if {"sender", "receiver", "amount"}.issubset(value):
            return value
        for child in value.values():
            found = find_transfer_payload(child)
            if found is not None:
                return found
    elif isinstance(value, list):
        for child in value:
            found = find_transfer_payload(child)
            if found is not None:
                return found
    return None


def transfer_from_exercised_event(event, event_id):
    """Conservatively reconstruct a semantic TransferFactory transfer."""

    if not isinstance(event, dict):
        return None
    choice = event.get("choice")
    if choice not in SEMANTIC_TRANSFER_CHOICES:
        return None

    argument = event.get("choiceArgument", event.get("choice_argument", {}))
    transfer = find_transfer_payload(argument)
    if transfer is None:
        return None

    sender = transfer.get("sender")
    receiver = transfer.get("receiver")
    amount = transfer.get("amount")
    if not isinstance(sender, str) or not sender:
        return None
    if not isinstance(receiver, str) or not receiver:
        return None
    try:
        decimal_amount = Decimal(str(amount))
    except (InvalidOperation, TypeError, ValueError):
        return None
    if not decimal_amount.is_finite() or decimal_amount <= 0:
        return None

    instrument = None
    instrument_id = transfer.get("instrumentId")
    if isinstance(instrument_id, dict):
        instrument = instrument_id.get("id")

    return {
        "event_id": str(event_id),
        "sender": sender,
        "receiver": receiver,
        "amount": str(amount),
        "instrument": instrument,
        "choice": choice,
    }


def _child_event_ids(event_wrapper):
    _, event = unwrap_event(event_wrapper)
    if event is None and isinstance(event_wrapper, dict):
        event = event_wrapper
    if not isinstance(event, dict):
        return []
    return event.get("childEventIds") or event.get("child_event_ids") or []


def get_transaction_events(transaction):
    """Return ordered ``(map_event_id, wrapper)`` pairs from list or tree JSON."""

    events = transaction.get("events")
    if isinstance(events, list):
        return [(None, wrapper) for wrapper in events]
    if isinstance(events, dict):
        return list(events.items())

    events_by_id = transaction.get("eventsById") or transaction.get("events_by_id")
    if not isinstance(events_by_id, dict):
        return []

    roots = transaction.get("rootEventIds") or transaction.get("root_event_ids") or []
    ordered = []
    visited = set()

    def walk(event_id):
        if event_id in visited:
            return
        visited.add(event_id)
        wrapper = events_by_id.get(event_id)
        if wrapper is None:
            return
        ordered.append((event_id, wrapper))
        for child_id in _child_event_ids(wrapper):
            walk(child_id)

    for root_id in roots:
        walk(root_id)
    # Preserve disconnected visible events too. Filtering can produce trees in
    # which an ancestor is hidden but a descendant is visible.
    for event_id in events_by_id:
        walk(event_id)
    return ordered


def _event_template_id(event):
    template_id = event.get("templateId") or event.get("template_id")
    if template_id is None:
        return None
    if isinstance(template_id, str):
        return template_id
    return json.dumps(template_id, sort_keys=True, separators=(",", ":"))


def parse_transaction(transaction):
    """Convert one private ledger transaction into a database write batch."""

    offset = transaction.get("offset")
    if offset is None:
        raise ValueError("Private transaction is missing its ledger offset")
    offset = int(offset)
    update_id = transaction.get("updateId") or transaction.get("update_id")
    if not update_id:
        update_id = f"offset-{offset}"
    record_time = transaction.get("recordTime") or transaction.get("record_time")

    batch = {
        "offset": offset,
        "update_id": str(update_id),
        "record_time": record_time,
        "created_holdings": [],
        "archived_contract_ids": [],
        "private_events": [],
        "transfers": [],
    }
    seen_event_ids = set()

    for index, (mapped_event_id, event_wrapper) in enumerate(
        get_transaction_events(transaction)
    ):
        event_type, event = unwrap_event(event_wrapper)
        if event is None:
            event = event_wrapper
            event_type = event.get("event_type", "UnknownEvent") if isinstance(event, dict) else None
        if not event_type or not isinstance(event, dict):
            continue

        event_id = (
            event.get("eventId")
            or event.get("event_id")
            or mapped_event_id
            or event.get("contractId")
            or event.get("contract_id")
            or f"{update_id}:{index}"
        )
        event_id = str(event_id)
        if event_id in seen_event_ids:
            continue
        seen_event_ids.add(event_id)

        batch["private_events"].append(
            {
                "event_id": event_id,
                "event_type": event_type,
                "template_id": _event_template_id(event),
                "choice": event.get("choice"),
                "raw": event_wrapper,
            }
        )

        if event_type in ("CreatedEvent", "CreatedTreeEvent"):
            holding = holding_from_created_event(event)
            if holding is not None:
                batch["created_holdings"].append(holding)
        elif event_type == "ArchivedEvent":
            contract_id = event.get("contractId") or event.get("contract_id")
            if contract_id:
                batch["archived_contract_ids"].append(contract_id)
        elif event_type == "ExercisedEvent":
            # LEDGER_EFFECTS represents consumption as an exercise rather than
            # an ACS-delta ArchivedEvent. The database only removes this ID if
            # it is one of our indexed Holdings.
            if event.get("consuming") is True:
                contract_id = event.get("contractId") or event.get("contract_id")
                if contract_id:
                    batch["archived_contract_ids"].append(contract_id)
            transfer = transfer_from_exercised_event(event, event_id)
            if transfer is not None:
                batch["transfers"].append(transfer)

    batch["archived_contract_ids"] = list(
        dict.fromkeys(batch["archived_contract_ids"])
    )
    return batch


def process_transaction(transaction):
    """Parse and atomically persist one transaction. Return whether it applied."""

    batch = parse_transaction(transaction)
    applied = database.apply_holding_changes(
        created_holdings=batch["created_holdings"],
        archived_contract_ids=batch["archived_contract_ids"],
        offset=batch["offset"],
        update_id=batch["update_id"],
        record_time=batch["record_time"],
        private_events=batch["private_events"],
        transfers=batch["transfers"],
    )
    if not applied:
        print(f"replay skipped at offset: {batch['offset']}")
        return False

    print(
        "indexed offset",
        batch["offset"],
        "| holdings +",
        len(batch["created_holdings"]),
        "-",
        len(batch["archived_contract_ids"]),
        "| private events",
        len(batch["private_events"]),
        "| semantic transfers",
        len(batch["transfers"]),
    )
    database.print_balances()
    return True


def process_message(message):
    """Process one decoded WebSocket message from Canton."""

    if not isinstance(message, dict):
        raise ValueError("Ledger stream message is not a JSON object")
    update = message.get("update", message)
    if "code" in message or "cause" in message:
        raise RuntimeError(json.dumps(message, indent=2))
    if not isinstance(update, dict):
        raise ValueError("Ledger stream update is not a JSON object")
    if "code" in update or "cause" in update:
        raise RuntimeError(json.dumps(update, indent=2))

    if "OffsetCheckpoint" in update:
        wrapper = update["OffsetCheckpoint"]
        value = wrapper.get("value", wrapper) if isinstance(wrapper, dict) else {}
        offset = value.get("offset")
        if offset is not None:
            database.save_offset(int(offset))
            print("checkpoint:", offset)
        return

    for transaction_key in ("Transaction", "TransactionTree"):
        if transaction_key not in update:
            continue
        wrapper = update[transaction_key]
        transaction = wrapper.get("value", wrapper) if isinstance(wrapper, dict) else wrapper
        process_transaction(transaction)
        return

    print("unrecognised stream message:", json.dumps(message)[:500])


def build_filters_for_parties(parties):
    """Build party-scoped Holding-interface plus wildcard filters."""

    return {
        party: {
            "cumulative": [
                {
                    "identifierFilter": {
                        "InterfaceFilter": {
                            "value": {
                                "interfaceId": c8lab.HOLDING,
                                "includeInterfaceView": True,
                                "includeCreatedEventBlob": False,
                            }
                        }
                    }
                },
                {
                    "identifierFilter": {
                        "WildcardFilter": {
                            "value": {"includeCreatedEventBlob": False}
                        }
                    }
                },
            ]
        }
        for party in parties
    }


def build_filters():
    status = database.get_selection_status()
    if status["restart_required"]:
        raise SelectionChangePending(
            "Party selection changed. Run scanner.py before restarting updates.py."
        )
    parties = status["active_parties"]
    if not parties:
        raise RuntimeError("No active tracked parties. Run scanner.py first.")
    for party in parties:
        print("tracking:", party)
    return build_filters_for_parties(parties)


def build_update_request(saved_offset, filters_by_party):
    """Build the exact resumable private `/v2/updates` subscription."""

    return {
        "beginExclusive": int(saved_offset),
        "updateFormat": {
            "includeTransactions": {
                "transactionShape": "TRANSACTION_SHAPE_LEDGER_EFFECTS",
                "eventFormat": {
                    "filtersByParty": filters_by_party,
                    "verbose": True,
                },
            }
        },
    }


def run_stream():
    if websocket is None:
        raise RuntimeError("websocket-client is required; install requirements.txt")
    saved_offset = database.get_saved_offset()
    if saved_offset is None:
        raise RuntimeError("No saved scanner offset. Run scanner.py first.")

    request_body = build_update_request(saved_offset, build_filters())
    token = c8lab.token()
    print("starting private scanner from offset:", saved_offset)
    ws = websocket.create_connection(
        WS_URL,
        header=[f"Authorization: Bearer {token}"],
        suppress_origin=True,
        timeout=20,
    )
    ws.settimeout(None)
    ws.send(json.dumps(request_body))
    print("connected to Canton private update stream")
    try:
        while True:
            raw = ws.recv()
            if raw:
                process_message(json.loads(raw))
                if database.get_selection_status()["restart_required"]:
                    raise SelectionChangePending(
                        "Party selection changed. Run scanner.py before "
                        "restarting updates.py."
                    )
    finally:
        ws.close()


def main():
    database.create_tables()
    while True:
        try:
            run_stream()
        except KeyboardInterrupt:
            print("\nscanner stopped")
            break
        except SelectionChangePending as error:
            print("\nupdates stopped cleanly:")
            print(str(error))
            break
        except Exception as error:
            print("\nstream disconnected:")
            print(str(error)[:1000])
            # c8lab caches bearer tokens; force a refresh before reconnecting.
            if hasattr(c8lab, "_tok"):
                c8lab._tok.clear()
            print("retrying...")
            time.sleep(2)


if __name__ == "__main__":
    main()
