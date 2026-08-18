from __future__ import annotations

import copy
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock

from src.ptt_keyword_notifier import (
    Article,
    ConfigError,
    DiscordError,
    is_quiet_hours,
    load_state,
    matches_target,
    parse_articles,
    process_matches,
    validate_config,
    write_state_atomic,
)


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"
CONFIG = validate_config(json.loads((ROOT / "config" / "monitor.json").read_text(encoding="utf-8")))
NOW = datetime.fromisoformat("2026-08-18T10:00:00+08:00")


class ParserAndRuleTests(unittest.TestCase):
    def test_match_fixture(self) -> None:
        document = (FIXTURES / "gamesale_index_match.html").read_text(encoding="utf-8")
        articles = parse_articles(document)
        self.assertEqual(len(articles), 3)
        self.assertEqual(articles[0].id, "/bbs/Gamesale/M.1789000000.A.001.html")
        self.assertTrue(matches_target(articles[0].title))
        self.assertTrue(matches_target("[NS2] 售 Dynasty Warriors: Origins"))

    def test_non_targets_are_rejected(self) -> None:
        document = (FIXTURES / "gamesale_index_no_match.html").read_text(encoding="utf-8")
        self.assertEqual([a for a in parse_articles(document) if matches_target(a.title)], [])
        self.assertFalse(matches_target("[NS2] 徵 真三國無雙 起源"))
        self.assertFalse(matches_target("[NS] 售 真三國無雙 起源"))


class QuietHoursTests(unittest.TestCase):
    def test_boundaries(self) -> None:
        cases = {
            "00:59": False,
            "01:00": True,
            "08:29": True,
            "08:30": False,
            "23:59": False,
        }
        for value, expected in cases.items():
            now = datetime.fromisoformat(f"2026-08-18T{value}:00+08:00")
            self.assertEqual(is_quiet_hours(now, CONFIG), expected, value)

    def test_pending_flushes_after_quiet_hours(self) -> None:
        initial = {"schema_version": 1, "notified": [], "pending": []}
        article = Article(
            "/bbs/Gamesale/M.1.A.AAA.html",
            "[NS2] 售 真三國無雙 起源",
            "https://www.ptt.cc/bbs/Gamesale/M.1.A.AAA.html",
        )
        send = Mock()
        quiet_now = datetime.fromisoformat("2026-08-18T01:05:00+08:00")
        state, pending, sent = process_matches(initial, [article], quiet_now, CONFIG, "normal", send)
        self.assertEqual(len(pending), 1)
        self.assertEqual(sent, [])
        send.reset_mock()
        morning = datetime.fromisoformat("2026-08-18T08:30:00+08:00")
        state, pending, sent = process_matches(state, [], morning, CONFIG, "normal", send)
        send.assert_called_once()
        self.assertEqual(len(sent), 1)
        self.assertEqual(state["pending"], [])
        self.assertEqual(state["notified"], [article.id])


class StateAndNotificationTests(unittest.TestCase):
    def test_existing_notified_id_is_not_sent_twice(self) -> None:
        article = Article(
            "/bbs/Gamesale/M.1.A.AAA.html",
            "[NS2] 售 真三國無雙 起源",
            "https://www.ptt.cc/bbs/Gamesale/M.1.A.AAA.html",
        )
        state = {"schema_version": 1, "notified": [article.id], "pending": []}
        send = Mock()
        new_state, pending, sent = process_matches(state, [article], NOW, CONFIG, "normal", send)
        send.assert_not_called()
        self.assertEqual(new_state, state)
        self.assertEqual(sent, [])

    def test_discord_failure_stays_pending(self) -> None:
        article = Article(
            "/bbs/Gamesale/M.2.A.BBB.html",
            "[NS2] 售 真三國無雙 起源",
            "https://www.ptt.cc/bbs/Gamesale/M.2.A.BBB.html",
        )
        state = {"schema_version": 1, "notified": [], "pending": []}

        def fail(_: dict[str, str]) -> None:
            raise DiscordError("test failure")

        new_state, failures, sent = process_matches(state, [article], NOW, CONFIG, "normal", fail)
        self.assertEqual(len(failures), 1)
        self.assertEqual(sent, [])
        self.assertEqual(new_state["notified"], [])
        self.assertEqual(new_state["pending"][0]["id"], article.id)

    def test_dry_run_does_not_mutate_state_or_send(self) -> None:
        article = Article(
            "/bbs/Gamesale/M.3.A.CCC.html",
            "[NS2] 售 真三國無雙 起源",
            "https://www.ptt.cc/bbs/Gamesale/M.3.A.CCC.html",
        )
        state = {"schema_version": 1, "notified": [], "pending": []}
        before = copy.deepcopy(state)
        send = Mock()
        new_state, pending, sent = process_matches(state, [article], NOW, CONFIG, "dry-run", send)
        send.assert_not_called()
        self.assertEqual(state, before)
        self.assertEqual(new_state, before)
        self.assertEqual(len(pending), 1)
        self.assertEqual(sent, [])

    def test_state_round_trip_and_invalid_state_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            state = {"schema_version": 1, "notified": ["/bbs/Gamesale/M.1.A.AAA.html"], "pending": []}
            write_state_atomic(path, state)
            self.assertEqual(load_state(path), state)
            path.write_text('{"schema_version": 999, "notified": [], "pending": []}', encoding="utf-8")
            with self.assertRaises(ConfigError):
                load_state(path)


if __name__ == "__main__":
    unittest.main()
