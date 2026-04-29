import json
import unittest
from datetime import datetime, timezone
from pathlib import Path

import windrose_monitor as wm


class StubParser:
    def get_server_state(self):
        return (
            datetime(2026, 4, 29, 12, 0, tzinfo=timezone.utc),
            {
                "server_name": "Test Server",
                "max_players": 10,
                "password_protected": False,
                "world_id": "world-123",
                "deployment_id": "deploy-123",
            },
        )


class PublicApiTests(unittest.TestCase):
    def test_api_redacts_private_player_identifiers(self):
        roster = wm.Roster()
        known = wm.KnownPlayers()
        roster.apply(
            [
                {
                    "name": "Alice",
                    "account_id": "private-account",
                    "session_id": "private-session",
                    "state": "Connected",
                    "time_in_game": "+00:15:00.000",
                    "connected_in": "+00:00:02.000",
                }
            ],
            datetime(2026, 4, 29, 12, 1, tzinfo=timezone.utc),
        )
        known.update_from_disconnected(
            [
                {
                    "name": "Bob",
                    "account_id": "old-account",
                    "session_id": "old-session",
                    "state": "Disconnected",
                    "time_in_game": "+00:20:00.000",
                    "farewell_reason": "Closed",
                    "left_at": datetime(2026, 4, 29, 11, 45, tzinfo=timezone.utc),
                }
            ]
        )

        payload = wm.build_api_payload(roster, known, StubParser())
        json.dumps(payload)

        self.assertEqual(payload["online"][0]["name"], "Alice")
        self.assertEqual(payload["recently_seen"][0]["name"], "Bob")
        for row in payload["online"] + payload["recently_seen"]:
            self.assertNotIn("account_id", row)
            self.assertNotIn("session_id", row)

    def test_api_route_requires_exact_path(self):
        self.assertTrue(wm.is_api_players_path("/api/players"))
        self.assertTrue(wm.is_api_players_path("/api/players?pretty=1"))
        self.assertFalse(wm.is_api_players_path("/api/players-extra"))

    def test_root_serves_static_dashboard_that_fetches_api(self):
        body = wm.load_dashboard_html()

        self.assertTrue(wm.is_dashboard_path("/"))
        self.assertTrue(wm.is_dashboard_path("/index.html"))
        self.assertIn("fetch('/api/players'", body)
        self.assertIn('id="online-body"', body)
        self.assertTrue((Path(__file__).resolve().parent.parent / "index.html").exists())


class KnownPlayersTests(unittest.TestCase):
    def test_missing_left_at_does_not_overwrite_existing_timestamp(self):
        known = wm.KnownPlayers()
        original = datetime(2026, 4, 29, 11, 45, tzinfo=timezone.utc)
        known.update_from_disconnected(
            [
                {
                    "name": "Bob",
                    "account_id": "account-1",
                    "session_id": "session-1",
                    "state": "Disconnected",
                    "time_in_game": "+00:20:00.000",
                    "left_at": original,
                }
            ]
        )

        changed = known.update_from_disconnected(
            [
                {
                    "name": "Bob",
                    "account_id": "account-1",
                    "session_id": "session-2",
                    "state": "Disconnected",
                    "time_in_game": "+00:25:00.000",
                    "left_at": None,
                }
            ]
        )

        rows = known.snapshot()
        self.assertFalse(changed)
        self.assertEqual(rows[0]["left_at"], original)
        self.assertEqual(rows[0]["session_id"], "session-1")


if __name__ == "__main__":
    unittest.main()
