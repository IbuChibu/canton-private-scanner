import sqlite3
from decimal import Decimal


DB_NAME = "scanner.db"


def get_connection():
    return sqlite3.connect(DB_NAME)


def create_tables():
    conn = get_connection()

    # --------------------------------------------------
    # CURRENT HOLDINGS
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
    # --------------------------------------------------

    conn.execute("""
        CREATE TABLE IF NOT EXISTS scanner_state (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            last_offset INTEGER NOT NULL
        )
    """)

    # --------------------------------------------------
    # HOLDING EVENT HISTORY
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
    # RECONSTRUCTED TRANSFERS
    # --------------------------------------------------

    conn.execute("""
        CREATE TABLE IF NOT EXISTS transfers (
            update_id TEXT NOT NULL,
            event_id TEXT NOT NULL,

            record_time TEXT NOT NULL,
            migration_id INTEGER NOT NULL,

            sender TEXT NOT NULL,
            receiver TEXT NOT NULL,

            amount TEXT NOT NULL,
            instrument TEXT,

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
        CREATE INDEX IF NOT EXISTS idx_transfers_record_time
        ON transfers (
            record_time
        )
    """)

    # --------------------------------------------------
    # PUBLIC SCAN API CURSOR
    # --------------------------------------------------

    conn.execute("""
        CREATE TABLE IF NOT EXISTS scan_state (
            id INTEGER PRIMARY KEY CHECK (id = 1),

            migration_id INTEGER NOT NULL,
            record_time TEXT NOT NULL
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
    """, (offset,))

    conn.commit()
    conn.close()


# =========================================================
# INITIAL ACS STATE
# =========================================================

def replace_holdings_for_party(party, holdings):
    conn = get_connection()

    conn.execute(
        """
        DELETE FROM holdings
        WHERE party = ?
        """,
        (party,)
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
    Replace holdings for all tracked parties and save the
    ledger offset in the same transaction.
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
                (party,)
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
        """, (offset,))

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
    conn = get_connection()

    rows = conn.execute("""
        SELECT
            party,
            instrument,
            SUM(CAST(amount AS REAL))
        FROM holdings
        GROUP BY party, instrument
        ORDER BY party
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
# LIVE HOLDING UPDATES
# =========================================================

def apply_holding_changes(
    created_holdings,
    archived_contract_ids,
    offset,
    update_id,
    record_time=None
):
    """
    Apply one Canton transaction to SQLite.

    Everything below happens atomically:

    1. Record archived Holdings in history.
    2. Remove archived Holdings.
    3. Insert newly created Holdings.
    4. Record created Holdings in history.
    5. Save the latest ledger offset.

    If anything fails, the whole transaction rolls back.
    """

    conn = get_connection()

    try:
        conn.execute("BEGIN")

        # --------------------------------------------------
        # ARCHIVED HOLDINGS
        # --------------------------------------------------

        for contract_id in archived_contract_ids:

            # Get the existing Holding BEFORE deleting it.

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
                (contract_id,)
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

            conn.execute(
                """
                DELETE FROM holdings
                WHERE contract_id = ?
                """,
                (contract_id,)
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
            (offset,)
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
    Return raw Holding create/archive history for one party.
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
        ORDER BY offset DESC, id DESC
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

def resolve_party(party_or_prefix):
    """
    Accept either a short party prefix or a complete Canton
    party identifier.

    Searches:

        holdings
        holding_history
        transfers
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
            or party.startswith(party_or_prefix)
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

def get_balance_for_party(party):
    """
    Calculate the current balance from active Holdings.

    Decimal is used rather than float so token amounts
    are not silently rounded.
    """

    conn = get_connection()

    rows = conn.execute("""
        SELECT
            amount,
            instrument
        FROM holdings
        WHERE party = ?
    """, (party,)).fetchall()

    conn.close()

    totals = {}

    for amount, instrument in rows:

        if instrument not in totals:
            totals[instrument] = Decimal("0")

        totals[instrument] += Decimal(amount)

    return [
        {
            "instrument": instrument,
            "amount": str(amount)
        }

        for instrument, amount in totals.items()
    ]


# =========================================================
# PUBLIC SCAN API STATE
# =========================================================

def get_scan_state():
    """
    Return the last processed public Scan API cursor.

    The Scan API cursor is:

        migration_id
        record_time
    """

    conn = get_connection()

    row = conn.execute("""
        SELECT
            migration_id,
            record_time
        FROM scan_state
        WHERE id = 1
    """).fetchone()

    conn.close()

    if row is None:
        return None

    return {
        "migration_id": row[0],
        "record_time": row[1]
    }


# =========================================================
# SAVE RECONSTRUCTED TRANSFERS
# =========================================================

def save_transfers_and_scan_state(
    transfers,
    migration_id,
    record_time
):
    """
    Save extracted transfer rows and advance the public
    Scan API cursor atomically.

    If the process crashes before COMMIT, neither the
    transfers nor the cursor are advanced.
    """

    conn = get_connection()

    try:
        conn.execute("BEGIN")

        for transfer in transfers:

            conn.execute("""
                INSERT OR IGNORE INTO transfers (
                    update_id,
                    event_id,
                    record_time,
                    migration_id,
                    sender,
                    receiver,
                    amount,
                    instrument
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                transfer["update_id"],
                transfer["event_id"],
                transfer["record_time"],
                transfer["migration_id"],
                transfer["sender"],
                transfer["receiver"],
                transfer["amount"],
                transfer["instrument"]
            ))

        conn.execute("""
            INSERT INTO scan_state (
                id,
                migration_id,
                record_time
            )
            VALUES (1, ?, ?)

            ON CONFLICT(id)
            DO UPDATE SET
                migration_id = excluded.migration_id,
                record_time = excluded.record_time
        """, (
            migration_id,
            record_time
        ))

        conn.commit()

    except Exception:
        conn.rollback()
        raise

    finally:
        conn.close()


# =========================================================
# TRANSFER HISTORY
# =========================================================

def get_transfers_for_party(
    party,
    limit=100
):
    """
    Return reconstructed transfers where the party was
    either the sender or receiver.
    """

    conn = get_connection()

    rows = conn.execute("""
        SELECT
            update_id,
            event_id,
            record_time,
            migration_id,
            sender,
            receiver,
            amount,
            instrument
        FROM transfers
        WHERE
            sender = ?
            OR receiver = ?
        ORDER BY
            record_time DESC
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
            "record_time": row[2],
            "migration_id": row[3],
            "sender": row[4],
            "receiver": row[5],
            "amount": row[6],
            "instrument": row[7]
        }

        for row in rows
    ]
