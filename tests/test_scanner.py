import asyncio
import base64
import json
import importlib
import os
import tempfile
import threading
import types
import unittest
from pathlib import Path
from unittest import mock

import c8lab
import database
import scanner
import updates
import worker


ALICE = "00209eb9a1e8485ba9a7383aa6115ab2::1220alice"
BOB = "0024bd501a4e4ea2b36125d43107085b::1220bob"
ADMIN = "admin::1220admin"
CAROL = "carol::1220carol"
FIXTURE = Path(__file__).parent / "fixtures" / "private_transaction.json"


def holding(contract_id, party, amount):
    return {
        "contractId": contract_id,
        "party": party,
        "amount": amount,
        "instrument": "Amulet",
        "admin": ADMIN,
        "locked": False,
    }


def catalog_entry(party, readable=True, **overrides):
    entry = {
        "party": party,
        "display_name": party.split("::", 1)[0],
        "is_local": True,
        "can_act_as": False,
        "can_read_as": False,
        "readable": readable,
        "source": "read_any_local",
    }
    entry.update(overrides)
    return entry


class AuthenticationAndDiscoveryTests(unittest.TestCase):
    def test_authenticated_user_resolution_override_devnet_and_localnet(self):
        with mock.patch.dict(os.environ, {"C8_USER": "explicit-user"}):
            self.assertEqual(c8lab.authenticated_user_id(), "explicit-user")

        payload = base64.urlsafe_b64encode(
            json.dumps({"sub": "devnet-user"}).encode()
        ).decode().rstrip("=")
        bearer = f"header.{payload}.signature"
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(
            c8lab, "IDP", "https://identity.example"
        ), mock.patch.object(c8lab, "token", return_value=bearer):
            self.assertEqual(c8lab.authenticated_user_id(), "devnet-user")

        with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(
            c8lab, "IDP", None
        ), mock.patch.object(c8lab, "USER", "local-user"):
            self.assertEqual(c8lab.authenticated_user_id(), "local-user")

    def test_rights_parsing_includes_act_read_excludes_execute(self):
        rights = [
            {"kind": {"CanActAs": {"value": {"party": ALICE}}}},
            {"kind": {"CanReadAs": {"value": {"party": BOB}}}},
            {"kind": {"CanExecuteAs": {"value": {"party": CAROL}}}},
            {"kind": {"CanReadAsAnyParty": {"value": {}}}},
        ]
        explicit, read_any = scanner.parse_user_rights(rights)
        self.assertTrue(read_any)
        self.assertEqual(
            explicit,
            {
                ALICE: {"can_act_as": True, "can_read_as": False},
                BOB: {"can_act_as": False, "can_read_as": True},
            },
        )

    def test_read_any_discovery_pages_local_parties_and_merges_rights(self):
        rights = [
            {"kind": {"CanActAs": {"value": {"party": ALICE}}}},
            {"kind": {"CanReadAs": {"value": {"party": CAROL}}}},
            {"kind": {"CanReadAsAnyParty": {"value": {}}}},
        ]
        pages = [
            {
                "partyDetails": [
                    {"party": ALICE, "isLocal": True},
                    {"party": "remote::1220remote", "isLocal": False},
                ],
                "nextPageToken": "next",
            },
            {"partyDetails": [{"party": BOB, "isLocal": True}]},
        ]
        with mock.patch.object(
            c8lab, "authenticated_user_id", return_value="scanner-user"
        ), mock.patch.object(
            c8lab, "user_rights", return_value=rights
        ), mock.patch.object(
            c8lab, "party_page", side_effect=pages
        ) as party_page, mock.patch.object(
            scanner,
            "holdings_at_offset",
            side_effect=AssertionError("discovery must not read Holdings"),
        ):
            result = scanner.discover_authorized_parties()

        entries = {entry["party"]: entry for entry in result["entries"]}
        self.assertEqual(set(entries), {ALICE, BOB, CAROL})
        self.assertTrue(entries[ALICE]["can_act_as"])
        self.assertEqual(entries[ALICE]["source"], "explicit_right+read_any_local")
        self.assertIsNone(entries[CAROL]["is_local"])
        self.assertEqual(party_page.call_count, 2)
        self.assertEqual(party_page.call_args_list[1].kwargs["page_token"], "next")


