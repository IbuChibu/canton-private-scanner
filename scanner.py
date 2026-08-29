import c8lab
import database

PARTIES = [
    "00209eb9a1e8485ba9a7383aa6115ab2",
    "0024bd501a4e4ea2b36125d43107085b",
    "002b2054df5f43b49524971477dfab81",
]

HOLDING = "#splice-api-token-holding-v1:Splice.Api.Token.HoldingV1:Holding"


def holdings_at_offset(party, offset):
    body = {
        "filter": {
            "filtersByParty": {
                party: {
                    "cumulative": [
                        {
                            "identifierFilter": {
                                "InterfaceFilter": {
                                    "value": {
                                        "interfaceId": HOLDING,
                                        "includeInterfaceView": True,
                                        "includeCreatedEventBlob": False
                                    }
                                }
                            }
                        }
                    ]
                }
            }
        },
        "verbose": False,
        "activeAtOffset": offset
    }

    response = c8lab.call("/v2/state/active-contracts", body)

    holdings = []

    for item in response:
        event = (
            item
            .get("contractEntry", {})
            .get("JsActiveContract", {})
            .get("createdEvent", {})
        )

        for view in event.get("interfaceViews", []):
            value = view.get("viewValue", {})

            holdings.append({
                "contractId": event.get("contractId"),
                "amount": value.get("amount"),
                "instrument": value.get("instrumentId", {}).get("id"),
                "admin": value.get("instrumentId", {}).get("admin"),
                "locked": value.get("lock") is not None,
            })

    return holdings


database.create_tables()

snapshot_offset = c8lab.ledger_end()

print("snapshot offset:", snapshot_offset)

for party_prefix in PARTIES:
    # Turn short prefix into the full Canton party ID.
    party = c8lab.find_party(party_prefix)

    holdings = holdings_at_offset(
        party,
        snapshot_offset
    )

    print()
    print("party:", party)
    print("holdings:", holdings)

    database.replace_holdings_for_party(
        party,
        holdings
    )

database.save_offset(snapshot_offset)

print()
print("ACS snapshot saved to scanner.db")