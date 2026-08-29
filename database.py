import json
import sqlite3
from decimal import Decimal


DB_NAME = "scanner.db"


# =========================================================
# CONNECTION
# =========================================================

def get_connection():
    return sqlite3.connect(DB_NAME)


# =========================================================
# CREATE TABLES
# =========================================================

def create_tables():
    conn = get_connection()

    # --------------------------------------------------
    # CURRENT HOLDINGS
    #
    # This represents the scanner's current view of the
    # active Holding contracts for the tracked parties.
    # --------------------------------------------------

    conn.execute("""
        CREATE TABLE IF NOT EXISTS holdings (
            contract_id TEXT PRIMARY KEY,
            party TEXT NOT NULL,
            amount TEXT NOT NULL,
            instrument TEXT,
            admin TEXT,
            locked INTEGER NOT NULL
        )
    """)

    # --------------------------------------------------
    # PRIVATE LEDGER SCANNER STATE
    #
    # The latest Canton Ledger API offset that we have
    # successfully processed.
    # --------------------------------------------------

    conn.execute("""
        CREATE TABLE IF NOT EXISTS scanner_state (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            last_offset INTEGER NOT NULL
        )
    """)

    # --------------------------------------------------
    # HOLDING EVENT HISTORY
    #
    # Low-level history of Holding contracts appearing
    # and disappearing.
    #
    # Important:
    # This is NOT the same thing as semantic transfer
    # history. A Holding could change because of:
    #
    # - transfer
    # - mint
    # - burn
    # - fee
    # - another ledger operation
    # --------------------------------------------------

    conn.execute("""
        CREATE TABLE IF NOT EXISTS holding_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,

            update_id TEXT NOT NULL,
            offset INTEGER NOT NULL,
            record_time TEXT,

            event_type TEXT NOT NULL,

            contract_id TEXT NOT NULL,
            party TEXT,
            amount TEXT,
            instrument TEXT,
            admin TEXT,
            locked INTEGER,

            UNIQUE (
                update_id,
                event_type,
                contract_id
            )
        )
    """)

    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_holding_history_party_offset
        ON holding_history (
            party,
            offset
        )
    """)

    # --------------------------------------------------
    # RAW PRIVATE LEDGER EVENTS
    #
    # This is our durable private event archive.
    #
    # Canton may eventually prune old participant data,
    # but once our scanner has seen an event we preserve
    # it here in SQLite.
    # --------------------------------------------------

    conn.execute("""
        CREATE TABLE IF NOT EXISTS private_events (
            update_id TEXT NOT NULL,
            event_id TEXT NOT NULL,

            offset INTEGER NOT NULL,
            record_time TEXT,

            event_type TEXT NOT NULL,
            template_id TEXT,
            choice TEXT,

            raw_json TEXT NOT NULL,

            PRIMARY KEY (
                update_id,
                event_id
            )
        )
    """)

    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_private_events_offset
        ON private_events (
            offset
        )
    """)

    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_private_events_update
        ON private_events (
            update_id
        )
    """)

    # --------------------------------------------------
    # RECONSTRUCTED PRIVATE TRANSFERS
    #
    # Only store a transfer here when the private ledger
    # event explicitly gives us enough information to say:
    #
    # sender -> receiver -> amount
    #
    # We do NOT infer every Holding archive/create pair
    # to be a transfer.
    # --------------------------------------------------

    conn.execute("""
        CREATE TABLE IF NOT EXISTS transfers (
            update_id TEXT NOT NULL,
            event_id TEXT NOT NULL,

            offset INTEGER NOT NULL,
            record_time TEXT,

            sender TEXT NOT NULL,
            receiver TEXT NOT NULL,

            amount TEXT NOT NULL,
            instrument TEXT,
            choice TEXT,

            PRIMARY KEY (
                update_id,
                event_id
            )
        )
    """)

    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_transfers_sender
        ON transfers (
            sender
        )
    """)

    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_transfers_receiver
        ON transfers (
            receiver
        )
    """)

    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_transfers_offset
        ON transfers (
            offset
        )
    """)

    conn.commit()
    conn.close()


# =========================================================
# PRIVATE LEDGER OFFSET
# =========================================================

def get_saved_offset():
    conn = get_connection()

    row = conn.execute(
        """
        SELECT last_offset
        FROM scanner_state
        WHERE id = 1
        """
    ).fetchone()

    conn.close()

    if row is None:
        return None

    return row[0]


