import sqlite3
import time
from uuid import uuid4

from .audit import AuditTrail
from .domain import ConflictError, NotFoundError, PermissionDenied
from .rules import RuleEngine, _iso_now

# Reasons written into basis_history / basis_flags when evidence changes.
_REASON_CALIBRATION = "calibration_certificate_changed"
_REASON_RULES = "rule_version_changed"
_REASON_LOT = "qc_lot_changed"
_REASON_BACKFILL_GAP = "historical_evidence_incomplete"


class DomainService:
    def __init__(self, repository, rules=None):
        self.repository = repository
        self.rules = rules or RuleEngine()
        self.audit = AuditTrail(repository)

    def _lookup(self, kind, field, value):
        return self.repository.find_entities(self.rules.normalize_kind(kind), field, value)

    def health(self):
        return {"status": "ok" if self.repository.ping() else "error"}

    # ------------------------------------------------------------------ create
    def create(self, actor, kind, data, idempotency_key=None):
        kind = self.rules.normalize_kind(kind)
        payload = dict(data or {})
        if idempotency_key:
            existing = self.repository.get_idempotency(actor.user_id, idempotency_key)
            if existing:
                entity = self.repository.get_entity(existing)
                if entity:
                    return entity
        validated = self.rules.validate_create(actor, kind, payload, self._lookup)
        if validated:
            payload.update(validated)
        entity_id = str(payload.pop("id", "") or uuid4())
        if self.repository.get_entity(entity_id):
            raise ConflictError("entity already exists: " + entity_id)
        status = self.rules.initial_status(kind)
        entity = self.repository.create_entity(entity_id, kind, status, payload, actor.user_id)
        self.audit.record(entity_id, actor, "create", None, status, {"kind": kind})
        if idempotency_key:
            self.repository.save_idempotency(actor.user_id, idempotency_key, entity_id)
        return entity

    # -------------------------------------------------------------- transition
    def transition(self, actor, entity_id, action, data=None, expected_version=None,
                   idempotency_key=None, max_attempts=5):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)

        # Replay after a crash between commit and response: return the stored result
        # and re-apply the (idempotent) side effects without duplicating the row.
        if idempotency_key:
            record = self.repository.get_idempotency_record(actor.user_id, idempotency_key)
            if record and record["entity_id"] == entity_id:
                committed = self.repository.get_entity(entity_id)
                self._run_action_effects(record["action"], committed, actor)
                return committed

        next_status, allowed_statuses, patch = self.rules.validate_transition(
            actor, entity, action, dict(data or {}), self._lookup
        )
        merged = dict(entity["data"])
        merged.update(patch)
        expected = int(expected_version) if expected_version is not None else entity["version"]
        audit_detail = {"patch": patch}
        if idempotency_key:
            audit_detail["idempotency_key"] = idempotency_key

        def attempt():
            return self.repository.apply_transition(
                entity_id,
                expected,
                allowed_statuses,
                next_status,
                merged,
                {
                    "actor_id": actor.user_id,
                    "actor_role": actor.role,
                    "action": action,
                    "from_status": entity["status"],
                    "to_status": next_status,
                    "detail": audit_detail,
                },
                (
                    {"actor_id": actor.user_id, "idem_key": idempotency_key, "action": action}
                    if idempotency_key
                    else None
                ),
            )

        updated = self._commit_with_retry(attempt, entity_id, expected, max_attempts=max_attempts)
        self._run_action_effects(action, updated, actor)
        return updated

    @staticmethod
    def _commit_with_retry(attempt, entity_id, expected_version, max_attempts=5):
        """Resume from the last durable point after transient SQLite lock failures."""
        for index in range(max_attempts):
            try:
                return attempt()
            except sqlite3.OperationalError as exc:
                if str(exc).lower() not in ("database is locked", "database table is locked"):
                    raise
                if index == max_attempts - 1:
                    raise
                time.sleep(0.01 * (2 ** index))
        raise ConflictError("could not commit transition for %s" % entity_id)

    # ----------------------------------------------- post-transition side effects
    def _run_action_effects(self, action, updated, actor):
        if action == "calibrate":
            self._invalidate_basis(
                instrument_id=updated["id"],
                changed="calibration",
                current_certificate=updated["data"].get("certificate_id"),
                actor=actor,
            )
        elif action == "switch_in":
            previous_lot_id = updated["data"].get("replaces_lot_id")
            if previous_lot_id:
                self._invalidate_basis(
                    qc_lot_id=previous_lot_id,
                    changed="qc_lot",
                    actor=actor,
                    detail="qc lot %s was switched out for %s" % (previous_lot_id, updated["id"]),
                )
                self._retire_switched_out_lot(previous_lot_id, updated["id"], actor)
        elif action in ("retire", "suspend"):
            self._invalidate_basis(
                qc_lot_id=updated["id"],
                changed="qc_lot",
                actor=actor,
                detail="qc lot %s was %s" % (updated["id"], action),
            )
        elif action == "revise_rules":
            self._invalidate_basis(
                assay_id=updated["id"],
                changed="rules",
                current_rule_version=updated["data"].get("rule_version"),
                actor=actor,
            )

    def _retire_switched_out_lot(self, lot_id, new_lot_id, actor):
        lot = self.repository.get_entity(lot_id)
        if not lot or lot["status"] != "active":
            return
        merged = dict(lot["data"])
        merged["retired_for_lot_id"] = new_lot_id
        self._commit_with_retry(
            lambda: self.repository.apply_transition(
                lot_id,
                lot["version"],
                ("active",),
                "retired",
                merged,
                {
                    "actor_id": actor.user_id,
                    "actor_role": actor.role,
                    "action": "auto_retire_on_switch",
                    "from_status": lot["status"],
                    "to_status": "retired",
                    "detail": {"replaced_by": new_lot_id},
                },
            ),
            lot_id,
            lot["version"],
        )

    def _invalidate_basis(self, actor, changed, qc_lot_id=None, instrument_id=None,
                          assay_id=None, current_certificate=None, current_rule_version=None,
                          detail=None):
        """Flag released batches whose frozen evidence no longer matches current truth.

        The release_basis snapshot itself is never rewritten; a basis_history entry is
        appended so the previously issued report keeps a complete evidence trail.
        """
        canonical = {
            "calibration": _REASON_CALIBRATION,
            "qc_lot": _REASON_LOT,
            "rules": _REASON_RULES,
        }[changed]
        for batch in self.repository.list_entities(kind="result_batch", status="released"):
            if batch["data"].get("basis_status") != "valid":
                continue
            basis = batch["data"].get("release_basis") or {}
            matched = False
            if changed == "calibration":
                frozen = str(basis.get("calibration", {}).get("certificate_id"))
                matched = (
                    batch["data"].get("instrument_id") == instrument_id
                    and current_certificate is not None
                    and frozen not in (str(current_certificate), "None")
                )
            elif changed == "qc_lot":
                matched = basis.get("qc_lot", {}).get("qc_lot_id") == qc_lot_id
            elif changed == "rules":
                frozen_version = basis.get("rules", {}).get("rule_version")
                matched = (
                    batch["data"].get("assay_id") == assay_id
                    and current_rule_version is not None
                    and frozen_version != current_rule_version
                )
            if not matched:
                continue
            data = dict(batch["data"])
            data["basis_status"] = "invalidated"
            data["basis_flags"] = [canonical]
            history = list(data.get("basis_history") or [])
            history.append(
                {
                    "type": "invalidated",
                    "changed": changed,
                    "reason": canonical,
                    "detail": detail,
                    "actor_id": actor.user_id,
                    "at": _iso_now(),
                }
            )
            data["basis_history"] = history
            updated = self._commit_with_retry(
                lambda b=batch, d=data: self.repository.apply_transition(
                    b["id"],
                    b["version"],
                    ("released",),
                    "released",
                    d,
                    {
                        "actor_id": actor.user_id,
                        "actor_role": actor.role,
                        "action": "basis_invalidated",
                        "from_status": "released",
                        "to_status": "released",
                        "detail": {"changed": changed},
                    },
                ),
                batch["id"],
                batch["version"],
            )
            # Keep loop versions fresh even though each batch is independent.
            del updated

    # --------------------------------------------------------------- backfill
    def backfill_release_basis(self, actor, target_id=None):
        """Reconstruct release evidence for batches released before snapshots existed.

        Complete evidence is marked valid; batches where any of the three sources
        cannot be reconstructed are flagged pending_review for manual follow-up.
        """
        if actor.role not in ("supervisor", "admin", "auditor"):
            raise PermissionDenied("role %s is not allowed to backfill evidence" % actor.role)
        batches = self.repository.list_entities(kind="result_batch", status="released")
        completed = 0
        pending = 0
        skipped = 0
        affected = []
        for batch in batches:
            if target_id and batch["id"] != target_id:
                continue
            data0 = batch["data"]
            if data0.get("release_basis"):
                # Modern batches already carry evidence. A pending_review row is
                # only retried when targeted explicitly (e.g. after missing records
                # are restored); bulk backfill reports it for follow-up.
                if data0.get("basis_status") == "pending_review" and not target_id:
                    pending += 1
                    affected.append(batch["id"])
                else:
                    skipped += 1
                continue
            basis, gaps = self._reconstruct_basis(batch)
            merged = dict(data0)
            merged["release_basis"] = basis
            history = list(merged.get("basis_history") or [])
            if gaps:
                merged["basis_status"] = "pending_review"
                merged["basis_flags"] = gaps + [_REASON_BACKFILL_GAP]
                history.append(
                    {
                        "type": "backfill_gap",
                        "gaps": gaps,
                        "actor_id": actor.user_id,
                        "at": _iso_now(),
                    }
                )
                pending += 1
            else:
                merged["basis_status"] = "valid"
                merged["basis_flags"] = list(merged.get("basis_flags") or [])
                history.append(
                    {"type": "backfill", "actor_id": actor.user_id, "at": _iso_now()}
                )
                completed += 1
            merged["basis_history"] = history
            self._commit_with_retry(
                lambda b=batch, d=merged: self.repository.apply_transition(
                    b["id"],
                    b["version"],
                    ("released",),
                    "released",
                    d,
                    {
                        "actor_id": actor.user_id,
                        "actor_role": actor.role,
                        "action": "backfill_release_basis",
                        "from_status": "released",
                        "to_status": "released",
                        "detail": {"gaps": gaps},
                    },
                ),
                batch["id"],
                batch["version"],
            )
            affected.append(batch["id"])
        return {
            "reconstructed": completed,
            "pending_review": pending,
            "skipped": skipped,
            "affected": affected,
        }

    def _reconstruct_basis(self, batch):
        """Best-effort historical reconstruction of the three evidence sources."""
        data = batch["data"]
        gaps = []
        run = self._lookup_one("qc_run", "id", data.get("qc_run_id"))
        instrument = self._lookup_one("instrument", "id", data.get("instrument_id"))
        assay = self._lookup_one("assay", "id", data.get("assay_id"))
        lot = None
        if run:
            lot = self._lookup_one("qc_lot", "id", run["data"].get("qc_lot_id"))
        if not run:
            gaps.append("qc_run_missing")
        if not instrument:
            gaps.append("instrument_missing")
        if not assay:
            gaps.append("assay_missing")
        if run and not lot:
            gaps.append("qc_lot_missing")

        # Pick the calibration certificate in force at result time: the last history
        # entry dated on/before the batch, falling back to the current certificate.
        certificate = {"certificate_id": None, "calibration_due": None, "calibrated_at": None}
        if instrument:
            history = instrument["data"].get("calibration_history") or []
            as_of = str(data.get("run_at", ""))
            prior = [
                entry
                for entry in history
                if str(entry.get("calibrated_at", ""))[:10] <= as_of[:10]
            ]
            if prior:
                chosen = prior[-1]
                certificate = {
                    "certificate_id": chosen.get("certificate_id"),
                    "calibration_due": chosen.get("calibration_due"),
                    "calibrated_at": chosen.get("calibrated_at"),
                }
            elif instrument["data"].get("certificate_id"):
                certificate = {
                    "certificate_id": instrument["data"].get("certificate_id"),
                    "calibration_due": instrument["data"].get("calibration_due"),
                    "calibrated_at": instrument["data"].get("calibrated_at"),
                }
            else:
                gaps.append("calibration_certificate_missing")

        # Rule version effective at result time: last revision on/before it; if the
        # assay has never been revised the initial version 1 is assumed historical.
        rule_version = 1
        rule_config = dict(assay["data"].get("rule_config") or {}) if assay else {}
        if assay:
            revisions = assay["data"].get("rule_revisions") or []
            as_of = str(data.get("run_at", ""))
            prior = [
                entry
                for entry in revisions
                if str(entry.get("revised_at", "")) <= as_of
            ]
            if prior:
                rule_version = prior[-1]["rule_version"]
                rule_config = dict(prior[-1].get("rule_config") or {})
            elif len(revisions) == 1:
                # Only one revision on record: assume version 1 applied historically.
                rule_version = 1

        basis = {
            "captured_at": None,
            "backfilled": True,
            "rules": {"rule_version": rule_version, "rule_config": rule_config},
            "qc_lot": (
                {
                    "qc_lot_id": lot["id"],
                    "version": lot["version"],
                    "lot_no": lot["data"].get("lot_no"),
                    "lot_key": lot["data"].get("lot_key"),
                    "target": lot["data"].get("target"),
                    "sd": lot["data"].get("sd"),
                }
                if lot
                else {"qc_lot_id": None}
            ),
            "calibration": {
                "certificate_id": certificate["certificate_id"],
                "calibration_due": certificate["calibration_due"],
                "calibrated_at": certificate["calibrated_at"],
            },
            "qc_run": (
                {
                    "qc_run_id": run["id"],
                    "version": run["version"],
                    "value": run["data"].get("value"),
                }
                if run
                else {"qc_run_id": None}
            ),
        }
        return basis, gaps

    def _lookup_one(self, kind, field, value):
        if not value:
            return None
        rows = self._lookup(kind, field, value) or []
        return rows[0] if rows else None

    # --------------------------------------------------------------- queries
    def get(self, entity_id):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        return entity

    def list(self, kind=None, status=None, basis_status=None):
        if kind:
            kind = self.rules.normalize_kind(kind)
        items = self.repository.list_entities(kind=kind, status=status)
        if basis_status:
            items = [
                item for item in items if item["data"].get("basis_status") == basis_status
            ]
        return items

    def audit_log(self, entity_id=None):
        return self.repository.list_audit(entity_id=entity_id)