class TemporaryScannerDatabase(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_db_name = database.DB_NAME
        database.DB_NAME = str(Path(self.temp_dir.name) / "scanner.db")
        database.create_tables()

    def tearDown(self):
        database.DB_NAME = self.original_db_name
        self.temp_dir.cleanup()

    def rows(self, query, parameters=()):
        conn = database.get_connection()
        try:
            return conn.execute(query, parameters).fetchall()
        finally:
            conn.close()


class DatabaseTests(TemporaryScannerDatabase):
    def test_existing_database_migration_preserves_holdings_and_offset(self):
        database.save_offset(9)
        database.replace_holdings_for_party(ALICE, [holding("legacy", ALICE, "1")])
        database.create_tables()
        self.assertEqual(database.get_saved_offset(), 9)
        self.assertEqual(self.rows("SELECT contract_id FROM holdings"), [("legacy",)])
        status = database.get_selection_status()
        self.assertEqual(status["desired_parties"], [ALICE])
        self.assertEqual(status["active_parties"], [ALICE])
        self.assertFalse(status["restart_required"])

    def test_holding_created_then_archived(self):
        database.save_offset(10)
        self.assertTrue(
            database.apply_holding_changes(
                [holding("holding-1", ALICE, "1.25")],
                [],
                11,
                "update-11",
            )
        )
        self.assertEqual(
            database.get_balance_for_party(ALICE),
            [{"instrument": "Amulet", "amount": "1.25"}],
        )

        self.assertTrue(
            database.apply_holding_changes(
                [],
                ["holding-1"],
                12,
                "update-12",
            )
        )
        self.assertEqual(database.get_balance_for_party(ALICE), [])
        self.assertEqual(
            [event["event_type"] for event in database.get_history_for_party(ALICE)],
            ["archived", "created"],
        )
        self.assertEqual(database.get_saved_offset(), 12)

    def test_replay_is_idempotent_and_does_not_resurrect_old_state(self):
        database.save_offset(20)
        database.apply_holding_changes(
            [holding("old", ALICE, "1")], [], 21, "update-21"
        )
        database.apply_holding_changes(
            [holding("new", ALICE, "0.5")], ["old"], 22, "update-22"
        )

        self.assertFalse(
            database.apply_holding_changes(
                [holding("old", ALICE, "1")], [], 21, "update-21"
            )
        )
        self.assertEqual(self.rows("SELECT contract_id FROM holdings"), [("new",)])
        self.assertEqual(database.get_saved_offset(), 22)
        database.save_offset(19)
        self.assertEqual(database.get_saved_offset(), 22)

    def test_offset_and_data_roll_back_together(self):
        database.save_offset(30)
        with self.assertRaises(TypeError):
            database.apply_holding_changes(
                [holding("would-be-created", ALICE, "1")],
                [],
                31,
                "update-31",
                private_events=[
                    {
                        "event_id": "bad-json",
                        "event_type": "CreatedEvent",
                        "raw": {"not-json": {1, 2}},
                    }
                ],
            )
        self.assertEqual(self.rows("SELECT contract_id FROM holdings"), [])
        self.assertEqual(database.get_saved_offset(), 30)

    def test_decimal_balance_and_prefix_resolution(self):
        database.replace_all_holdings_and_save_offset(
            {
                ALICE: [
                    holding("decimal-1", ALICE, "0.1"),
                    holding("decimal-2", ALICE, "0.2"),
                ]
            },
            40,
        )
        self.assertEqual(
            database.get_balance_for_party(ALICE),
            [{"instrument": "Amulet", "amount": "0.3"}],
        )

        database.replace_all_holdings_and_save_offset(
            {
                ALICE: [
                    holding(
                        "decimal-large",
                        ALICE,
                        "12345678901234567890.123456789012345678",
                    ),
                    holding("decimal-tiny", ALICE, "0.000000000000000001"),
                ]
            },
            41,
        )
        self.assertEqual(
            database.get_balance_for_party(ALICE),
            [
                {
                    "instrument": "Amulet",
                    "amount": "12345678901234567890.123456789012345679",
                }
            ],
        )
        self.assertEqual(database.resolve_party(ALICE.split("::")[0]), ALICE)

    def test_public_scan_schema_is_removed(self):
        conn = database.get_connection()
        try:
            conn.execute("DROP TABLE transfers")
            conn.execute(
                "CREATE TABLE transfers (migration_id INTEGER, update_id TEXT)"
            )
            conn.execute("CREATE TABLE scan_state (migration_id INTEGER)")
            conn.commit()
        finally:
            conn.close()

        database.create_tables()
        columns = {row[1] for row in self.rows("PRAGMA table_info(transfers)")}
        self.assertNotIn("migration_id", columns)
        self.assertIn("offset", columns)
        self.assertEqual(
            self.rows(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='scan_state'"
            ),
            [],
        )


class CatalogAndSelectionTests(TemporaryScannerDatabase):
    def test_catalog_replacement_is_atomic_and_failed_refresh_retains_cache(self):
        database.replace_party_catalog(
            [catalog_entry(ALICE)], "user-one", read_as_any=True
        )
        with self.assertRaises(ValueError):
            database.replace_party_catalog(
                [catalog_entry(BOB), {"display_name": "missing-party"}],
                "user-two",
                read_as_any=False,
            )
        database.record_party_catalog_error("directory timed out")
        response = database.list_parties()
        self.assertEqual([item["party"] for item in response["items"]], [ALICE])
        state = database.get_party_catalog_state()
        self.assertFalse(state["complete"])
        self.assertEqual(state["error"], "directory timed out")
        self.assertEqual(state["user_id"], "user-one")

    def test_revoked_selected_party_is_retained_as_unreadable(self):
        database.replace_party_catalog(
            [catalog_entry(ALICE), catalog_entry(BOB)], "user", True
        )
        database.set_desired_parties([ALICE])
        database.replace_party_catalog([catalog_entry(BOB)], "user", True)
        items = {
            item["party"]: item for item in database.list_parties()["items"]
        }
        self.assertFalse(items[ALICE]["readable"])
        self.assertTrue(items[ALICE]["selected"])
        self.assertTrue(items[BOB]["readable"])

    def test_first_catalog_refresh_materializes_revoked_legacy_selection(self):
        database.save_offset(10)
        database.replace_holdings_for_party(ALICE, [holding("legacy", ALICE, "1")])
        database.create_tables()
        database.replace_party_catalog([], "new-user", False)
        items = database.list_parties()["items"]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["party"], ALICE)
        self.assertTrue(items[0]["selected"])
        self.assertTrue(items[0]["active"])
        self.assertFalse(items[0]["readable"])

    def test_search_pagination_flags_and_selection_revisions(self):
        database.replace_party_catalog(
            [catalog_entry(ALICE), catalog_entry(BOB), catalog_entry(CAROL)],
            "user",
            True,
        )
        first = database.set_desired_parties([BOB, ALICE, ALICE])
        self.assertEqual(first["desired_parties"], [ALICE, BOB])
        self.assertEqual(first["desired_revision"], 1)
        same = database.set_desired_parties([ALICE, BOB])
        self.assertEqual(same["desired_revision"], 1)

        result = database.list_parties("002", limit=1, offset=1)
        self.assertEqual(result["total"], 2)
        self.assertEqual(len(result["items"]), 1)
        self.assertTrue(result["items"][0]["selected"])

    def test_selection_rejects_empty_prefix_unknown_inaccessible_and_oversized(self):
        database.replace_party_catalog(
            [catalog_entry(ALICE), catalog_entry(BOB, readable=False)],
            "user",
            False,
        )
        invalid = (
            ([], 50),
            ([ALICE.split("::")[0]], 50),
            ([CAROL], 50),
            ([BOB], 50),
            ([ALICE, BOB], 1),
        )
        for parties, maximum in invalid:
            with self.subTest(parties=parties, maximum=maximum):
                with self.assertRaises(ValueError):
                    database.set_desired_parties(parties, maximum)

    def test_reconciliation_preserves_history_and_activates_atomically(self):
        database.replace_party_catalog(
            [catalog_entry(ALICE), catalog_entry(BOB)], "user", True
        )
        initial = database.set_desired_parties([ALICE])
        database.replace_all_holdings_and_save_offset(
            {ALICE: [holding("alice-holding", ALICE, "1")]},
            100,
            expected_revision=initial["desired_revision"],
        )
        database.apply_holding_changes(
            [],
            [],
            101,
            "transfer-update",
            transfers=[
                {
                    "event_id": "transfer-event",
                    "sender": ALICE,
                    "receiver": BOB,
                    "amount": "0.5",
                    "instrument": "Amulet",
                    "choice": "TransferFactory_Transfer",
                }
            ],
        )
        pending = database.set_desired_parties([BOB])
        database.reconcile_tracked_parties(
            {BOB: [holding("bob-holding", BOB, "2")]},
            [ALICE],
            101,
            pending["desired_revision"],
        )
        self.assertEqual(self.rows("SELECT party FROM holdings"), [(BOB,)])
        self.assertEqual(len(database.get_transfers_for_party(ALICE)), 1)
        self.assertEqual(database.get_saved_offset(), 101)
        self.assertFalse(database.get_party_tracking(ALICE)["active"])
        self.assertEqual(
            database.get_party_tracking(ALICE)["deactivated_at_offset"], 101
        )
        self.assertEqual(database.get_party_tracking(BOB)["activated_at_offset"], 101)
        self.assertFalse(database.get_selection_status()["restart_required"])

    def test_revision_race_rolls_back_reconciliation(self):
        database.replace_party_catalog(
            [catalog_entry(ALICE), catalog_entry(BOB)], "user", True
        )
        initial = database.set_desired_parties([ALICE])
        database.replace_all_holdings_and_save_offset(
            {ALICE: [holding("alice-holding", ALICE, "1")]},
            100,
            expected_revision=initial["desired_revision"],
        )
        stale = database.set_desired_parties([BOB])
        database.set_desired_parties([ALICE])
        with self.assertRaises(ValueError):
            database.reconcile_tracked_parties(
                {BOB: [holding("bob-holding", BOB, "2")]},
                [ALICE],
                100,
                stale["desired_revision"],
            )
        self.assertEqual(self.rows("SELECT contract_id FROM holdings"), [("alice-holding",)])
        self.assertEqual(database.get_active_parties(), [ALICE])


