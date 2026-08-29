import json
import urllib.request


URL = (
    "https://sv-proxy.dev.digik.cantor8.tech"
    "/api/scan/v1/updates"
)


def walk_event(event_id, events, depth=0):
    event = events.get(event_id)

    if event is None:
        return

    indent = "  " * depth

    event_type = event.get("event_type")
    template = event.get("template_id", "")

    print(
        f"{indent}- {event_type} | "
        f"{template.split(':')[-1]}"
    )

    if event_type == "exercised_event":

        print(
            f"{indent}  choice: "
            f"{event.get('choice')}"
        )

        print(
            f"{indent}  consuming: "
            f"{event.get('consuming')}"
        )

        # Recursively visit children.
        for child_id in event.get(
            "child_event_ids", []
        ):
            walk_event(
                child_id,
                events,
                depth + 1
            )


body = {
    "page_size": 20
}


request = urllib.request.Request(
    URL,
    method="POST",
    data=json.dumps(body).encode(),
    headers={
        "Content-Type": "application/json"
    }
)


response = urllib.request.urlopen(
    request,
    timeout=30
)


data = json.loads(
    response.read()
)


updates = data.get(
    "transactions",
    data.get("updates", [])
)


for update in updates:

    print()
    print("=" * 80)

    print(
        "update:",
        update.get("update_id")
    )

    print(
        "time:",
        update.get("record_time")
    )

    events = update.get(
        "events_by_id",
        {}
    )

    roots = update.get(
        "root_event_ids",
        []
    )

    # Start at each root and recursively
    # follow its children.
    for root_id in roots:
        walk_event(
            root_id,
            events
        )