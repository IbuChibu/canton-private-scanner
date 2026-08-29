import json
import importlib
import tempfile
import unittest
from pathlib import Path

import database
import updates


ALICE = "00209eb9a1e8485ba9a7383aa6115ab2::1220alice"
BOB = "0024bd501a4e4ea2b36125d43107085b::1220bob"
ADMIN = "admin::1220admin"
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

    def test_checkpoint_does_not_regress_resume_offset(self):
        database.save_offset(90)
        updates.process_message(
            {"update": {"OffsetCheckpoint": {"value": {"offset": 89}}}}
        )
        self.assertEqual(database.get_saved_offset(), 90)


class ApiTests(TemporaryScannerDatabase):
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

        self.assertEqual(api.health(), {"status": "ok", "last_offset": 201})
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


if __name__ == "__main__":
    unittest.main()