class ScannerWorkflowTests(TemporaryScannerDatabase):
    def test_first_bootstrap_reads_desired_acs_at_one_ledger_offset(self):
        database.replace_party_catalog([catalog_entry(ALICE)], "user", False)
        database.set_desired_parties([ALICE])
        with mock.patch.object(c8lab, "ledger_end", return_value=500), mock.patch.object(
            scanner,
            "holdings_at_offset",
            return_value=[holding("initial", ALICE, "1")],
        ) as read_acs:
            scanner.bootstrap_or_reconcile()

        read_acs.assert_called_once_with(ALICE, 500)
        self.assertEqual(database.get_saved_offset(), 500)
        self.assertEqual(database.get_active_parties(), [ALICE])
        self.assertEqual(self.rows("SELECT contract_id FROM holdings"), [("initial",)])

    def test_acs_denial_or_pruning_leaves_prior_active_selection_intact(self):
        database.replace_party_catalog(
            [catalog_entry(ALICE), catalog_entry(BOB)], "user", True
        )
        initial = database.set_desired_parties([ALICE])
        database.replace_all_holdings_and_save_offset(
            {ALICE: [holding("existing", ALICE, "1")]},
            600,
            expected_revision=initial["desired_revision"],
        )
        database.set_desired_parties([BOB])

        for message in ("HTTP 403 permission denied", "activeAtOffset was pruned"):
            with self.subTest(message=message), mock.patch.object(
                scanner,
                "holdings_at_offset",
                side_effect=c8lab.LabError(message),
            ):
                with self.assertRaises(c8lab.LabError):
                    scanner.bootstrap_or_reconcile()
            self.assertEqual(database.get_saved_offset(), 600)
            self.assertEqual(database.get_active_parties(), [ALICE])
            self.assertEqual(
                self.rows("SELECT contract_id FROM holdings"), [("existing",)]
            )
            self.assertTrue(database.get_selection_status()["restart_required"])