def save_offset(offset):
    """
    Save the latest processed Canton Ledger API offset.

    Used for checkpoint messages where no matching
    transaction needs to be written.
    """

    conn = get_connection()

    conn.execute("""
        INSERT INTO scanner_state (
            id,
            last_offset
        )
        VALUES (1, ?)

        ON CONFLICT(id)
        DO UPDATE SET
            last_offset = excluded.last_offset
    """, (
        offset,
    ))

    conn.commit()
    conn.close()


# =========================================================
# INITIAL ACS STATE
# =========================================================

def replace_holdings_for_party(
    party,
    holdings
):
    """
    Replace the current Holdings for one party.

    Mostly useful during initial ACS/bootstrap work.
    """

    conn = get_connection()

    conn.execute(
        """
        DELETE FROM holdings
        WHERE party = ?
        """,
        (
            party,
        )
    )

    for holding in holdings:

        conn.execute("""
            INSERT INTO holdings (
                contract_id,
                party,
                amount,
                instrument,
                admin,
                locked
            )
            VALUES (?, ?, ?, ?, ?, ?)
        """, (
            holding["contractId"],
            party,
            holding["amount"],
            holding["instrument"],
            holding["admin"],
            int(holding["locked"])
        ))

    conn.commit()
    conn.close()


def replace_all_holdings_and_save_offset(
    holdings_by_party,
    offset
):
    """
    Replace the ACS Holdings for all tracked parties and
    save the snapshot ledger offset atomically.

    This prevents the database from containing Holdings
    from one ledger position while claiming to have
    processed another position.
    """

    conn = get_connection()

    try:

        conn.execute("BEGIN")

        for party, holdings in holdings_by_party.items():

            conn.execute(
                """
                DELETE FROM holdings
                WHERE party = ?
                """,
                (
                    party,
                )
            )

            for holding in holdings:

                conn.execute("""
                    INSERT INTO holdings (
                        contract_id,
                        party,
                        amount,
                        instrument,
                        admin,
                        locked
                    )
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (
                    holding["contractId"],
                    party,
                    holding["amount"],
                    holding["instrument"],
                    holding["admin"],
                    int(holding["locked"])
                ))

        conn.execute("""
            INSERT INTO scanner_state (
                id,
                last_offset
            )
            VALUES (1, ?)

            ON CONFLICT(id)
            DO UPDATE SET
                last_offset = excluded.last_offset
        """, (
            offset,
        ))

        conn.commit()

    except Exception:

        conn.rollback()
        raise

    finally:

        conn.close()


# =========================================================
# BALANCE DEBUG OUTPUT
# =========================================================

def print_balances():
    """
    Print the current indexed balances.

    This is only debug output.

    The API uses Decimal in get_balance_for_party()
    instead of SQLite REAL arithmetic.
    """

    conn = get_connection()

    rows = conn.execute("""
        SELECT
            party,
            instrument,
            SUM(CAST(amount AS REAL))
        FROM holdings
        GROUP BY
            party,
            instrument
        ORDER BY
            party
    """).fetchall()

    conn.close()

    print()
    print("CURRENT INDEXED BALANCES")

    for party, instrument, amount in rows:

        print(
            party.split("::")[0],
            amount,
            instrument
        )


# =========================================================
# APPLY ONE PRIVATE LEDGER TRANSACTION
# =========================================================

def apply_holding_changes(
    created_holdings,
    archived_contract_ids,
    offset,
    update_id,
    record_time=None,
    private_events=None,
    transfers=None
):
    """
    Apply one Canton private transaction to SQLite.

    Everything below happens atomically:

    1. Record archived Holdings in history.
    2. Remove archived Holdings from current state.
    3. Insert newly created Holdings.
    4. Record created Holdings in Holding history.
    5. Preserve all visible private ledger events.
    6. Preserve confidently reconstructed transfers.
    7. Save the latest Ledger API offset.

    If anything fails, the entire transaction rolls back.

    This means scanner_state never advances beyond data
    that was successfully persisted.
    """

    conn = get_connection()

    private_events = private_events or []
    transfers = transfers or []

    try:

        conn.execute("BEGIN")

        # --------------------------------------------------
        # ARCHIVED HOLDINGS
        # --------------------------------------------------

        for contract_id in archived_contract_ids:

            # The ArchivedEvent itself may only identify the
            # contract that disappeared.
            #
            # Our holdings table already has useful details,
            # so retrieve them BEFORE deleting the contract.

            existing = conn.execute(
                """
                SELECT
                    party,
                    amount,
                    instrument,
                    admin,
                    locked
                FROM holdings
                WHERE contract_id = ?
                """,
                (
                    contract_id,
                )
            ).fetchone()

            if existing is not None:

                (
                    party,
                    amount,
                    instrument,
                    admin,
                    locked
                ) = existing

                conn.execute(
                    """
                    INSERT OR IGNORE INTO holding_history (
                        update_id,
                        offset,
                        record_time,
                        event_type,
                        contract_id,
                        party,
                        amount,
                        instrument,
                        admin,
                        locked
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        update_id,
                        offset,
                        record_time,
                        "archived",
                        contract_id,
                        party,
                        amount,
                        instrument,
                        admin,
                        locked
                    )
                )

            # If this archived contract wasn't a Holding we
            # were tracking, this simply deletes nothing.

            conn.execute(
                """
                DELETE FROM holdings
                WHERE contract_id = ?
                """,
                (
                    contract_id,
                )
            )

        # --------------------------------------------------
        # CREATED HOLDINGS
        # --------------------------------------------------

        for holding in created_holdings:

            conn.execute(
                """
                INSERT INTO holdings (
                    contract_id,
                    party,
                    amount,
                    instrument,
                    admin,
                    locked
                )
                VALUES (?, ?, ?, ?, ?, ?)

                ON CONFLICT(contract_id)
                DO UPDATE SET
                    party = excluded.party,
                    amount = excluded.amount,
                    instrument = excluded.instrument,
                    admin = excluded.admin,
                    locked = excluded.locked
                """,
                (
                    holding["contractId"],
                    holding["party"],
                    holding["amount"],
                    holding["instrument"],
                    holding["admin"],
                    int(holding["locked"])
                )
            )

            conn.execute(
                """
                INSERT OR IGNORE INTO holding_history (
                    update_id,
                    offset,
                    record_time,
                    event_type,
                    contract_id,
                    party,
                    amount,
                    instrument,
                    admin,
                    locked
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    update_id,
                    offset,
                    record_time,
                    "created",
                    holding["contractId"],
                    holding["party"],
                    holding["amount"],
                    holding["instrument"],
                    holding["admin"],
                    int(holding["locked"])
                )
            )

        # --------------------------------------------------
        # RAW PRIVATE LEDGER EVENTS
        # --------------------------------------------------

        for event in private_events:

            conn.execute(
                """
                INSERT OR IGNORE INTO private_events (
                    update_id,
                    event_id,
                    offset,
                    record_time,
                    event_type,
                    template_id,
                    choice,
                    raw_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    update_id,
                    event["event_id"],
                    offset,
                    record_time,
                    event["event_type"],
                    event.get("template_id"),
                    event.get("choice"),
                    json.dumps(
                        event["raw"]
                    )
                )
            )

        # --------------------------------------------------
        # SEMANTIC PRIVATE TRANSFERS
        # --------------------------------------------------

        for transfer in transfers:

            conn.execute(
                """
                INSERT OR IGNORE INTO transfers (
                    update_id,
                    event_id,
                    offset,
                    record_time,
                    sender,
                    receiver,
                    amount,
                    instrument,
                    choice
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    update_id,
                    transfer["event_id"],
                    offset,
                    record_time,
                    transfer["sender"],
                    transfer["receiver"],
                    transfer["amount"],
                    transfer.get("instrument"),
                    transfer.get("choice")
                )
            )

        # --------------------------------------------------
        # SAVE PRIVATE LEDGER OFFSET
        # --------------------------------------------------

        conn.execute(
            """
            INSERT INTO scanner_state (
                id,
                last_offset
            )
            VALUES (1, ?)

            ON CONFLICT(id)
            DO UPDATE SET
                last_offset = excluded.last_offset
            """,
            (
                offset,
            )
        )

        conn.commit()

    except Exception:

        conn.rollback()
        raise

    finally:

        conn.close()


# =========================================================
# HOLDING EVENT HISTORY
# =========================================================

def get_history_for_party(
    party,
    limit=100
):
    """
    Return low-level Holding create/archive history for
    one tracked party.

    This remains useful for debugging even once the main
    /history endpoint uses semantic transfers.
    """

    conn = get_connection()

    rows = conn.execute(
        """
        SELECT
            update_id,
            offset,
            record_time,
            event_type,
            contract_id,
            party,
            amount,
            instrument
        FROM holding_history
        WHERE party = ?
        ORDER BY
            offset DESC,
            id DESC
        LIMIT ?
        """,
        (
            party,
            limit
        )
    ).fetchall()

    conn.close()

    return [
        {
            "update_id": row[0],
            "offset": row[1],
            "record_time": row[2],
            "event_type": row[3],
            "contract_id": row[4],
            "party": row[5],
            "amount": row[6],
            "instrument": row[7]
        }

        for row in rows
    ]


# =========================================================
# PARTY RESOLUTION
# =========================================================

def resolve_party(
    party_or_prefix
):
    """
    Accept either:

        00209eb9...

    or:

        00209eb9...::1220...

    and return the complete Canton party identifier.

    Parties can be discovered from current Holdings,
    Holding history, or reconstructed transfer history.
    """

    conn = get_connection()

    rows = conn.execute("""
        SELECT DISTINCT party
        FROM (

            SELECT party
            FROM holdings
            WHERE party IS NOT NULL

            UNION

            SELECT party
            FROM holding_history
            WHERE party IS NOT NULL

            UNION

            SELECT sender AS party
            FROM transfers
            WHERE sender IS NOT NULL

            UNION

            SELECT receiver AS party
            FROM transfers
            WHERE receiver IS NOT NULL
        )
    """).fetchall()

    conn.close()

    parties = [
        row[0]
        for row in rows
    ]

    matches = [
        party
        for party in parties
        if (
            party == party_or_prefix
            or party.startswith(
                party_or_prefix
            )
        )
    ]

    if len(matches) == 0:
        return None

    if len(matches) > 1:

        raise ValueError(
            f"Party prefix '{party_or_prefix}' is ambiguous."
        )

    return matches[0]


# =========================================================
# CURRENT BALANCE
# =========================================================

def get_balance_for_party(
    party
):
    """
    Calculate current balances from active Holdings.

    Decimal is deliberately used instead of float so
    token amounts do not silently lose precision.
    """

    conn = get_connection()

    rows = conn.execute("""
        SELECT
            amount,
            instrument
        FROM holdings
        WHERE party = ?
    """, (
        party,
    )).fetchall()

    conn.close()

    totals = {}

    for amount, instrument in rows:

        if instrument not in totals:

            totals[instrument] = Decimal(
                "0"
            )

        totals[instrument] += Decimal(
            amount
        )

    return [
        {
            "instrument": instrument,
            "amount": str(amount)
        }

        for instrument, amount
        in totals.items()
    ]


# =========================================================
# PRIVATE TRANSFER HISTORY
# =========================================================

def get_transfers_for_party(
    party,
    limit=100
):
    """
    Return reconstructed private transfers where the
    requested party was either sender or receiver.
    """

    conn = get_connection()

    rows = conn.execute("""
        SELECT
            update_id,
            event_id,
            offset,
            record_time,
            sender,
            receiver,
            amount,
            instrument,
            choice
        FROM transfers
        WHERE
            sender = ?
            OR receiver = ?
        ORDER BY
            offset DESC
        LIMIT ?
    """, (
        party,
        party,
        limit
    )).fetchall()

    conn.close()

    return [
        {
            "update_id": row[0],
            "event_id": row[1],
            "offset": row[2],
            "record_time": row[3],
            "sender": row[4],
            "receiver": row[5],
            "amount": row[6],
            "instrument": row[7],
            "choice": row[8]
        }

        for row in rows
    ]


# =========================================================
# RAW PRIVATE EVENT DEBUGGING
# =========================================================

def get_recent_private_events(
    limit=100
):
    """
    Return recent private events.

    Primarily useful while developing and verifying the
    transaction parser.
    """

    conn = get_connection()

    rows = conn.execute("""
        SELECT
            update_id,
            event_id,
            offset,
            record_time,
            event_type,
            template_id,
            choice,
            raw_json
        FROM private_events
        ORDER BY offset DESC
        LIMIT ?
    """, (
        limit,
    )).fetchall()

    conn.close()

    return [
        {
            "update_id": row[0],
            "event_id": row[1],
            "offset": row[2],
            "record_time": row[3],
            "event_type": row[4],
            "template_id": row[5],
            "choice": row[6],
            "raw": json.loads(row[7])
        }

        for row in rows
    ]