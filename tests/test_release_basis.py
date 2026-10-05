import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path

from src.domain import Actor, ConflictError, PermissionDenied
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


class BasisFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.service = DomainService(
            SQLiteRepository(Path(self.tmp.name) / "basis.db"),
            RuleEngine(),
        )
        self.supervisor = Actor("qc-supervisor", "supervisor")
        self.operator_one = Actor("duty-a", "supervisor")
        self.operator_two = Actor("duty-b", "supervisor")

    def tearDown(self):
        self.tmp.cleanup()

    def _setup(self, value=5.02, rule_config=None):
        assay = self.service.create(
            self.supervisor,
            "assay",
            {
                "name": "Glucose",
                "unit": "mmol/L",
                "allowed_low": 3.9,
                "allowed_high": 6.1,
                "rule_config": rule_config or {"limit_sd": 3, "trend_n": 4},
            },
        )
        lot = self.service.create(
            self.supervisor,
            "qc_lot",
            {"assay_id": assay["id"], "lot_no": "LOT-1", "target": 5.0, "sd": 0.1,
             "expires_at": "2099-01-01"},
        )
        lot = self.service.transition(self.supervisor, lot["id"], "activate", {"activated_by": "qc-1"})
        instrument = self.service.create(
            self.supervisor,
            "instrument",
            {"name": "Analyzer A", "serial": "A-100", "calibration_due": "2099-01-01"},
        )
        instrument = self.service.transition(
            self.supervisor,
            instrument["id"],
            "calibrate",
            {"calibration_due": "2099-01-01", "certificate_id": "CERT-1",
             "calibrated_at": "2026-09-27T07:00:00Z"},
        )
        run = self.service.create(
            self.supervisor,
            "qc_run",
            {"assay_id": assay["id"], "qc_lot_id": lot["id"],
             "instrument_id": instrument["id"], "value": value,
             "run_at": "2026-09-27T08:00:00Z"},
        )
        run = self.service.transition(self.supervisor, run["id"], "evaluate", {"evaluated_by": "qc-1"})
        batch = self.service.create(
            self.supervisor,
            "result_batch",
            {"assay_id": assay["id"], "instrument_id": instrument["id"],
             "qc_run_id": run["id"], "run_at": "2026-09-27T08:05:00Z", "patient_count": 12},
        )
        batch = self.service.transition(
            self.supervisor, batch["id"], "release", {"reviewer_id": "qc-2"}
        )
        return assay, lot, instrument, run, batch


class ReleaseBasisTest(BasisFixture):
    def test_release_freezes_lot_certificate_and_rule_version(self):
        assay, lot, instrument, run, batch = self._setup()
        self.assertEqual(batch["status"], "released")
        self.assertEqual(batch["data"]["basis_status"], "valid")
        basis = batch["data"]["release_basis"]
        self.assertEqual(basis["qc_lot"]["qc_lot_id"], lot["id"])
        self.assertEqual(basis["qc_lot"]["lot_no"], "LOT-1")
        self.assertEqual(basis["calibration"]["certificate_id"], "CERT-1")
        self.assertEqual(basis["rules"]["rule_version"], 1)
        self.assertEqual(basis["rules"]["rule_config"]["limit_sd"], 3)
        self.assertIn("captured_at", basis)

    def test_release_without_certificate_is_rejected(self):
        assay = self.service.create(
            self.supervisor, "assay",
            {"name": "X", "unit": "u", "allowed_low": 0, "allowed_high": 10},
        )
        lot = self.service.create(
            self.supervisor, "qc_lot",
            {"assay_id": assay["id"], "lot_no": "L", "target": 5, "sd": 0.1,
             "expires_at": "2099-01-01"},
        )
        lot = self.service.transition(self.supervisor, lot["id"], "activate", {"activated_by": "a"})
        instrument = self.service.create(
            self.supervisor, "instrument",
            {"name": "I", "serial": "S", "calibration_due": "2099-01-01"},
        )
        run = self.service.create(
            self.supervisor, "qc_run",
            {"assay_id": assay["id"], "qc_lot_id": lot["id"],
             "instrument_id": instrument["id"], "value": 5.0, "run_at": "2026-09-27T08:00:00Z"},
        )
        run = self.service.transition(self.supervisor, run["id"], "evaluate", {"evaluated_by": "a"})
        batch = self.service.create(
            self.supervisor, "result_batch",
            {"assay_id": assay["id"], "instrument_id": instrument["id"],
             "qc_run_id": run["id"], "run_at": "2026-09-27T08:05:00Z", "patient_count": 1},
        )
        with self.assertRaises(ConflictError):
            self.service.transition(self.supervisor, batch["id"], "release", {"reviewer_id": "r"})


