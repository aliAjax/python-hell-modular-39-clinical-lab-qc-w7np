from uuid import uuid4

from .audit import AuditTrail
from .domain import ConflictError, NotFoundError, PermissionDenied
from .repository import utcnow
from .rules import RuleEngine


class DomainService:
    def __init__(self, repository, rules=None):
        self.repository = repository
        self.rules = rules or RuleEngine()
        self.audit = AuditTrail(repository)

    def _lookup(self, kind, field, value):
        return self.repository.find_entities(self.rules.normalize_kind(kind), field, value)

    def health(self):
        return {"status": "ok" if self.repository.ping() else "error"}

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

    def transition(self, actor, entity_id, action, data=None, expected_version=None):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        expected = int(expected_version) if expected_version is not None else entity["version"]
        next_status, patch = self.rules.validate_transition(
            actor, entity, action, dict(data or {}), self._lookup
        )
        merged = dict(entity["data"])
        merged.update(patch)
        updated = self.repository.update_entity(entity_id, expected, next_status, merged)
        self.audit.record(
            entity_id,
            actor,
            action,
            entity["status"],
            updated["status"],
            {"patch": patch},
        )
        self._invalidate_for(updated, action, actor)
        return updated

    def get(self, entity_id):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        return entity

    def list(self, kind=None, status=None):
        if kind:
            kind = self.rules.normalize_kind(kind)
        return self.repository.list_entities(kind=kind, status=status)

    def audit_log(self, entity_id=None):
        return self.repository.list_audit(entity_id=entity_id)

    # ------------------------------------------------------------------
    # 依据变更与失效 (basis change invalidation)
    # ------------------------------------------------------------------

    def _invalidate_for(self, entity, action, actor):
        kind = entity["kind"]
        if kind == "qc_lot":
            if action == "switch_in":
                self._mark_affected_lot(
                    entity["data"].get("replaces_lot_id"), "qc_lot_switched", actor
                )
            elif action in ("retire", "suspend"):
                self._mark_affected_lot(entity["id"], "qc_lot_" + action, actor)
        elif kind == "instrument" and action == "calibrate":
            self._mark_affected_instrument(
                entity["id"], entity["data"].get("certificate_id"), actor
            )
        elif kind == "assay" and action == "update_rules":
            self._mark_affected_rules(
                entity["id"], entity["data"].get("rule_version"), actor
            )

    def _mark_affected_lot(self, lot_id, reason, actor):
        if not lot_id:
            return
        for batch in self.repository.list_entities(kind="result_batch", status="released"):
            basis = batch["data"].get("release_basis") or {}
            if (basis.get("qc_lot") or {}).get("id") == lot_id:
                self._set_affected(batch, reason, actor)

    def _mark_affected_instrument(self, instrument_id, new_certificate_id, actor):
        for batch in self.repository.list_entities(kind="result_batch", status="released"):
            if batch["data"].get("instrument_id") != instrument_id:
                continue
            basis = batch["data"].get("release_basis") or {}
            certificate = (basis.get("calibration") or {}).get("certificate_id")
            if certificate != new_certificate_id:
                self._set_affected(batch, "calibration_recalibrated", actor)

    def _mark_affected_rules(self, assay_id, new_rule_version, actor):
        try:
            new_version = int(new_rule_version)
        except (TypeError, ValueError):
            return
        for batch in self.repository.list_entities(kind="result_batch", status="released"):
            if batch["data"].get("assay_id") != assay_id:
                continue
            basis = batch["data"].get("release_basis") or {}
            version = int((basis.get("rules") or {}).get("rule_version", 1))
            if version < new_version:
                self._set_affected(batch, "rules_updated", actor)

    def _set_affected(self, batch, reason, actor):
        for _ in range(3):
            fresh = self.repository.get_entity(batch["id"])
            if not fresh or fresh["status"] != "released":
                return
            data = dict(fresh["data"])
            data["affected_by"] = actor.user_id
            data["affected_reason"] = reason
            data["affected_at"] = utcnow()
            try:
                self.repository.update_entity(fresh["id"], fresh["version"], "affected", data)
            except ConflictError:
                continue
            self.audit.record(
                fresh["id"], actor, "invalidate", "released", "affected", {"reason": reason}
            )
            return

    # ------------------------------------------------------------------
    # 历史依据回填 (backfill release basis for legacy records)
    # ------------------------------------------------------------------

    def backfill_basis(self, actor):
        if actor.role not in ("supervisor", "admin"):
            raise PermissionDenied("only supervisor or admin can backfill release basis")
        processed = 0
        complete = 0
        pending = 0
        for batch in self.repository.list_entities(kind="result_batch"):
            if batch["data"].get("release_basis"):
                continue
            if batch["status"] not in ("released", "affected"):
                continue
            basis, review_status = self._reconstruct_basis(batch)
            data = dict(batch["data"])
            data["release_basis"] = basis
            data["review_status"] = review_status
            for _ in range(3):
                fresh = self.repository.get_entity(batch["id"])
                if not fresh:
                    break
                try:
                    self.repository.update_entity(fresh["id"], fresh["version"], fresh["status"], data)
                    break
                except ConflictError:
                    continue
            self.audit.record(
                batch["id"],
                actor,
                "backfill",
                batch["status"],
                batch["status"],
                {"review_status": review_status},
            )
            processed += 1
            if review_status == "complete":
                complete += 1
            else:
                pending += 1
        return {"processed": processed, "complete": complete, "pending_review": pending}

    def _reconstruct_basis(self, batch):
        run = self.repository.get_entity(batch["data"].get("qc_run_id"))
        lot = run and self.repository.get_entity(run["data"].get("qc_lot_id"))
        instrument = self.repository.get_entity(batch["data"].get("instrument_id"))
        assay = self.repository.get_entity(batch["data"].get("assay_id"))
        run_at = batch["data"].get("run_at") or batch["created_at"]

        lot_snapshot = None
        if lot:
            lot_snapshot = {
                "id": lot["id"],
                "lot_no": lot["data"].get("lot_no"),
                "target": lot["data"].get("target"),
                "sd": lot["data"].get("sd"),
            }

        calibration_snapshot = None
        calibration_history = (instrument or {}).get("data", {}).get("calibration_history") or []
        active_calibration = self._active_at(calibration_history, "calibrated_at", run_at)
        if active_calibration and active_calibration.get("certificate_id"):
            calibration_snapshot = {
                "certificate_id": active_calibration["certificate_id"],
                "calibration_due": active_calibration.get("calibration_due"),
            }

        rules_snapshot = None
        rule_history = (assay or {}).get("data", {}).get("rule_history") or []
        active_rules = self._active_at(rule_history, "changed_at", run_at)
        if active_rules:
            rules_snapshot = {
                "rule_version": active_rules.get("version"),
                "rule_config": active_rules.get("rule_config"),
            }

        basis = {
            "qc_lot": lot_snapshot,
            "calibration": calibration_snapshot,
            "rules": rules_snapshot,
            "released_at": run_at,
            "backfilled": True,
        }
        complete = bool(lot_snapshot and calibration_snapshot and rules_snapshot)
        return basis, "complete" if complete else "pending_review"

    @staticmethod
    def _active_at(history, timestamp_field, as_of):
        candidates = [
            entry
            for entry in history
            if str(entry.get(timestamp_field, "")) <= str(as_of)
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda entry: str(entry.get(timestamp_field, "")))