class RuntimeWorkerTests(TemporaryScannerDatabase):
    def activate(self, party=ALICE, offset=800):
        database.replace_party_catalog(
            [catalog_entry(ALICE), catalog_entry(BOB)],
            "worker-user",
            True,
        )
        selected = database.set_desired_parties([party])
        database.replace_all_holdings_and_save_offset(
            {party: []},
            offset,
            expected_revision=selected["desired_revision"],
        )

    def test_runtime_state_is_persisted_and_errors_are_bounded(self):
        self.assertEqual(database.get_scanner_runtime()["status"], "stopped")
        database.update_scanner_runtime("starting", worker_pid=1234)
        database.update_scanner_runtime("retrying", error=("failure\n" * 200))
        retrying = database.get_scanner_runtime()
        self.assertEqual(retrying["status"], "retrying")
        self.assertEqual(retrying["worker_pid"], 1234)
        self.assertLessEqual(len(retrying["last_error"]), 500)
        self.assertNotIn("\n", retrying["last_error"])

        database.update_scanner_runtime("connected")
        connected = database.get_scanner_runtime()
        self.assertIsNotNone(connected["connected_at"])
        self.assertIsNotNone(connected["last_heartbeat"])
        database.touch_scanner_runtime()
        self.assertIsNotNone(database.get_scanner_runtime()["last_heartbeat"])

        database.update_scanner_runtime("stopped")
        stopped = database.get_scanner_runtime()
        self.assertEqual(stopped["status"], "stopped")
        self.assertIsNone(stopped["worker_pid"])
        with self.assertRaises(ValueError):
            database.update_scanner_runtime("unknown")

    def test_quiet_stream_detects_pending_selection_within_timeout(self):
        self.activate()

        class StreamTimeout(Exception):
            pass

        class FakeSocket:
            def __init__(self):
                self.timeout = None
                self.closed = False
                self.request = None

            def settimeout(self, timeout):
                self.timeout = timeout

            def send(self, request):
                self.request = json.loads(request)

            def recv(self):
                database.set_desired_parties([BOB])
                raise StreamTimeout()

            def close(self):
                self.closed = True

        socket = FakeSocket()
        websocket_module = types.SimpleNamespace(
            WebSocketTimeoutException=StreamTimeout,
            create_connection=lambda *args, **kwargs: socket,
        )
        with mock.patch.object(updates, "websocket", websocket_module), mock.patch.object(
            c8lab,
            "token",
            return_value="test-token",
        ):
            with self.assertRaises(updates.SelectionChangePending):
                updates.run_stream(selection_check_interval=0.1)

        self.assertEqual(socket.timeout, 0.1)
        self.assertTrue(socket.closed)
        self.assertNotIn(
            "filtersForAnyParty",
            socket.request["updateFormat"]["includeTransactions"]["eventFormat"],
        )
        self.assertEqual(database.get_scanner_runtime()["status"], "connected")
        self.assertTrue(database.get_selection_status()["restart_required"])

    def test_local_worker_reconciles_then_reconnects_automatically(self):
        self.activate()
        stop_event = threading.Event()
        bootstrap_calls = []
        stream_calls = []

        def reconcile():
            status = database.get_selection_status()
            bootstrap_calls.append(status["desired_revision"])
            if status["restart_required"]:
                database.reconcile_tracked_parties(
                    {BOB: []},
                    [ALICE],
                    database.get_saved_offset(),
                    status["desired_revision"],
                )

        def stream(stop_requested=None):
            stream_calls.append(database.get_active_parties())
            if len(stream_calls) == 1:
                database.set_desired_parties([BOB])
                raise updates.SelectionChangePending("selection changed")
            self.assertFalse(stop_requested())
            stop_event.set()

        with mock.patch.object(worker.scanner, "bootstrap_or_reconcile", reconcile), mock.patch.object(
            worker.updates,
            "run_stream",
            stream,
        ):
            worker.run_worker(stop_event, initial_backoff=0, maximum_backoff=0)

        self.assertEqual(stream_calls, [[ALICE], [BOB]])
        self.assertGreaterEqual(len(bootstrap_calls), 2)
        self.assertFalse(database.get_selection_status()["restart_required"])
        self.assertEqual(database.get_scanner_runtime()["status"], "stopped")

    def test_fastapi_supervisor_restarts_and_stops_child_cleanly(self):
        api = importlib.import_module("api")

        class FakeProcess:
            def __init__(self, immediate=False):
                self.immediate = immediate
                self.returncode = None
                self.done = asyncio.Event()
                self.terminated = False
                self.killed = False

            async def wait(self):
                if self.immediate:
                    self.returncode = 7
                    return 7
                await self.done.wait()
                self.returncode = 0
                return 0

            def terminate(self):
                self.terminated = True
                self.done.set()

            def kill(self):
                self.killed = True
                self.done.set()

        async def exercise():
            stop_event = asyncio.Event()
            processes = []

            async def spawn():
                process = FakeProcess(immediate=not processes)
                processes.append(process)
                if len(processes) == 2:
                    stop_event.set()
                return process

            await api.supervise_local_worker(
                stop_event,
                spawn=spawn,
                initial_backoff=0,
                maximum_backoff=0,
            )
            return processes

        processes = asyncio.run(exercise())
        self.assertEqual(len(processes), 2)
        self.assertEqual(processes[0].returncode, 7)
        self.assertTrue(processes[1].terminated)
        self.assertFalse(processes[1].killed)
        self.assertEqual(database.get_scanner_runtime()["status"], "stopped")


