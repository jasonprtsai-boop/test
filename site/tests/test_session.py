import tempfile
import threading
import unittest
from pathlib import Path

from tmchess.board import standard_starting_board
from tmchess.config import SITE_ROOT
from tmchess.session import GameSessionStore, SessionCorruptionError


class GameSessionStoreTests(unittest.TestCase):
    def test_latest_unfinished_game_can_be_loaded(self):
        (SITE_ROOT / "runtime").mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=SITE_ROOT / "runtime") as directory:
            store = GameSessionStore(Path(directory))
            game_id = "20261012T010203Z-12345678"
            store.create(game_id)
            payload = {
                "schema_version": 1,
                "game_id": game_id,
                "saved_at": "2026-10-12T01:02:03+00:00",
                "phase": "PLAYER_TURN",
                "current_turn": "player",
                "first_actor": "player",
                "board": standard_starting_board(),
            }

            store.save(game_id, payload)

            self.assertEqual(store.load_latest(), payload)

    def test_finished_game_is_not_offered_for_resume(self):
        (SITE_ROOT / "runtime").mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=SITE_ROOT / "runtime") as directory:
            store = GameSessionStore(Path(directory))
            game_id = "20261012T010203Z-12345678"
            store.create(game_id)
            store.save(game_id, {
                "schema_version": 1,
                "game_id": game_id,
                "saved_at": "2026-10-12T01:02:03+00:00",
                "phase": "FINISHED",
                "board": standard_starting_board(),
            })

            self.assertIsNone(store.load_latest())

    def test_finished_latest_game_does_not_fall_back_to_older_game(self):
        (SITE_ROOT / "runtime").mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=SITE_ROOT / "runtime") as directory:
            store = GameSessionStore(Path(directory))
            older = "20261012T010203Z-12345678"
            latest = "20261012T020304Z-87654321"
            for game_id, phase in ((older, "PLAYER_TURN"), (latest, "FINISHED")):
                store.create(game_id)
                store.save(game_id, {
                    "schema_version": 1,
                    "game_id": game_id,
                    "saved_at": game_id,
                    "phase": phase,
                    "board": standard_starting_board(),
                })

            self.assertIsNone(store.load_latest())

    def test_motion_unknown_snapshot_is_loaded_even_before_initial_board(self):
        (SITE_ROOT / "runtime").mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=SITE_ROOT / "runtime") as temporary:
            store = GameSessionStore(Path(temporary))
            game_id = "20261012T010203Z-12345678"
            store.create(game_id)
            payload = {
                "schema_version": 1,
                "game_id": game_id,
                "phase": "INITIAL_CAPTURE",
                "board": None,
                "motion_state_uncertain": True,
            }
            store.save(game_id, payload)

            self.assertEqual(store.load_latest(), payload)

    def test_corrupt_latest_snapshot_falls_back_to_backup_with_motion_lock(self):
        (SITE_ROOT / "runtime").mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=SITE_ROOT / "runtime") as temporary:
            store = GameSessionStore(Path(temporary))
            game_id = "20261012T010203Z-12345678"
            store.create(game_id)
            first = {
                "schema_version": 3,
                "game_id": game_id,
                "phase": "PLAYER_TURN",
                "board": standard_starting_board(),
                "motion_state_uncertain": False,
                "marker": "backup",
            }
            store.save(game_id, first)
            store.save(game_id, {**first, "marker": "latest"})
            (store.game_directory(game_id) / "game.json").write_text("{broken", encoding="utf-8")

            recovered = store.load_latest()

            self.assertEqual(recovered["marker"], "backup")
            self.assertTrue(recovered["motion_state_uncertain"])
            self.assertIn("損壞", recovered["recovery_warning"])

    def test_corrupt_latest_snapshot_without_valid_backup_fails_closed(self):
        (SITE_ROOT / "runtime").mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=SITE_ROOT / "runtime") as temporary:
            store = GameSessionStore(Path(temporary))
            game_id = "20261012T010203Z-12345678"
            store.create(game_id)
            (store.game_directory(game_id) / "game.json").write_text("{broken", encoding="utf-8")

            with self.assertRaises(SessionCorruptionError) as caught:
                store.load_latest()

            self.assertEqual(caught.exception.game_id, game_id)

    def test_schema_three_requires_explicit_boolean_motion_state(self):
        (SITE_ROOT / "runtime").mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=SITE_ROOT / "runtime") as temporary:
            store = GameSessionStore(Path(temporary))
            game_id = "20261012T010203Z-12345678"
            store.create(game_id)
            directory = store.game_directory(game_id)
            (directory / "game.json").write_text(
                '{"schema_version":3,"game_id":"20261012T010203Z-12345678",'
                '"phase":"ROBOT_MOVING","board":null}',
                encoding="utf-8",
            )

            with self.assertRaises(SessionCorruptionError):
                store.load_latest()

    def test_legacy_unsafe_phase_without_motion_flag_is_locked(self):
        (SITE_ROOT / "runtime").mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=SITE_ROOT / "runtime") as temporary:
            store = GameSessionStore(Path(temporary))
            game_id = "20261012T010203Z-12345678"
            store.create(game_id)
            store.save(game_id, {
                "schema_version": 1,
                "game_id": game_id,
                "phase": "ROBOT_MOVING",
                "board": standard_starting_board(),
            })

            recovered = store.load_latest()

            self.assertTrue(recovered["motion_state_uncertain"])
            self.assertIn("舊版", recovered["recovery_warning"])

    def test_concurrent_saves_keep_latest_file_valid_and_leave_no_temporaries(self):
        (SITE_ROOT / "runtime").mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=SITE_ROOT / "runtime") as temporary:
            store = GameSessionStore(Path(temporary))
            game_id = "20261012T010203Z-12345678"
            store.create(game_id)
            errors = []

            def writer(index):
                try:
                    for sequence in range(15):
                        store.save(game_id, {
                            "schema_version": 3,
                            "game_id": game_id,
                            "phase": "PLAYER_TURN",
                            "board": standard_starting_board(),
                            "motion_state_uncertain": False,
                            "writer": index,
                            "sequence": sequence,
                        })
                except Exception as exc:
                    errors.append(exc)

            threads = [threading.Thread(target=writer, args=(index,)) for index in range(6)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=5)

            self.assertFalse(errors)
            self.assertTrue(all(not thread.is_alive() for thread in threads))
            self.assertIsNotNone(store.load_latest())
            self.assertEqual(
                list(store.game_directory(game_id).glob(".*.tmp")),
                [],
            )


if __name__ == "__main__":
    unittest.main()
