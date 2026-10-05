import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from src.domain import Actor, ConflictError, InvalidTransition
from src.repository import SQLiteRepository, utcnow
from src.rules import RuleEngine
from src.service import DomainService


class BasisTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repository = SQLiteRepository(Path(self.tmp.name) / "basis.db")
        self.service = DomainService(self.repository, RuleEngine())
        self.supervisor = Actor("qc-supervisor", "supervisor")

    def tearDown(self):
        self.tmp.cleanup()

    def _base(self):
        assay = self.service.create(
            self.supervisor,
            "assay",
            {
                "name": "Glucose",
                "unit": "mmol/L",
                "allowed_low": 3.9,
                "allowed_high": 6.1,
                "rule_config": {"limit_sd": 3, "trend_n": 4, "consecutive_n": 4},
            },
        )
        lot = self.service.create(
            self.supervisor,
            "qc_lot",
            {"assay_id": assay["id"], "lot_no": "LOT-1", "target": 5.0, "sd": 0.1, "expires_at": "2099-01-01"},
        )
        lot = self.service.transition(self.supervisor, lot["id"], "activate", {"activated_by": "qc-1"})
        instrument = self.service.create(
            self.supervisor,
            "instrument",
            {"name": "Analyzer A", "serial": "A-100", "calibration_due": "2099-01-01"},
        )
        return assay, lot, instrument

    def _accepted_run(self, assay, lot, instrument, value=5.02, run_at="2026-09-27T08:00:00Z"):
        run = self.service.create(
            self.supervisor,
            "qc_run",
            {
                "assay_id": assay["id"],
                "qc_lot_id": lot["id"],
                "instrument_id": instrument["id"],
                "value": value,
                "run_at": run_at,
            },
        )
        return self.service.transition(self.supervisor, run["id"], "evaluate", {"evaluated_by": "qc-1"})

    def _released_batch(self, assay, lot, instrument, run_at="2026-09-27T08:05:00Z"):
        run = self._accepted_run(assay, lot, instrument, run_at=run_at)
        batch = self.service.create(
            self.supervisor,
            "result_batch",
            {
                "assay_id": assay["id"],
                "instrument_id": instrument["id"],
                "qc_run_id": run["id"],
                "run_at": run_at,
                "patient_count": 12,
            },
        )
        return self.service.transition(self.supervisor, batch["id"], "release", {"reviewer_id": "qc-2"})

    def test_release_snapshots_effective_basis(self):
        assay, lot, instrument = self._base()
        released = self._released_batch(assay, lot, instrument)
        basis = released["data"]["release_basis"]
        self.assertEqual(basis["qc_lot"]["id"], lot["id"])
        self.assertEqual(basis["qc_lot"]["lot_no"], "LOT-1")
        self.assertEqual(basis["qc_lot"]["target"], 5.0)
        self.assertEqual(basis["qc_lot"]["sd"], 0.1)
        self.assertIsNone(basis["calibration"]["certificate_id"])
        self.assertEqual(basis["calibration"]["calibration_due"], "2099-01-01")
        self.assertEqual(basis["rules"]["rule_version"], 1)
        self.assertEqual(basis["rules"]["rule_config"], assay["data"]["rule_config"])
        self.assertTrue(basis["released_at"])

    def test_lot_switch_marks_released_batch_affected_and_keeps_snapshot(self):
        assay, first, instrument = self._base()
        released = self._released_batch(assay, first, instrument)
        second = self.service.create(
            self.supervisor,
            "qc_lot",
            {"assay_id": assay["id"], "lot_no": "LOT-2", "target": 5.1, "sd": 0.1, "expires_at": "2099-06-01"},
        )
        self.service.transition(
            self.supervisor,
            second["id"],
            "switch_in",
            {"previous_lot_id": first["id"], "switched_at": "2026-09-27T09:00:00Z"},
        )
        affected = self.service.get(released["id"])
        self.assertEqual(affected["status"], "affected")
        self.assertEqual(affected["data"]["affected_reason"], "qc_lot_switched")
        self.assertEqual(affected["data"]["affected_by"], self.supervisor.user_id)
        self.assertIsNotNone(affected["data"]["release_basis"])
        self.assertEqual(affected["data"]["release_basis"]["qc_lot"]["id"], first["id"])

    def test_recalibration_marks_released_batch_affected(self):
        assay, lot, instrument = self._base()
        released = self._released_batch(assay, lot, instrument)
        self.service.transition(
            self.supervisor,
            instrument["id"],
            "calibrate",
            {"calibration_due": "2099-12-31", "certificate_id": "CERT-2"},
        )
        affected = self.service.get(released["id"])
        self.assertEqual(affected["status"], "affected")
        self.assertEqual(affected["data"]["affected_reason"], "calibration_recalibrated")
        self.assertIsNotNone(affected["data"]["release_basis"])

    def test_rules_update_marks_released_batch_affected(self):
        assay, lot, instrument = self._base()
        released = self._released_batch(assay, lot, instrument)
        self.service.transition(
            self.supervisor,
            assay["id"],
            "update_rules",
            {"rule_config": {"limit_sd": 2, "trend_n": 4, "consecutive_n": 4}},
        )
        affected = self.service.get(released["id"])
        self.assertEqual(affected["status"], "affected")
        self.assertEqual(affected["data"]["affected_reason"], "rules_updated")
        self.assertEqual(affected["data"]["release_basis"]["rules"]["rule_version"], 1)

    def test_recalculate_releases_with_new_basis_and_keeps_history(self):
        assay, lot, instrument = self._base()
        released = self._released_batch(assay, lot, instrument)
        self.service.transition(
            self.supervisor,
            assay["id"],
            "update_rules",
            {"rule_config": {"limit_sd": 2, "trend_n": 4, "consecutive_n": 4}},
        )
        affected = self.service.get(released["id"])
        self.assertEqual(affected["status"], "affected")
        recalculated = self.service.transition(self.supervisor, released["id"], "recalculate", {})
        self.assertEqual(recalculated["status"], "released")
        self.assertEqual(recalculated["data"]["release_basis"]["rules"]["rule_version"], 2)
        self.assertEqual(recalculated["data"]["release_basis"]["qc_lot"]["id"], lot["id"])
        self.assertEqual(len(recalculated["data"]["basis_history"]), 1)
        self.assertEqual(recalculated["data"]["basis_history"][0]["rules"]["rule_version"], 1)
        self.assertIsNone(recalculated["data"]["affected_reason"])
        self.assertEqual(recalculated["data"]["review_status"], "complete")

    def test_concurrent_release_accepts_only_first(self):
        assay, lot, instrument = self._base()
        run = self._accepted_run(assay, lot, instrument)
        batch = self.service.create(
            self.supervisor,
            "result_batch",
            {
                "assay_id": assay["id"],
                "instrument_id": instrument["id"],
                "qc_run_id": run["id"],
                "run_at": "2026-09-27T08:05:00Z",
                "patient_count": 12,
            },
        )
        version = batch["version"]
        barrier = threading.Barrier(2)
        results = []
        errors = []

        def release():
            barrier.wait()
            try:
                results.append(
                    self.service.transition(
                        self.supervisor,
                        batch["id"],
                        "release",
                        {"reviewer_id": "qc-2"},
                        expected_version=version,
                    )
                )
            except (ConflictError, InvalidTransition) as exc:
                errors.append(exc)

        first = threading.Thread(target=release)
        second = threading.Thread(target=release)
        first.start()
        second.start()
        first.join()
        second.join()
        self.assertEqual(len(results), 1)
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], (ConflictError, InvalidTransition))
        final = self.service.get(batch["id"])
        self.assertEqual(final["status"], "released")

    def test_optimistic_lock_rejects_second_release(self):
        assay, lot, instrument = self._base()
        run = self._accepted_run(assay, lot, instrument)
        batch = self.service.create(
            self.supervisor,
            "result_batch",
            {
                "assay_id": assay["id"],
                "instrument_id": instrument["id"],
                "qc_run_id": run["id"],
                "run_at": "2026-09-27T08:05:00Z",
                "patient_count": 12,
            },
        )
        self.repository.update_entity(batch["id"], batch["version"], "released", dict(batch["data"]))
        with self.assertRaises(ConflictError):
            self.repository.update_entity(batch["id"], batch["version"], "released", dict(batch["data"]))

    def test_retry_resumes_after_database_lock(self):
        entity = self.repository.create_entity("e1", "assay", "active", {"a": 1}, "u")
        real_connect = self.repository._connect
        state = {"calls": 0}

        def flaky_connect():
            state["calls"] += 1
            if state["calls"] == 1:
                raise sqlite3.OperationalError("database is locked")
            return real_connect()

        self.repository._connect = flaky_connect
        with patch("src.repository.time.sleep"):
            updated = self.repository.update_entity("e1", entity["version"], "active", {"a": 2})
        self.assertEqual(updated["data"]["a"], 2)
        self.assertEqual(self.repository.get_entity("e1")["data"]["a"], 2)

    def test_backfill_complete_for_legacy_released_batch(self):
        assay, lot, _ = self._base()
        instrument = self.service.create(
            self.supervisor,
            "instrument",
            {
                "name": "Analyzer A",
                "serial": "A-100",
                "calibration_due": "2099-01-01",
                "certificate_id": "CERT-1",
            },
        )
        run_at = utcnow()
        run = self._accepted_run(assay, lot, instrument, run_at=run_at)
        batch = self.service.create(
            self.supervisor,
            "result_batch",
            {
                "assay_id": assay["id"],
                "instrument_id": instrument["id"],
                "qc_run_id": run["id"],
                "run_at": run_at,
                "patient_count": 12,
            },
        )
        # Simulate a legacy release that never recorded its basis.
        self.repository.update_entity(batch["id"], batch["version"], "released", dict(batch["data"]))
        result = self.service.backfill_basis(self.supervisor)
        self.assertEqual(result["processed"], 1)
        self.assertEqual(result["complete"], 1)
        self.assertEqual(result["pending_review"], 0)
        fetched = self.service.get(batch["id"])
        self.assertEqual(fetched["data"]["review_status"], "complete")
        self.assertTrue(fetched["data"]["release_basis"]["backfilled"])
        self.assertEqual(fetched["data"]["release_basis"]["qc_lot"]["id"], lot["id"])
        self.assertEqual(fetched["data"]["release_basis"]["rules"]["rule_version"], 1)
        self.assertEqual(fetched["data"]["release_basis"]["calibration"]["certificate_id"], "CERT-1")

    def test_backfill_marks_incomplete_pending_review(self):
        assay, lot, instrument = self._base()
        run = self._accepted_run(assay, lot, instrument, run_at="2000-01-01T00:00:00Z")
        batch = self.service.create(
            self.supervisor,
            "result_batch",
            {
                "assay_id": assay["id"],
                "instrument_id": instrument["id"],
                "qc_run_id": run["id"],
                "run_at": "2000-01-01T00:00:00Z",
                "patient_count": 1,
            },
        )
        self.repository.update_entity(batch["id"], batch["version"], "released", dict(batch["data"]))
        result = self.service.backfill_basis(self.supervisor)
        self.assertEqual(result["processed"], 1)
        self.assertEqual(result["complete"], 0)
        self.assertEqual(result["pending_review"], 1)
        fetched = self.service.get(batch["id"])
        self.assertEqual(fetched["data"]["review_status"], "pending_review")
        self.assertIsNone(fetched["data"]["release_basis"]["calibration"])
        self.assertIsNone(fetched["data"]["release_basis"]["rules"])

    def test_backfill_skips_batches_with_basis_and_waiting(self):
        assay, lot, instrument = self._base()
        released = self._released_batch(assay, lot, instrument)
        run = self._accepted_run(assay, lot, instrument)
        self.service.create(
            self.supervisor,
            "result_batch",
            {
                "assay_id": assay["id"],
                "instrument_id": instrument["id"],
                "qc_run_id": run["id"],
                "run_at": "2026-09-27T08:05:00Z",
                "patient_count": 1,
            },
        )
        result = self.service.backfill_basis(self.supervisor)
        self.assertEqual(result["processed"], 0)
        self.assertEqual(self.service.get(released["id"])["data"]["review_status"], "complete")


if __name__ == "__main__":
    unittest.main()