class UpdateParsingTests(TemporaryScannerDatabase):
    def load_fixture(self):
        return json.loads(FIXTURE.read_text())

    def test_holding_and_transfer_extraction_from_private_transaction(self):
        batch = updates.parse_transaction(self.load_fixture())
        self.assertEqual(batch["offset"], 101)
        self.assertEqual(
            {item["contractId"] for item in batch["created_holdings"]},
            {"holding-change", "holding-receiver"},
        )
        self.assertEqual(batch["archived_contract_ids"], ["holding-old"])
        self.assertEqual(len(batch["private_events"]), 4)
        self.assertEqual(
            batch["transfers"],
            [
                {
                    "event_id": "event-transfer",
                    "sender": ALICE,
                    "receiver": BOB,
                    "amount": "0.3",
                    "instrument": "Amulet",
                    "choice": "TransferFactory_Transfer",
                }
            ],
        )

    def test_explicit_archived_event_is_supported(self):
        batch = updates.parse_transaction(
            {
                "offset": 50,
                "updateId": "archive-update",
                "events": [
                    {
                        "ArchivedEvent": {
                            "value": {
                                "eventId": "archive-event",
                                "contractId": "holding-archived",
                            }
                        }
                    }
                ],
            }
        )
        self.assertEqual(batch["archived_contract_ids"], ["holding-archived"])

    def test_non_transfer_exercise_is_not_semantic_history(self):
        payload = {
            "choice": "AssetTransfer_Maybe",
            "choiceArgument": {
                "sender": ALICE,
                "receiver": BOB,
                "amount": "99",
            },
        }
        self.assertIsNone(updates.transfer_from_exercised_event(payload, "event-x"))

    def test_fixture_persists_holdings_events_transfer_and_offset_atomically(self):
        database.replace_all_holdings_and_save_offset(
            {ALICE: [holding("holding-old", ALICE, "1.0")]},
            100,
        )
        self.assertTrue(updates.process_transaction(self.load_fixture()))
        self.assertEqual(
            set(self.rows("SELECT contract_id FROM holdings")),
            {("holding-change",), ("holding-receiver",)},
        )
        self.assertEqual(len(database.get_recent_private_events()), 4)
        self.assertEqual(len(database.get_transfers_for_party(ALICE)), 1)
        self.assertEqual(database.get_saved_offset(), 101)

    def test_tree_map_event_ids_are_stable_and_children_are_walked(self):
        transaction = {
            "offset": 60,
            "rootEventIds": ["root"],
            "eventsById": {
                "root": {
                    "ExercisedEvent": {
                        "value": {
                            "choice": "OtherChoice",
                            "childEventIds": ["child"],
                            "consuming": False,
                        }
                    }
                },
                "child": {
                    "ArchivedEvent": {
                        "value": {"contractId": "tree-holding"}
                    }
                },
            },
        }
        batch = updates.parse_transaction(transaction)
        self.assertEqual(
            [event["event_id"] for event in batch["private_events"]],
            ["root", "child"],
        )
        self.assertEqual(batch["archived_contract_ids"], ["tree-holding"])

    def test_resume_request_is_party_scoped_and_uses_both_filters(self):
        filters = updates.build_filters_for_parties([ALICE, BOB])
        request = updates.build_update_request(77, filters)
        self.assertEqual(request["beginExclusive"], 77)
        include = request["updateFormat"]["includeTransactions"]
        self.assertEqual(
            include["transactionShape"],
            "TRANSACTION_SHAPE_LEDGER_EFFECTS",
        )
        self.assertNotIn("filtersForAnyParty", include["eventFormat"])
        cumulative = include["eventFormat"]["filtersByParty"][ALICE]["cumulative"]
        self.assertIn("InterfaceFilter", cumulative[0]["identifierFilter"])
        self.assertIn("WildcardFilter", cumulative[1]["identifierFilter"])

    def test_live_filters_use_active_parties_and_refuse_pending_selection(self):
        database.replace_party_catalog(
            [catalog_entry(ALICE), catalog_entry(BOB)], "user", True
        )
        initial = database.set_desired_parties([ALICE])
        database.replace_all_holdings_and_save_offset(
            {ALICE: []}, 70, expected_revision=initial["desired_revision"]
        )
        self.assertEqual(set(updates.build_filters()), {ALICE})
        database.set_desired_parties([BOB])
        with self.assertRaises(updates.SelectionChangePending):
            updates.build_filters()

    def test_checkpoint_does_not_regress_resume_offset(self):
        database.save_offset(90)
        updates.process_message(
            {"update": {"OffsetCheckpoint": {"value": {"offset": 89}}}}
        )
        self.assertEqual(database.get_saved_offset(), 90)