class InvalidationTest(BasisFixture):
    def test_new_calibration_certificate_invalidates_and_revalidates(self):
        assay, lot, instrument, run, batch = self._setup()
        instrument = self.service.transition(
            self.supervisor,
            instrument["id"],
            "calibrate",
            {"calibration_due": "2099-12-31", "certificate_id": "CERT-2",
             "calibrated_at": "2026-09-28T07:00:00Z"},
        )
        flagged = self.service.get(batch["id"])
        self.assertEqual(flagged["data"]["basis_status"], "invalidated")
        self.assertIn("calibration_certificate_changed", flagged["data"]["basis_flags"])
        # The original evidence snapshot is preserved on the issued report.
        self.assertEqual(flagged["data"]["release_basis"]["calibration"]["certificate_id"], "CERT-1")
        self.assertEqual(flagged["status"], "released")
        invalidation = [
            entry for entry in flagged["data"]["basis_history"] if entry["type"] == "invalidated"
        ]
        self.assertEqual(len(invalidation), 1)
        # Recalculation against the new certificate restores a valid basis.
        revalidated = self.service.transition(
            self.supervisor,
            batch["id"],
            "revalidate",
            {"reviewer_id": "qc-3", "note": "recomputed after calibration"},
        )
        self.assertEqual(revalidated["data"]["basis_status"], "valid")
        self.assertEqual(
            revalidated["data"]["release_basis"]["calibration"]["certificate_id"], "CERT-2"
        )
        self.assertEqual(revalidated["data"]["basis_flags"], [])

    def test_same_certificate_recalibration_does_not_invalidate(self):
        assay, lot, instrument, run, batch = self._setup()
        self.service.transition(
            self.supervisor,
            instrument["id"],
            "calibrate",
            {"calibration_due": "2099-12-31", "certificate_id": "CERT-1",
             "calibrated_at": "2026-09-28T07:00:00Z"},
        )
        self.assertEqual(self.service.get(batch["id"])["data"]["basis_status"], "valid")

    def test_revised_rules_invalidate_released_batches(self):
        assay, lot, instrument, run, batch = self._setup()
        self.service.transition(
            self.supervisor,
            assay["id"],
            "revise_rules",
            {"rule_config": {"limit_sd": 2.5, "trend_n": 6}, "reason": "tighten limits"},
        )
        flagged = self.service.get(batch["id"])
        self.assertEqual(flagged["data"]["basis_status"], "invalidated")
        self.assertIn("rule_version_changed", flagged["data"]["basis_flags"])
        assay = self.service.get(assay["id"])
        self.assertEqual(assay["data"]["rule_version"], 2)
        # The run can be re-evaluated under the new rules and still accepted.
        rerun = self.service.transition(
            self.supervisor, run["id"], "evaluate", {"evaluated_by": "qc-9"}
        )
        self.assertEqual(rerun["status"], "accepted")
        fixed = self.service.transition(
            self.supervisor, batch["id"], "revalidate", {"reviewer_id": "qc-3"}
        )
        self.assertEqual(fixed["data"]["basis_status"], "valid")
        self.assertEqual(fixed["data"]["release_basis"]["rules"]["rule_version"], 2)

    def test_lot_switch_invalidates_old_lot_batches_and_retires_lot(self):
        assay, first, instrument, run, batch = self._setup()
        second = self.service.create(
            self.supervisor,
            "qc_lot",
            {"assay_id": assay["id"], "lot_no": "LOT-2", "target": 5.1, "sd": 0.1,
             "expires_at": "2099-06-01"},
        )
        self.service.transition(
            self.supervisor,
            second["id"],
            "switch_in",
            {"previous_lot_id": first["id"], "switched_at": "2026-09-27T09:00:00Z"},
        )
        flagged = self.service.get(batch["id"])
        self.assertEqual(flagged["data"]["basis_status"], "invalidated")
        self.assertIn("qc_lot_changed", flagged["data"]["basis_flags"])
        # The switched-out lot is retired so it cannot back future releases.
        self.assertEqual(self.service.get(first["id"])["status"], "retired")
        # Recompute against a replacement accepted run on the new lot.
        replacement = self.service.create(
            self.supervisor,
            "qc_run",
            {"assay_id": assay["id"], "qc_lot_id": second["id"],
             "instrument_id": instrument["id"], "value": 5.08,
             "run_at": "2026-09-27T09:10:00Z"},
        )
        replacement = self.service.transition(
            self.supervisor, replacement["id"], "evaluate", {"evaluated_by": "qc-1"}
        )
        fixed = self.service.transition(
            self.supervisor,
            batch["id"],
            "revalidate",
            {"reviewer_id": "qc-3", "replacement_run_id": replacement["id"]},
        )
        self.assertEqual(fixed["data"]["basis_status"], "valid")
        self.assertEqual(fixed["data"]["release_basis"]["qc_lot"]["lot_no"], "LOT-2")
        self.assertEqual(fixed["data"]["qc_run_id"], replacement["id"])

    def test_invalidated_batches_are_listable_by_basis_status(self):
        assay, lot, instrument, run, batch = self._setup()
        self.service.transition(
            self.supervisor,
            instrument["id"],
            "calibrate",
            {"calibration_due": "2099-12-31", "certificate_id": "CERT-9",
             "calibrated_at": "2026-09-28T07:00:00Z"},
        )
        items = self.service.list("result_batch", basis_status="invalidated")
        self.assertEqual([item["id"] for item in items], [batch["id"]])
        self.assertEqual(self.service.list("result_batch", basis_status="valid"), [])


