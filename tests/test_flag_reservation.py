"""Offline admission tests; no Hermes or model calls."""
import concurrent.futures
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("cap_store", Path(__file__).resolve().parents[1] / "work_store.py")
store_mod = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = store_mod
spec.loader.exec_module(store_mod)


class ReservationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = store_mod.Store(Path(self.temp.name))

    def reserve(self, key):
        return self.store.flag_reserve("W1", "finding", source="events:1", key=key,
                                       others=["W2"], hourly_limit=12)

    def test_held_flags_cannot_evict_wakes_from_budget(self):
        for i in range(12):
            self.assertIs(self.reserve(str(i)), True)
        with self.store._db() as db:
            db.executemany("INSERT INTO events(ref,kind,who,text,detail,at,key) VALUES ('W1','flag','watch','',?,?,?)",
                           [(json.dumps({"woke": False}), time.time(), f"held:{i}") for i in range(600)])
        self.assertIs(self.reserve("after600"), False)

    def test_concurrent_reservations_never_exceed_limit(self):
        # Exercise concurrent reservations on an initialized WAL store, as used
        # by the watcher. First-open WAL setup is a separate pre-existing race.
        with self.store._db():
            pass
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(self.reserve, map(str, range(30))))
        self.assertEqual(results.count(True), 12)
        self.assertEqual(results.count(False), 18)

    def test_duplicate_never_delivers_again(self):
        self.assertIs(self.reserve("same"), True)
        self.assertIsNone(self.reserve("same"))
        self.assertEqual(len(self.store.ledger(limit=-1)), 1)

    def test_expired_wakes_release_capacity(self):
        for i in range(12):
            self.store.event_add("W1", "flag", "watch", "old", key=str(i),
                                 detail='{"woke":true}', at=time.time() - 3601)
        self.assertIs(self.reserve("new"), True)

    def test_corrupt_history_fails_conservatively(self):
        for i in range(12):
            self.store.event_add("W1", "flag", "watch", "unknown", key=str(i), detail="broken")
        self.assertIs(self.reserve("new"), False)

    def test_database_error_does_not_admit(self):
        with patch.object(self.store, "_db", side_effect=RuntimeError("unavailable")):
            with self.assertRaises(RuntimeError):
                self.reserve("new")


if __name__ == "__main__":
    unittest.main()
