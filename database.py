"""SQLite persistence for the private Canton ledger scanner."""

import json
import os
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation


DB_NAME = os.environ.get("SCANNER_DB", "scanner.db")


def utc_now():
    return datetime.now(timezone.utc).isoformat()


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
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS party_catalog (
                party TEXT PRIMARY KEY,
                display_name TEXT NOT NULL,
                is_local INTEGER,
                can_act_as INTEGER NOT NULL DEFAULT 0,
                can_read_as INTEGER NOT NULL DEFAULT 0,
                readable INTEGER NOT NULL DEFAULT 0,
                discovery_source TEXT NOT NULL,
                refreshed_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_party_catalog_display_name
            ON party_catalog (display_name)
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS party_catalog_state (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                user_id TEXT,
                read_as_any INTEGER NOT NULL DEFAULT 0,
                refreshed_at TEXT,
                complete INTEGER NOT NULL DEFAULT 0,
                error TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS party_selection (
                party TEXT PRIMARY KEY,
                selected_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS tracked_parties (
                party TEXT PRIMARY KEY,
                active INTEGER NOT NULL,
                activated_at_offset INTEGER,
                deactivated_at_offset INTEGER
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS scanner_config (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                desired_revision INTEGER NOT NULL,
                active_revision INTEGER NOT NULL
            )
            """
        )
        conn.execute(
            """
            INSERT OR IGNORE INTO scanner_config (
                id, desired_revision, active_revision
            ) VALUES (1, 0, 0)
            """
        )

        # Existing databases predate configurable party selection. Seed both
        # desired and active state from their current Holdings without moving
        # the private ledger offset or rereading ACS.
        tracked_count = conn.execute(
            "SELECT COUNT(*) FROM tracked_parties"
        ).fetchone()[0]
        selection_count = conn.execute(
            "SELECT COUNT(*) FROM party_selection"
        ).fetchone()[0]
        existing_parties = [
            row[0]
            for row in conn.execute(
                "SELECT DISTINCT party FROM holdings ORDER BY party"
            ).fetchall()
        ]
        if tracked_count == 0 and selection_count == 0 and existing_parties:
            offset_row = conn.execute(
                "SELECT last_offset FROM scanner_state WHERE id = 1"
            ).fetchone()
            offset = offset_row[0] if offset_row else None
            selected_at = utc_now()
            for party in existing_parties:
                conn.execute(
                    "INSERT INTO party_selection (party, selected_at) VALUES (?, ?)",
                    (party, selected_at),
                )
                conn.execute(
                    """
                    INSERT INTO tracked_parties (
                        party, active, activated_at_offset, deactivated_at_offset
                    ) VALUES (?, 1, ?, NULL)
                    """,
                    (party, offset),
                )
            conn.execute(
                """
                UPDATE scanner_config
                SET desired_revision = 1, active_revision = 1
                WHERE id = 1
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


def replace_party_catalog(entries, user_id, read_as_any, refreshed_at=None):
    """Atomically replace the readable catalog after a complete discovery."""

    refreshed_at = refreshed_at or utc_now()
    conn = get_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")

        # Rows that are neither selected nor historically tracked can be
        # discarded. Selected/tracked rows remain visible when access is
        # revoked, but are explicitly marked unreadable.
        conn.execute(
            """
            DELETE FROM party_catalog
            WHERE party NOT IN (SELECT party FROM party_selection)
              AND party NOT IN (SELECT party FROM tracked_parties)
            """
        )
        conn.execute(
            """
            UPDATE party_catalog
            SET can_act_as = 0,
                can_read_as = 0,
                readable = 0,
                discovery_source = 'retained_selection',
                refreshed_at = ?
            """,
            (refreshed_at,),
        )

        for entry in entries:
            party = entry.get("party")
            if not isinstance(party, str) or not party:
                raise ValueError("Catalog entries require a full party ID")
            conn.execute(
                """
                INSERT INTO party_catalog (
                    party, display_name, is_local, can_act_as, can_read_as,
                    readable, discovery_source, refreshed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(party) DO UPDATE SET
                    display_name = excluded.display_name,
                    is_local = excluded.is_local,
                    can_act_as = excluded.can_act_as,
                    can_read_as = excluded.can_read_as,
                    readable = excluded.readable,
                    discovery_source = excluded.discovery_source,
                    refreshed_at = excluded.refreshed_at
                """,
                (
                    party,
                    entry.get("display_name") or party.split("::", 1)[0],
                    (
                        None
                        if entry.get("is_local") is None
                        else int(bool(entry.get("is_local")))
                    ),
                    int(bool(entry.get("can_act_as"))),
                    int(bool(entry.get("can_read_as"))),
                    int(bool(entry.get("readable", True))),
                    entry.get("source") or "explicit_right",
                    refreshed_at,
                ),
            )

        # A legacy database may have desired/active parties before it has ever
        # had a catalog. Materialize any such missing rows so a rights refresh
        # that no longer exposes them still reports them as unreadable.
        retained_parties = conn.execute(
            """
            SELECT party FROM party_selection
            UNION
            SELECT party FROM tracked_parties
            """
        ).fetchall()
        for (party,) in retained_parties:
            conn.execute(
                """
                INSERT OR IGNORE INTO party_catalog (
                    party, display_name, is_local, can_act_as, can_read_as,
                    readable, discovery_source, refreshed_at
                ) VALUES (?, ?, NULL, 0, 0, 0, 'retained_selection', ?)
                """,
                (party, party.split("::", 1)[0], refreshed_at),
            )

        conn.execute(
            """
            INSERT INTO party_catalog_state (
                id, user_id, read_as_any, refreshed_at, complete, error
            ) VALUES (1, ?, ?, ?, 1, NULL)
            ON CONFLICT(id) DO UPDATE SET
                user_id = excluded.user_id,
                read_as_any = excluded.read_as_any,
                refreshed_at = excluded.refreshed_at,
                complete = 1,
                error = NULL
            """,
            (user_id, int(bool(read_as_any)), refreshed_at),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def record_party_catalog_error(error):
    """Record a failed refresh without altering the previous catalog rows."""

    conn = get_connection()
    try:
        conn.execute(
            """
            INSERT INTO party_catalog_state (id, complete, error)
            VALUES (1, 0, ?)
            ON CONFLICT(id) DO UPDATE SET complete = 0, error = excluded.error
            """,
            (str(error)[:1000],),
        )
        conn.commit()
    finally:
        conn.close()


def get_party_catalog_state():
    conn = get_connection()
    try:
        row = conn.execute(
            """
            SELECT user_id, read_as_any, refreshed_at, complete, error
            FROM party_catalog_state WHERE id = 1
            """
        ).fetchone()
        readable_count = conn.execute(
            "SELECT COUNT(*) FROM party_catalog WHERE readable = 1"
        ).fetchone()[0]
        total_count = conn.execute(
            "SELECT COUNT(*) FROM party_catalog"
        ).fetchone()[0]
    finally:
        conn.close()
    if row is None:
        row = (None, 0, None, 0, None)
    return {
        "user_id": row[0],
        "read_as_any": bool(row[1]),
        "refreshed_at": row[2],
        "complete": bool(row[3]),
        "error": row[4],
        "readable_count": readable_count,
        "total_count": total_count,
    }


def party_catalog_is_empty():
    return get_party_catalog_state()["total_count"] == 0


def list_parties(query="", limit=50, offset=0):
    """Search and paginate the cached catalog without contacting Canton."""

    limit = max(1, int(limit))
    offset = max(0, int(offset))
    query = (query or "").strip().lower()
    where = ""
    parameters = []
    if query:
        where = "WHERE instr(lower(c.party), ?) > 0 OR instr(lower(c.display_name), ?) > 0"
        parameters.extend((query, query))

    conn = get_connection()
    try:
        total = conn.execute(
            f"SELECT COUNT(*) FROM party_catalog c {where}",
            parameters,
        ).fetchone()[0]
        rows = conn.execute(
            f"""
            SELECT c.party, c.display_name, c.readable, c.is_local,
                   c.can_act_as, c.can_read_as, c.discovery_source,
                   CASE WHEN s.party IS NULL THEN 0 ELSE 1 END AS selected,
                   COALESCE(t.active, 0) AS active
            FROM party_catalog c
            LEFT JOIN party_selection s ON s.party = c.party
            LEFT JOIN tracked_parties t ON t.party = c.party
            {where}
            ORDER BY lower(c.display_name), c.party
            LIMIT ? OFFSET ?
            """,
            (*parameters, limit, offset),
        ).fetchall()
    finally:
        conn.close()

    items = []
    for row in rows:
        items.append(
            {
                "party": row[0],
                "display_name": row[1],
                "readable": bool(row[2]),
                "is_local": None if row[3] is None else bool(row[3]),
                "can_act_as": bool(row[4]),
                "can_read_as": bool(row[5]),
                "source": row[6],
                "selected": bool(row[7]),
                "active": bool(row[8]),
            }
        )
    return {"items": items, "total": total, "limit": limit, "offset": offset}


def _selection_status(conn):
    desired = [
        row[0]
        for row in conn.execute(
            "SELECT party FROM party_selection ORDER BY party"
        ).fetchall()
    ]
    active = [
        row[0]
        for row in conn.execute(
            "SELECT party FROM tracked_parties WHERE active = 1 ORDER BY party"
        ).fetchall()
    ]
    row = conn.execute(
        "SELECT desired_revision, active_revision FROM scanner_config WHERE id = 1"
    ).fetchone()
    desired_revision, active_revision = row or (0, 0)
    return {
        "desired_parties": desired,
        "active_parties": active,
        "desired_count": len(desired),
        "active_count": len(active),
        "desired_revision": desired_revision,
        "active_revision": active_revision,
        "restart_required": desired_revision != active_revision,
    }


def get_selection_status():
    conn = get_connection()
    try:
        return _selection_status(conn)
    finally:
        conn.close()


def get_desired_parties():
    return get_selection_status()["desired_parties"]


def get_active_parties():
    return get_selection_status()["active_parties"]


def set_desired_parties(parties, max_parties=50):
    """Validate and atomically replace the desired party selection."""

    if not isinstance(parties, list):
        raise ValueError("parties must be a JSON array")
    if any(not isinstance(party, str) or not party for party in parties):
        raise ValueError("Every selected party must be a non-empty string")
    unique = sorted(set(parties))
    if not unique:
        raise ValueError("At least one party must be selected")
    if len(unique) > int(max_parties):
        raise ValueError(f"At most {int(max_parties)} parties may be selected")
    if any("::" not in party for party in unique):
        raise ValueError("Selections must use full party IDs")

    conn = get_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        placeholders = ",".join("?" for _ in unique)
        rows = conn.execute(
            f"SELECT party, readable FROM party_catalog WHERE party IN ({placeholders})",
            unique,
        ).fetchall()
        found = {party: bool(readable) for party, readable in rows}
        unknown = [party for party in unique if party not in found]
        inaccessible = [party for party in unique if party in found and not found[party]]
        if unknown:
            raise ValueError("Unknown parties: " + ", ".join(unknown))
        if inaccessible:
            raise ValueError("Inaccessible parties: " + ", ".join(inaccessible))

        current = [
            row[0]
            for row in conn.execute(
                "SELECT party FROM party_selection ORDER BY party"
            ).fetchall()
        ]
        if current != unique:
            conn.execute("DELETE FROM party_selection")
            selected_at = utc_now()
            conn.executemany(
                "INSERT INTO party_selection (party, selected_at) VALUES (?, ?)",
                [(party, selected_at) for party in unique],
            )
            conn.execute(
                "UPDATE scanner_config SET desired_revision = desired_revision + 1 WHERE id = 1"
            )
        status = _selection_status(conn)
        conn.commit()
        return status
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def seed_default_selection(parties):
    """Select fresh-database defaults only when every default is readable."""

    parties = list(dict.fromkeys(parties))
    if not parties:
        return get_selection_status()
    conn = get_connection()
    try:
        existing = conn.execute(
            "SELECT COUNT(*) FROM party_selection"
        ).fetchone()[0]
        placeholders = ",".join("?" for _ in parties)
        readable = conn.execute(
            f"SELECT party FROM party_catalog WHERE readable = 1 AND party IN ({placeholders})",
            parties,
        ).fetchall()
    finally:
        conn.close()
    if existing or {row[0] for row in readable} != set(parties):
        return get_selection_status()
    return set_desired_parties(parties, max_parties=max(50, len(parties)))


def get_party_tracking(party):
    conn = get_connection()
    try:
        row = conn.execute(
            """
            SELECT active, activated_at_offset, deactivated_at_offset
            FROM tracked_parties WHERE party = ?
            """,
            (party,),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    return {
        "active": bool(row[0]),
        "activated_at_offset": row[1],
        "deactivated_at_offset": row[2],
    }


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


def replace_all_holdings_and_save_offset(
    holdings_by_party,
    offset,
    active_parties=None,
    expected_revision=None,
):
    """Atomically install an ACS snapshot and its exact ledger offset."""

    offset = int(offset)
    active_parties = sorted(set(active_parties or holdings_by_party.keys()))
    conn = get_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT last_offset FROM scanner_state WHERE id = 1"
        ).fetchone()
        if row is not None and offset < row[0]:
            raise ValueError("ACS snapshot offset cannot regress scanner state")

        config = conn.execute(
            "SELECT desired_revision FROM scanner_config WHERE id = 1"
        ).fetchone()
        desired_revision = config[0] if config else 0
        if expected_revision is not None and desired_revision != int(expected_revision):
            raise ValueError("Desired party selection changed during ACS bootstrap")

        desired = {
            row[0] for row in conn.execute("SELECT party FROM party_selection")
        }
        if not desired:
            selected_at = utc_now()
            conn.executemany(
                "INSERT INTO party_selection (party, selected_at) VALUES (?, ?)",
                [(party, selected_at) for party in active_parties],
            )
            conn.execute(
                "UPDATE scanner_config SET desired_revision = desired_revision + 1 WHERE id = 1"
            )
            desired_revision += 1
            desired = set(active_parties)
        if desired != set(active_parties):
            raise ValueError("ACS parties do not match the desired party selection")

        conn.execute("DELETE FROM holdings")
        for party, holdings in holdings_by_party.items():
            for holding in holdings:
                _insert_holding(conn, holding, party)
        _save_offset(conn, offset)
        conn.execute(
            """
            UPDATE tracked_parties
            SET active = 0, deactivated_at_offset = ?
            WHERE active = 1
            """,
            (offset,),
        )
        for party in active_parties:
            conn.execute(
                """
                INSERT INTO tracked_parties (
                    party, active, activated_at_offset, deactivated_at_offset
                ) VALUES (?, 1, ?, NULL)
                ON CONFLICT(party) DO UPDATE SET
                    active = 1,
                    activated_at_offset = CASE
                        WHEN tracked_parties.active = 1
                        THEN tracked_parties.activated_at_offset
                        ELSE excluded.activated_at_offset
                    END,
                    deactivated_at_offset = NULL
                """,
                (party, offset),
            )
        conn.execute(
            "UPDATE scanner_config SET active_revision = desired_revision WHERE id = 1"
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def reconcile_tracked_parties(
    holdings_by_party,
    removed_parties,
    offset,
    expected_revision,
):
    """Apply additions/removals at the saved offset in one transaction."""

    offset = int(offset)
    additions = set(holdings_by_party)
    removals = set(removed_parties)
    conn = get_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        saved = conn.execute(
            "SELECT last_offset FROM scanner_state WHERE id = 1"
        ).fetchone()
        if saved is None or saved[0] != offset:
            raise ValueError("Scanner offset changed during party reconciliation")
        revision = conn.execute(
            "SELECT desired_revision FROM scanner_config WHERE id = 1"
        ).fetchone()[0]
        if revision != int(expected_revision):
            raise ValueError("Desired party selection changed during reconciliation")

        desired = {
            row[0] for row in conn.execute("SELECT party FROM party_selection")
        }
        active = {
            row[0]
            for row in conn.execute(
                "SELECT party FROM tracked_parties WHERE active = 1"
            )
        }
        if additions != desired - active or removals != active - desired:
            raise ValueError("Reconciliation inputs do not match desired selection")

        for party in removals:
            conn.execute("DELETE FROM holdings WHERE party = ?", (party,))
            conn.execute(
                """
                UPDATE tracked_parties
                SET active = 0, deactivated_at_offset = ?
                WHERE party = ?
                """,
                (offset, party),
            )
        for party, holdings in holdings_by_party.items():
            conn.execute("DELETE FROM holdings WHERE party = ?", (party,))
            for holding in holdings:
                _insert_holding(conn, holding, party)
            conn.execute(
                """
                INSERT INTO tracked_parties (
                    party, active, activated_at_offset, deactivated_at_offset
                ) VALUES (?, 1, ?, NULL)
                ON CONFLICT(party) DO UPDATE SET
                    active = 1,
                    activated_at_offset = excluded.activated_at_offset,
                    deactivated_at_offset = NULL
                """,
                (party, offset),
            )

        conn.execute(
            "UPDATE scanner_config SET active_revision = ? WHERE id = 1",
            (revision,),
        )
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
            UNION
            SELECT party FROM tracked_parties WHERE party IS NOT NULL
            UNION
            SELECT party FROM party_catalog WHERE party IS NOT NULL
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
