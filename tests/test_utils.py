"""Regression tests for L4D2 status player rows."""

import unittest

from core.utils import format_status_message, parse_status_output


class StatusPlayerTests(unittest.TestCase):
    """Cover server and client variants of the status player table."""

    def test_l4d2_slots_and_bots(self):
        """Parse human rows with slot numbers without counting survivor bots."""
        raw = "\n".join(
            [
                'players : 2 humans, 0 bots (8 max) (not hibernating)',
                '# userid name uniqueid connected ping loss state rate adr',
                '#250 1 "Player One" STEAM_1:0:123 11:02 92 0 '
                'active 25000 192.0.2.1:27005',
                '# 251 2 "中文玩家" STEAM_1:1:456 00:12 45 1 '
                'active 30000 192.0.2.2:27005',
                '# 4 "Coach" BOT active',
                '# 5 "Ellis" BOT active',
                '#end',
            ]
        )

        status = parse_status_output(raw)

        self.assertEqual(status.player_count, 2)
        self.assertEqual(status.max_players, 8)
        self.assertEqual(len(status.players), 2)
        player = status.players[0]
        self.assertEqual(player.userid, "250")
        self.assertEqual(player.name, "Player One")
        self.assertEqual(player.uniqueid, "STEAM_1:0:123")
        self.assertEqual(player.ping, "92")
        self.assertEqual(player.loss, "0")
        self.assertEqual(player.state, "active")
        self.assertEqual(player.rate, "25000")
        self.assertEqual(player.address, "192.0.2.1:27005")
        self.assertEqual(status.players[1].name, "中文玩家")
        self.assertIn("Player One", format_status_message(status, "test"))

    def test_optional_columns_and_legacy_rows(self):
        """Accept omitted network columns and rows without a slot number."""
        rows = [
            ('# 12 1 "Player" STEAM_1:0:123 00:28 86 0 active 30000', "30000", ""),
            ('# 12 "Player" [U:1:246] 00:28 86 0 active', "", ""),
            (
                '# 12 "Player" STEAM_1:0:123 00:28 86 0 '
                'active 30000 192.0.2.1:27005',
                "30000",
                "192.0.2.1:27005",
            ),
        ]
        for row, rate, address in rows:
            with self.subTest(row=row):
                status = parse_status_output(row)
                self.assertEqual(len(status.players), 1)
                player = status.players[0]
                self.assertEqual(player.userid, "12")
                self.assertEqual(player.name, "Player")
                self.assertEqual(player.ping, "86")
                self.assertEqual(player.loss, "0")
                self.assertEqual(player.state, "active")
                self.assertEqual(player.rate, rate)
                self.assertEqual(player.address, address)

    def test_empty_and_quoted_names(self):
        """Preserve names containing quotes or no characters."""
        for name in ["", 'A "quoted" player', r"A\B"]:
            with self.subTest(name=name):
                row = f'# 12 1 "{name}" STEAM_1:0:123 00:28 86 0 active 30000'
                status = parse_status_output(row)
                self.assertEqual(len(status.players), 1)
                self.assertEqual(status.players[0].name, name)


if __name__ == "__main__":
    unittest.main()