class ApiTests(TemporaryScannerDatabase):
    def test_frontend_shell_and_assets_are_served_without_hiding_api_routes(self):
        api = importlib.import_module("api")
        response = api.frontend()
        index_path = Path(response.path)
        html = index_path.read_text()
        styles = (index_path.parent / "styles.css").read_text()
        javascript = (index_path.parent / "app.js").read_text()

        self.assertEqual(response.media_type, "text/html")
        self.assertIn('id="dashboard"', html)
        self.assertIn('href="/assets/styles.css"', html)
        self.assertIn('src="/assets/app.js"', html)
        self.assertIn('id="admin-dialog"', html)
        self.assertIn("@media (max-width: 600px)", styles)
        self.assertIn("prefers-reduced-motion", styles)
        self.assertIn("showModal", javascript)
        self.assertIn('requestJson("/health"', javascript)
        self.assertIn('addEventListener("visibilitychange"', javascript)
        self.assertIn("RequestTimeoutError", javascript)

        route_paths = {route.path for route in api.app.routes}
        self.assertTrue(
            {
                "/",
                "/assets",
                "/health",
                "/parties",
                "/parties/selection",
                "/balance/{party}",
                "/history/{party}",
                "/docs",
            }.issubset(route_paths)
        )

    def test_balance_health_and_semantic_history(self):
        database.replace_all_holdings_and_save_offset(
            {ALICE: [holding("api-holding", ALICE, "1.1")]},
            200,
        )
        database.apply_holding_changes(
            [],
            [],
            201,
            "api-update",
            transfers=[
                {
                    "event_id": "api-transfer",
                    "sender": ALICE,
                    "receiver": BOB,
                    "amount": "0.4",
                    "instrument": "Amulet",
                    "choice": "TransferFactory_Transfer",
                }
            ],
        )
        api = importlib.import_module("api")

        health = api.health()
        self.assertEqual(health["status"], "ok")
        self.assertEqual(health["last_offset"], 201)
        self.assertEqual(health["active_party_count"], 1)
        self.assertEqual(health["desired_party_count"], 1)
        self.assertFalse(health["restart_required"])
        self.assertEqual(health["stream"]["status"], "stopped")
        self.assertFalse(health["stream"]["enabled"])
        self.assertEqual(
            api.balance(ALICE)["balances"],
            [{"instrument": "Amulet", "amount": "1.1"}],
        )
        response = api.history(ALICE, limit=10)
        self.assertEqual(response["count"], 1)
        self.assertEqual(response["transfers"][0]["direction"], "sent")
        self.assertEqual(response["transfers"][0]["counterparty"], BOB)
        received = api.history(BOB, limit=10)["transfers"][0]
        self.assertEqual(received["direction"], "received")
        self.assertEqual(received["counterparty"], ALICE)

    def test_semantic_history_pagination_and_total_preserve_perspective(self):
        transfers = [
            (701, "sent", ALICE, BOB, "1.000000000000000001"),
            (702, "received", BOB, ALICE, "2.5"),
            (703, "self", ALICE, ALICE, "0.000000000000000001"),
        ]
        for offset, label, sender, receiver, amount in transfers:
            database.apply_holding_changes(
                [],
                [],
                offset,
                f"history-{label}",
                record_time=f"2026-08-29T18:0{offset - 701}:00Z",
                transfers=[
                    {
                        "event_id": f"event-{label}",
                        "sender": sender,
                        "receiver": receiver,
                        "amount": amount,
                        "instrument": "Amulet",
                        "choice": "TransferFactory_Transfer",
                    }
                ],
            )

        api = importlib.import_module("api")
        first_page = api.history(ALICE, limit=2, offset=0)
        self.assertEqual(first_page["count"], 2)
        self.assertEqual(first_page["total"], 3)
        self.assertEqual(first_page["limit"], 2)
        self.assertEqual(first_page["offset"], 0)
        self.assertEqual(
            [transfer["direction"] for transfer in first_page["transfers"]],
            ["self", "received"],
        )

        second_page = api.history(ALICE, limit=2, offset=2)
        self.assertEqual(second_page["count"], 1)
        self.assertEqual(second_page["total"], 3)
        self.assertEqual(second_page["offset"], 2)
        self.assertEqual(second_page["transfers"][0]["direction"], "sent")
        self.assertEqual(
            second_page["transfers"][0]["amount"],
            "1.000000000000000001",
        )

    def test_cached_party_api_and_persisted_selection(self):
        database.replace_party_catalog(
            [catalog_entry(ALICE), catalog_entry(BOB)], "api-user", True
        )
        initial = database.set_desired_parties([ALICE])
        database.replace_all_holdings_and_save_offset(
            {ALICE: []}, 300, expected_revision=initial["desired_revision"]
        )
        api = importlib.import_module("api")

        response = api.parties(q="002", limit=50, offset=0)
        self.assertEqual(response["total"], 2)
        flags = {item["party"]: item for item in response["items"]}
        self.assertTrue(flags[ALICE]["selected"])
        self.assertTrue(flags[ALICE]["active"])
        self.assertFalse(response["restart_required"])
        self.assertEqual(response["catalog"]["user_id"], "api-user")

        with mock.patch.object(api, "ADMIN_TOKEN", "test-admin-token"):
            public_selection = api.party_selection()
            self.assertEqual(public_selection["desired_parties"], [ALICE])
            self.assertEqual(public_selection["active_parties"], [ALICE])
            self.assertTrue(public_selection["selection_management_enabled"])
            self.assertEqual(public_selection["max_parties"], 50)

            selection = api.update_party_selection(
                api.PartySelectionRequest(parties=[BOB]),
                authorization="Bearer test-admin-token",
            )
        self.assertEqual(selection["desired_parties"], [BOB])
        self.assertEqual(selection["active_parties"], [ALICE])
        self.assertTrue(selection["restart_required"])

        with self.assertRaises(Exception) as raised:
            api.balance(BOB)
        self.assertEqual(raised.exception.status_code, 409)

    def test_selection_mutation_is_disabled_or_rejects_invalid_admin_tokens(self):
        database.replace_party_catalog([catalog_entry(ALICE)], "api-user", False)
        api = importlib.import_module("api")
        request = api.PartySelectionRequest(parties=[ALICE])

        with mock.patch.object(api, "ADMIN_TOKEN", None):
            self.assertFalse(api.party_selection()["selection_management_enabled"])
            with self.assertRaises(Exception) as disabled:
                api.update_party_selection(request, authorization=None)
            self.assertEqual(disabled.exception.status_code, 503)

        with mock.patch.object(api, "ADMIN_TOKEN", "correct-token"):
            for authorization in (None, "Basic correct-token", "Bearer wrong-token"):
                with self.subTest(authorization=authorization):
                    with self.assertRaises(Exception) as forbidden:
                        api.update_party_selection(
                            request,
                            authorization=authorization,
                        )
                    self.assertEqual(forbidden.exception.status_code, 403)

            response = api.update_party_selection(
                request,
                authorization="Bearer correct-token",
            )
            self.assertEqual(response["desired_parties"], [ALICE])


if __name__ == "__main__":
    unittest.main()