class ConcurrencyTest(BasisFixture):
    def test_two_operators_release_same_batch_only_first_wins(self):
        assay, lot, instrument, run, _ = self._setup_and_release_waiting_batch()
        results = {}
        barrier = threading.Barrier(2)

        def do_release(actor, key):
            barrier.wait()
            try:
                entity = self.service.transition(
                    actor, batch_id, "release", {"reviewer_id": actor.user_id},
                    idempotency_key=key,
                )
                results[actor.user_id] = ("ok", entity["data"].get("released_by"))
            except ConflictError as exc:
                results[actor.user_id] = ("conflict", str(exc))
        batch_id = self._waiting_batch_id
        thread_one = threading.Thread(target=do_release, args=(self.operator_one, "release-1"))
        thread_two = threading.Thread(target=do_release, args=(self.operator_two, "release-2"))
        thread_one.start()
        thread_two.start()
        thread_one.join(timeout=30)
        thread_two.join(timeout=30)
        statuses = sorted(value[0] for value in results.values())
        self.assertEqual(statuses, ["conflict", "ok"])
        released = self.service.get(self._waiting_batch_id)
        self.assertEqual(released["status"], "released")
        # Exactly one of the racing submissions produced an audited release.
        releases = [
            row for row in self.service.audit_log(released["id"])
            if row["action"] == "release"
            and row["detail"].get("idempotency_key") in ("release-1", "release-2")
        ]
        self.assertEqual(len(releases), 1)
        winners = [value[1] for value in results.values() if value[0] == "ok"]
        self.assertEqual(winners[0], released["data"]["released_by"])

    def _setup_and_release_waiting_batch(self):
        assay, lot, instrument, run, batch = self._setup()
        # Undo the release from the fixture to get a waiting batch.
        released = self.service.transition(
            self.supervisor, batch["id"], "correct", {"reason": "reset for race test"}
        )
        self.assertEqual(released["status"], "waiting")
        self._waiting_batch_id = batch["id"]
        return assay, lot, instrument, run, released

    def test_same_operator_idempotent_release_replay_after_failure(self):
        assay, lot, instrument, run, released = self._setup_and_release_waiting_batch()
        entity = self.service.transition(
            self.operator_one,
            self._waiting_batch_id,
            "release",
            {"reviewer_id": "duty-a"},
            idempotency_key="release-dup",
        )
        self.assertEqual(entity["status"], "released")
        replay = self.service.transition(
            self.operator_one,
            self._waiting_batch_id,
            "release",
            {"reviewer_id": "duty-a"},
            idempotency_key="release-dup",
        )
        self.assertEqual(replay["id"], entity["id"])
        releases = [
            row for row in self.service.audit_log(entity["id"])
            if row["action"] == "release"
            and row["detail"].get("idempotency_key") == "release-dup"
        ]
        self.assertEqual(len(releases), 1)


class RetryTest(BasisFixture):
    def test_commit_resumes_after_transient_database_lock(self):
        assay, lot, instrument, run, batch = self._setup()
        calls = {"count": 0}
        repository = self.service.repository
        original = repository.apply_transition

        def flaky(*args, **kwargs):
            # Only the instrument's own commit fails once; invalidation commits pass.
            if args and args[0] == instrument["id"]:
                calls["count"] += 1
                if calls["count"] == 1:
                    raise sqlite3.OperationalError("database is locked")
            return original(*args, **kwargs)

        repository.apply_transition = flaky
        recalibrated = self.service.transition(
            self.supervisor,
            instrument["id"],
            "calibrate",
            {"calibration_due": "2099-12-31", "certificate_id": "CERT-7",
             "calibrated_at": "2026-09-28T07:00:00Z"},
            idempotency_key="calib-retry",
        )
        repository.apply_transition = original
        self.assertEqual(recalibrated["data"]["certificate_id"], "CERT-7")
        self.assertEqual(calls["count"], 2)
        flagged = self.service.get(batch["id"])
        self.assertEqual(flagged["data"]["basis_status"], "invalidated")

    def test_retry_resumes_from_durable_commit_when_ack_lost(self):
        # First attempt commits the tx but the client retries with the same key:
        # the replay must return the committed row without inserting a duplicate.
        assay, lot, instrument, run, released = self._setup_and_release_waiting_batch()
        entity = self.service.transition(
            self.operator_one,
            self._waiting_batch_id,
            "release",
            {"reviewer_id": "duty-a"},
            idempotency_key="ack-lost",
        )
        again = self.service.transition(
            self.operator_one,
            self._waiting_batch_id,
            "release",
            {"reviewer_id": "duty-a"},
            idempotency_key="ack-lost",
        )
        self.assertEqual(again["version"], entity["version"])

    def _setup_and_release_waiting_batch(self):
        assay, lot, instrument, run, batch = self._setup()
        released = self.service.transition(
            self.supervisor, batch["id"], "correct", {"reason": "reset"}
        )
        self._waiting_batch_id = batch["id"]
        return assay, lot, instrument, run, released


