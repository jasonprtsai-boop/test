import unittest

from tmchess.workflow_journal import WorkflowJournal


class WorkflowJournalTests(unittest.TestCase):
    def test_reset_preserves_compatibility_list_identity(self):
        journal = WorkflowJournal()
        move_history = journal.move_history
        journal.move_history.append({"uci": "a0a1"})

        journal.reset()

        self.assertIs(journal.move_history, move_history)
        self.assertEqual(move_history, [])

    def test_restore_filters_invalid_entries_and_copies_engine_details(self):
        source = {
            "move_history": [{"uci": "a0a1"}, "invalid"],
            "engine_history": [{"lines": [{"pv": ["a0a1"]}]}],
        }
        journal = WorkflowJournal()

        journal.restore(source)
        source["engine_history"][0]["lines"][0]["pv"].append("a1a2")

        self.assertEqual(journal.move_history, [{"uci": "a0a1"}])
        self.assertEqual(journal.engine_history[0]["lines"][0]["pv"], ["a0a1"])

    def test_append_state_history_deduplicates_move_and_event(self):
        journal = WorkflowJournal()
        state = {
            "phase": "PLAYER_TURN",
            "current_turn": "player",
            "message": "輪到玩家",
            "last_move": {
                "committed_at": "2026-11-05T00:00:00+00:00",
                "actor": "engine",
                "uci": "a0a1",
                "source": [0, 0],
                "destination": [0, 1],
            },
            "vision": {"capture_id": "capture-1"},
            "robot": {"motion_may_continue": False},
            "error": None,
        }

        journal.append_state_history(state, "2026-11-05T00:00:01+00:00", player_side="w", ai_side="b")
        journal.append_state_history(state, "2026-11-05T00:00:02+00:00", player_side="w", ai_side="b")

        self.assertEqual(len(journal.move_history), 1)
        self.assertEqual(len(journal.event_history), 1)
        self.assertEqual(journal.move_history[0]["source_text"], "a0")
        self.assertEqual(journal.move_history[0]["side"], "b")


if __name__ == "__main__":
    unittest.main()
