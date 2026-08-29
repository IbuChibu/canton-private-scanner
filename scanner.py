"""Initial ACS bootstrap for the private Canton scanner."""

import c8lab
import database


PARTY_PREFIXES = [
    "00209eb9a1e8485ba9a7383aa6115ab2",
    "0024bd501a4e4ea2b36125d43107085b",
    "002b2054df5f43b49524971477dfab81",
]


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


def main():
    database.create_tables()
    saved_offset = database.get_saved_offset()
    if saved_offset is not None:
        print(
            "scanner.db is already bootstrapped at offset",
            saved_offset,
            "- start updates.py to resume",
        )
        return

    # Capture the ledger end first, then read every party's ACS at that exact
    # position. No local state changes until every remote read has succeeded.
    snapshot_offset = int(c8lab.ledger_end())
    holdings_by_party = {}
    print("snapshot offset:", snapshot_offset)
    for prefix in PARTY_PREFIXES:
        party = c8lab.find_party(prefix)
        holdings = holdings_at_offset(party, snapshot_offset)
        holdings_by_party[party] = holdings
        print("party:", party, "| holdings:", len(holdings))

    database.replace_all_holdings_and_save_offset(
        holdings_by_party,
        snapshot_offset,
    )
    print("ACS snapshot and matching offset saved atomically to scanner.db")


if __name__ == "__main__":
    main()
