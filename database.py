"""SQLite persistence for the private Canton ledger scanner."""

import json
import os
import sqlite3
from decimal import Decimal, InvalidOperation


DB_NAME = os.environ.get("SCANNER_DB", "scanner.db")


def get_connection():
    """Open a connection suitable for the scanner and concurrent API reads."""

    return sqlite3.connect(DB_NAME, timeout=30)


def _table_columns(conn, table):
    return {
        row[1]
        for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
    }


def create_tables():
    """Create the private index schema and discard the old public Scan cursor."""

    conn = get_connection()
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("BEGIN")

        # The abandoned public Scan API prototype used migration_id. Its rows
        # are not private Ledger API transfers and must not be mixed with them.
        conn.execute("DROP TABLE IF EXISTS scan_state")
        transfer_columns = _table_columns(conn, "transfers")
        if transfer_columns and (
            "migration_id" in transfer_columns
            or "offset" not in transfer_columns
            or "choice" not in transfer_columns
        ):
            conn.execute("DROP TABLE transfers")
        conn.execute("DROP INDEX IF EXISTS idx_transfers_sender")
        conn.execute("DROP INDEX IF EXISTS idx_transfers_receiver")
        conn.execute("DROP INDEX IF EXISTS idx_transfers_offset")

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS holdings (
                contract_id TEXT PRIMARY KEY,
                party TEXT NOT NULL,
                amount TEXT NOT NULL,
                instrument TEXT,
                admin TEXT,
                locked INTEGER NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS scanner_state (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                last_offset INTEGER NOT NULL
            )
            """
        )
        conn.execute(
            """
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
                UNIQUE (update_id, event_type, contract_id)
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_holding_history_party_offset
            ON holding_history (party, offset)
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS private_events (
                update_id TEXT NOT NULL,
                event_id TEXT NOT NULL,
                offset INTEGER NOT NULL,
                record_time TEXT,
                event_type TEXT NOT NULL,
                template_id TEXT,
                choice TEXT,
                raw_json TEXT NOT NULL,
                PRIMARY KEY (update_id, event_id)
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_private_events_offset
            ON private_events (offset)
            """
        )
        conn.execute(
            """
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
                PRIMARY KEY (update_id, event_id)
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_transfers_sender_offset
            ON transfers (sender, offset)
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_transfers_receiver_offset
            ON transfers (receiver, offset)
            """
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_saved_offset():
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT last_offset FROM scanner_state WHERE id = 1"
        ).fetchone()
        return None if row is None else row[0]
    finally:
        conn.close()


def _save_offset(conn, offset):
    """Advance, but never regress, the private Ledger API checkpoint."""

    conn.execute(
        """
        INSERT INTO scanner_state (id, last_offset)
        VALUES (1, ?)
        ON CONFLICT(id) DO UPDATE SET
            last_offset = excluded.last_offset
        WHERE excluded.last_offset > scanner_state.last_offset
        """,
        (int(offset),),
    )


def save_offset(offset):
    """Persist an offset checkpoint that contains no transaction data."""

    conn = get_connection()
    try:
        _save_offset(conn, offset)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _insert_holding(conn, holding, default_party=None):
    party = holding.get("party") or default_party
    if not holding.get("contractId") or not party or holding.get("amount") is None:
        raise ValueError("Holding requires contractId, party, and amount")
    try:
        amount = Decimal(str(holding["amount"]))
    except (InvalidOperation, TypeError, ValueError) as error:
        raise ValueError("Holding amount must be a valid Decimal") from error
    if not amount.is_finite() or amount < 0:
        raise ValueError("Holding amount must be finite and non-negative")

    conn.execute(
        """
        INSERT INTO holdings (
            contract_id, party, amount, instrument, admin, locked
        ) VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(contract_id) DO UPDATE SET
            party = excluded.party,
            amount = excluded.amount,
            instrument = excluded.instrument,
            admin = excluded.admin,
            locked = excluded.locked
        """,
        (
            holding["contractId"],
            party,
            str(holding["amount"]),
            holding.get("instrument"),
            holding.get("admin"),
            int(bool(holding.get("locked"))),
        ),
    )


def replace_holdings_for_party(party, holdings):
    """Replace one party's current Holdings (legacy/bootstrap helper)."""

    conn = get_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("DELETE FROM holdings WHERE party = ?", (party,))
        for holding in holdings:
            _insert_holding(conn, holding, party)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def replace_all_holdings_and_save_offset(holdings_by_party, offset):
    """Atomically install an ACS snapshot and its exact ledger offset."""

    offset = int(offset)
    conn = get_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT last_offset FROM scanner_state WHERE id = 1"
        ).fetchone()
        if row is not None and offset < row[0]:
            raise ValueError("ACS snapshot offset cannot regress scanner state")
        conn.execute("DELETE FROM holdings")
        for party, holdings in holdings_by_party.items():
            for holding in holdings:
                _insert_holding(conn, holding, party)
        _save_offset(conn, offset)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def apply_holding_changes(
    created_holdings,
    archived_contract_ids,
    offset,
    update_id,
    record_time=None,
    private_events=None,
    transfers=None,
):
    """Apply one ledger transaction and its offset in one SQLite transaction.

    Returns ``False`` when the offset was already processed. This monotonic
    guard makes reconnect replay harmless and prevents old creates from
    resurrecting contracts that a later update archived.
    """

    offset = int(offset)
    private_events = private_events or []
    transfers = transfers or []
    conn = get_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT last_offset FROM scanner_state WHERE id = 1"
        ).fetchone()
        if row is not None and offset <= row[0]:
            conn.rollback()
            return False

        for contract_id in dict.fromkeys(archived_contract_ids):
            existing = conn.execute(
                """
                SELECT party, amount, instrument, admin, locked
                FROM holdings WHERE contract_id = ?
                """,
                (contract_id,),
            ).fetchone()
            if existing is not None:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO holding_history (
                        update_id, offset, record_time, event_type, contract_id,
                        party, amount, instrument, admin, locked
                    ) VALUES (?, ?, ?, 'archived', ?, ?, ?, ?, ?, ?)
                    """,
                    (update_id, offset, record_time, contract_id, *existing),
                )
            conn.execute("DELETE FROM holdings WHERE contract_id = ?", (contract_id,))

        for holding in created_holdings:
            _insert_holding(conn, holding)
            conn.execute(
                """
                INSERT OR IGNORE INTO holding_history (
                    update_id, offset, record_time, event_type, contract_id,
                    party, amount, instrument, admin, locked
                ) VALUES (?, ?, ?, 'created', ?, ?, ?, ?, ?, ?)
                """,
                (
                    update_id,
                    offset,
                    record_time,
                    holding["contractId"],
                    holding["party"],
                    str(holding["amount"]),
                    holding.get("instrument"),
                    holding.get("admin"),
                    int(bool(holding.get("locked"))),
                ),
            )

        for event in private_events:
            conn.execute(
                """
                INSERT OR IGNORE INTO private_events (
                    update_id, event_id, offset, record_time, event_type,
                    template_id, choice, raw_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    update_id,
                    event["event_id"],
                    offset,
                    record_time,
                    event["event_type"],
                    event.get("template_id"),
                    event.get("choice"),
                    json.dumps(event["raw"], separators=(",", ":"), sort_keys=True),
                ),
            )

        for transfer in transfers:
            sender = transfer.get("sender")
            receiver = transfer.get("receiver")
            if not isinstance(sender, str) or not sender:
                raise ValueError("Transfer sender must be a non-empty party")
            if not isinstance(receiver, str) or not receiver:
                raise ValueError("Transfer receiver must be a non-empty party")
            try:
                transfer_amount = Decimal(str(transfer["amount"]))
            except (InvalidOperation, KeyError, TypeError, ValueError) as error:
                raise ValueError("Transfer amount must be a valid Decimal") from error
            if not transfer_amount.is_finite() or transfer_amount <= 0:
                raise ValueError("Transfer amount must be finite and positive")
            conn.execute(
                """
                INSERT OR IGNORE INTO transfers (
                    update_id, event_id, offset, record_time, sender, receiver,
                    amount, instrument, choice
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    update_id,
                    transfer["event_id"],
                    offset,
                    record_time,
                    sender,
                    receiver,
                    str(transfer["amount"]),
                    transfer.get("instrument"),
                    transfer.get("choice"),
                ),
            )

        _save_offset(conn, offset)
        conn.commit()
        return True
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def print_balances():
    """Print current balances using the same Decimal aggregation as the API."""

    conn = get_connection()
    try:
        parties = [row[0] for row in conn.execute(
            "SELECT DISTINCT party FROM holdings ORDER BY party"
        )]
    finally:
        conn.close()

    print("\nCURRENT INDEXED BALANCES")
    for party in parties:
        for balance in get_balance_for_party(party):
            print(party.split("::")[0], balance["amount"], balance["instrument"])


def get_history_for_party(party, limit=100):
    """Return low-level Holding create/archive effects for debugging."""

    conn = get_connection()
    try:
        rows = conn.execute(
            """
            SELECT update_id, offset, record_time, event_type, contract_id,
                   party, amount, instrument
            FROM holding_history
            WHERE party = ?
            ORDER BY offset DESC, id DESC
            LIMIT ?
            """,
            (party, int(limit)),
        ).fetchall()
    finally:
        conn.close()
    keys = (
        "update_id", "offset", "record_time", "event_type", "contract_id",
        "party", "amount", "instrument",
    )
    return [dict(zip(keys, row)) for row in rows]


def resolve_party(party_or_prefix):
    """Resolve an exact party or an unambiguous prefix known to the index."""

    conn = get_connection()
    try:
        rows = conn.execute(
            """
            SELECT party FROM holdings WHERE party IS NOT NULL
            UNION
            SELECT party FROM holding_history WHERE party IS NOT NULL
            UNION
            SELECT sender FROM transfers WHERE sender IS NOT NULL
            UNION
            SELECT receiver FROM transfers WHERE receiver IS NOT NULL
            """
        ).fetchall()
    finally:
        conn.close()

    matches = sorted({
        row[0]
        for row in rows
        if row[0] == party_or_prefix or row[0].startswith(party_or_prefix)
    })
    if not matches:
        return None
    if len(matches) > 1:
        raise ValueError(f"Party prefix '{party_or_prefix}' is ambiguous.")
    return matches[0]


def get_balance_for_party(party):
    """Aggregate active Holding amounts exactly with Decimal."""

    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT amount, instrument FROM holdings WHERE party = ?",
            (party,),
        ).fetchall()
    finally:
        conn.close()

    totals = {}
    for amount, instrument in rows:
        totals[instrument] = totals.get(instrument, Decimal("0")) + Decimal(amount)
    return [
        {"instrument": instrument, "amount": str(totals[instrument])}
        for instrument in sorted(totals, key=lambda value: value or "")
    ]


def get_transfers_for_party(party, limit=100):
    """Return semantic transfers in which the party is sender or receiver."""

    conn = get_connection()
    try:
        rows = conn.execute(
            """
            SELECT update_id, event_id, offset, record_time, sender, receiver,
                   amount, instrument, choice
            FROM transfers
            WHERE sender = ? OR receiver = ?
            ORDER BY offset DESC, event_id DESC
            LIMIT ?
            """,
            (party, party, int(limit)),
        ).fetchall()
    finally:
        conn.close()
    keys = (
        "update_id", "event_id", "offset", "record_time", "sender", "receiver",
        "amount", "instrument", "choice",
    )
    return [dict(zip(keys, row)) for row in rows]


def get_recent_private_events(limit=100):
    """Return the durable raw private-event archive for parser debugging."""

    conn = get_connection()
    try:
        rows = conn.execute(
            """
            SELECT update_id, event_id, offset, record_time, event_type,
                   template_id, choice, raw_json
            FROM private_events
            ORDER BY offset DESC, event_id DESC
            LIMIT ?
            """,
            (int(limit),),
        ).fetchall()
    finally:
        conn.close()
    keys = (
        "update_id", "event_id", "offset", "record_time", "event_type",
        "template_id", "choice", "raw",
    )
    result = []
    for row in rows:
        values = list(row)
        values[-1] = json.loads(values[-1])
        result.append(dict(zip(keys, values)))
    return result