class BackfillTest(BasisFixture):
    def _legacy_released_batch(self, with_certificate=True, with_lot=True):
        assay = self.service.create(
            self.supervisor, "assay",
            {"name": "Legacy", "unit": "u", "allowed_low": 0, "allowed_high": 10},
        )
        lot = self.service.create(
            self.supervisor, "qc_lot",
            {"assay_id": assay["id"], "lot_no": "OLD-LOT", "target": 5, "sd": 0.1,
             "expires_at": "2099-01-01"},
        )
        lot = self.service.transition(self.supervisor, lot["id"], "activate", {"activated_by": "a"})
        instrument = self.service.create(
            self.supervisor, "instrument",
            {"name": "Old", "serial": "OLD-S", "calibration_due": "2099-01-01"},
        )
        if with_certificate:
            instrument = self.service.transition(
                self.supervisor,
                instrument["id"],
                "calibrate",
                {"calibration_due": "2099-01-01", "certificate_id": "OLD-CERT",
                 "calibrated_at": "2026-08-01T07:00:00Z"},
            )
        run = self.service.create(
            self.supervisor, "qc_run",
            {"assay_id": assay["id"], "qc_lot_id": lot["id"],
             "instrument_id": instrument["id"], "value": 5.0, "run_at": "2026-08-02T08:00:00Z"},
        )
        run = self.service.transition(self.supervisor, run["id"], "evaluate", {"evaluated_by": "a"})
        batch = self.service.create(
            self.supervisor, "result_batch",
            {"assay_id": assay["id"], "instrument_id": instrument["id"],
             "qc_run_id": run["id"], "run_at": "2026-08-02T08:05:00Z", "patient_count": 3},
        )
        # Simulate a legacy row released before evidence snapshots were mandatory:
        # write the released state directly, bypassing the modern release gate.
        data = dict(batch["data"])
        data["released_by"] = "legacy"
        if not with_lot:
            data.pop("qc_run_id", None)
        legacy = self.service.repository.update_entity(batch["id"], batch["version"], "released", data)
        return legacy, instrument

    def test_backfill_reconstructs_complete_evidence(self):
        batch, _ = self._legacy_released_batch()
        summary = self.service.backfill_release_basis(self.supervisor)
        self.assertGreaterEqual(summary["reconstructed"], 1)
        filled = self.service.get(batch["id"])
        self.assertEqual(filled["data"]["basis_status"], "valid")
        self.assertEqual(
            filled["data"]["release_basis"]["calibration"]["certificate_id"], "OLD-CERT"
        )
        self.assertEqual(filled["data"]["release_basis"]["qc_lot"]["lot_no"], "OLD-LOT")
        self.assertTrue(filled["data"]["release_basis"]["backfilled"])

    def test_backfill_marks_unreconstructable_pending_review(self):
        batch, _ = self._legacy_released_batch(with_certificate=False)
        self.service.backfill_release_basis(self.supervisor)
        flagged = self.service.get(batch["id"])
        self.assertEqual(flagged["data"]["basis_status"], "pending_review")
        self.assertIn("calibration_certificate_missing", flagged["data"]["basis_flags"])
        pending = self.service.list("result_batch", basis_status="pending_review")
        self.assertIn(batch["id"], [item["id"] for item in pending])

    def test_backfill_is_idempotent(self):
        batch, _ = self._legacy_released_batch()
        first = self.service.backfill_release_basis(self.supervisor, target_id=batch["id"])
        second = self.service.backfill_release_basis(self.supervisor, target_id=batch["id"])
        self.assertEqual(first["reconstructed"], 1)
        self.assertEqual(second["skipped"], 1)
        self.assertEqual(second["reconstructed"], 0)

    def test_viewer_cannot_backfill(self):
        with self.assertRaises(PermissionDenied):
            self.service.backfill_release_basis(Actor("v", "viewer"))


if __name__ == "__main__":
    unittest.main()
