"""Authorized-party discovery and exact-offset ACS reconciliation."""

import argparse

import c8lab
import database


DEFAULT_PARTIES = [
    "00209eb9a1e8485ba9a7383aa6115ab2::1220c9426e3190112631c94845c4b2780b8e7a0f2e527edb706c7e093850e339c64a",
    "0024bd501a4e4ea2b36125d43107085b::122085e16f18fc9ab1aee5a881b7e8ac5d889ed917d5003cc04084372b06fb7b9a4a",
    "002b2054df5f43b49524971477dfab81::12202f9064ea8fc956798ae934c5b543dc91b7774cd4a867d66be32f43e88f8a2b99",
]


def parse_user_rights(rights):
    """Return explicit readable rights and whether read-as-any is granted."""

    explicit = {}
    read_as_any = False
    for right in rights:
        if not isinstance(right, dict):
            continue
        kind = right.get("kind", right)
        if not isinstance(kind, dict):
            continue
        if "CanReadAsAnyParty" in kind:
            read_as_any = True
        for right_name, flag in (("CanActAs", "can_act_as"), ("CanReadAs", "can_read_as")):
            if right_name not in kind:
                continue
            value = kind[right_name]
            if isinstance(value, dict):
                value = value.get("value", value)
            party = value.get("party") if isinstance(value, dict) else None
            if not isinstance(party, str) or not party:
                continue
            entry = explicit.setdefault(
                party,
                {"can_act_as": False, "can_read_as": False},
            )
            entry[flag] = True
    return explicit, read_as_any


def discover_authorized_parties():
    """Build a complete readable catalog without probing Holdings ACS."""

    user_id = c8lab.authenticated_user_id()
    explicit, read_as_any = parse_user_rights(c8lab.user_rights(user_id))
    discovered = {}

    for party, flags in explicit.items():
        discovered[party] = {
            "party": party,
            "display_name": party.split("::", 1)[0],
            "is_local": None,
            "can_act_as": flags["can_act_as"],
            "can_read_as": flags["can_read_as"],
            "readable": True,
            "source": "explicit_right",
        }

    if read_as_any:
        page_token = None
        seen_page_tokens = set()
        while True:
            page = c8lab.party_page(
                page_size=10_000,
                page_token=page_token,
                sub=user_id,
            )
            for detail in page.get("partyDetails", []):
                party = detail.get("party")
                if not party:
                    continue
                if not detail.get("isLocal"):
                    if party in discovered:
                        discovered[party]["is_local"] = False
                    continue
                entry = discovered.setdefault(
                    party,
                    {
                        "party": party,
                        "display_name": party.split("::", 1)[0],
                        "is_local": True,
                        "can_act_as": False,
                        "can_read_as": False,
                        "readable": True,
                        "source": "read_any_local",
                    },
                )
                entry["is_local"] = True
                if entry["source"] == "explicit_right":
                    entry["source"] = "explicit_right+read_any_local"
            page_token = page.get("nextPageToken")
            if not page_token:
                break
            if page_token in seen_page_tokens:
                raise c8lab.LabError("Party directory repeated a pagination token")
            seen_page_tokens.add(page_token)

    return {
        "user_id": user_id,
        "read_as_any": read_as_any,
        "entries": sorted(discovered.values(), key=lambda item: item["party"]),
    }


def refresh_party_catalog():
    """Discover completely, then atomically replace the SQLite cache."""

    try:
        discovery = discover_authorized_parties()
        database.replace_party_catalog(
            discovery["entries"],
            discovery["user_id"],
            discovery["read_as_any"],
        )
    except Exception as error:
        database.record_party_catalog_error(error)
        raise
    print(
        "party catalog refreshed:",
        len(discovery["entries"]),
        "readable parties for",
        discovery["user_id"],
    )
    return discovery


def holdings_at_offset(party, offset):
    """Read one party's Holding interface ACS at an exact ledger offset."""

    body = {
        "filter": {
            "filtersByParty": {
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
                        }
                    ]
                }
            }
        },
        "verbose": False,
        "activeAtOffset": int(offset),
    }
    response = c8lab.call("/v2/state/active-contracts", body)

    holdings = []
    for item in response:
        event = (
            item.get("contractEntry", {})
            .get("JsActiveContract", {})
            .get("createdEvent", {})
        )
        for view in event.get("interfaceViews", []):
            if "HoldingV1:Holding" not in str(view.get("interfaceId", c8lab.HOLDING)):
                continue
            value = view.get("viewValue", {})
            instrument_id = value.get("instrumentId", {})
            if not event.get("contractId") or value.get("amount") is None:
                continue
            holdings.append(
                {
                    "contractId": event["contractId"],
                    "party": value.get("owner") or party,
                    "amount": str(value["amount"]),
                    "instrument": instrument_id.get("id"),
                    "admin": instrument_id.get("admin"),
                    "locked": value.get("lock") is not None,
                }
            )
    return holdings


def bootstrap_or_reconcile():
    """Bring active indexing to the desired revision without moving offset."""

    status = database.get_selection_status()
    desired = status["desired_parties"]
    if not desired:
        raise c8lab.LabError(
            "No readable default party selection is available. Use "
            "PUT /parties/selection, then run scanner.py again."
        )

    saved_offset = database.get_saved_offset()
    if saved_offset is None:
        # Capture ledger end first, then read every ACS at that same position.
        snapshot_offset = int(c8lab.ledger_end())
        holdings_by_party = {}
        print("snapshot offset:", snapshot_offset)
        for party in desired:
            holdings_by_party[party] = holdings_at_offset(party, snapshot_offset)
            print("party:", party, "| holdings:", len(holdings_by_party[party]))
        database.replace_all_holdings_and_save_offset(
            holdings_by_party,
            snapshot_offset,
            active_parties=desired,
            expected_revision=status["desired_revision"],
        )
        print("ACS snapshot and matching offset saved atomically to scanner.db")
        return

    if not status["restart_required"]:
        print(
            "scanner.db is ready at offset",
            saved_offset,
            "- start updates.py to resume",
        )
        return

    active = set(status["active_parties"])
    desired_set = set(desired)
    additions = sorted(desired_set - active)
    removals = sorted(active - desired_set)

    # All remote ACS reads finish before any local write. A 403, pruned offset,
    # or other Canton error therefore leaves the active selection untouched.
    holdings_by_party = {}
    for party in additions:
        holdings_by_party[party] = holdings_at_offset(party, saved_offset)
        print("addition:", party, "| holdings:", len(holdings_by_party[party]))
    database.reconcile_tracked_parties(
        holdings_by_party,
        removals,
        saved_offset,
        status["desired_revision"],
    )
    print(
        "party selection reconciled at offset",
        saved_offset,
        "| added:",
        len(additions),
        "| removed:",
        len(removals),
    )


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Discover authorized parties and bootstrap/reconcile the scanner"
    )
    parser.add_argument(
        "--refresh-parties",
        action="store_true",
        help="refresh the authorized-party catalog before continuing",
    )
    parser.add_argument(
        "--catalog-only",
        action="store_true",
        help="populate the party selector without reading Holdings ACS",
    )
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    database.create_tables()
    if args.refresh_parties or database.party_catalog_is_empty():
        refresh_party_catalog()

    if not database.get_desired_parties():
        database.seed_default_selection(DEFAULT_PARTIES)

    if args.catalog_only:
        status = database.get_selection_status()
        print(
            "catalog ready | desired parties:",
            status["desired_count"],
            "| bootstrap skipped",
        )
        return
    bootstrap_or_reconcile()


if __name__ == "__main__":
    main()
