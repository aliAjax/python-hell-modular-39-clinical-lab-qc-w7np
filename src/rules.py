from datetime import datetime, timezone

from .domain import ConflictError, InvalidTransition, PermissionDenied, ValidationError


def _iso_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _find_one(lookup, kind, field, value):
    if lookup is None:
        return None
    rows = lookup(kind, field, value) or []
    return rows[0] if rows else None


def calibration_is_valid(calibration_due, as_of):
    return str(calibration_due)[:10] >= str(as_of)[:10]


def evaluate_qc(history, value, target, sd, config=None):
    """Evaluate one QC value against numeric and multi-rule criteria."""
    config = dict(config or {})
    try:
        value = float(value)
        target = float(target)
        sd = float(sd)
    except (TypeError, ValueError):
        raise ValidationError("qc value, target and sd must be numeric")
    if sd <= 0:
        raise ValidationError("qc sd must be positive")
    limit = float(config.get("limit_sd", 3.0))
    bias_n = int(config.get("consecutive_n", 4))
    bias_sd = float(config.get("consecutive_sd", 1.0))
    trend_n = int(config.get("trend_n", 4))
    z_score = round((value - target) / sd, 4)
    flags = []
    if abs(z_score) > limit:
        flags.append("1_3s")
    values = [float(item) for item in history] + [value]
    if len(values) >= bias_n:
        window = values[-bias_n:]
        if all(item > target + bias_sd * sd for item in window):
            flags.append("bias_high")
        if all(item < target - bias_sd * sd for item in window):
            flags.append("bias_low")
    if len(values) >= trend_n:
        window = values[-trend_n:]
        if all(window[index] < window[index + 1] for index in range(len(window) - 1)):
            flags.append("trend_up")
        if all(window[index] > window[index + 1] for index in range(len(window) - 1)):
            flags.append("trend_down")
    passed = not flags
    return {
        "accepted": passed,
        "flags": flags,
        "z_score": z_score,
        "rule_snapshot": {
            "limit_sd": limit,
            "consecutive_n": bias_n,
            "consecutive_sd": bias_sd,
            "trend_n": trend_n,
        },
    }


def unrecovered_rejection(batches):
    return [batch for batch in batches if batch.get("status") == "intercepted"]


def _validate_assay(actor, data, lookup):
    try:
        low = float(data.get("allowed_low"))
        high = float(data.get("allowed_high"))
    except (TypeError, ValueError):
        raise ValidationError("allowed_low and allowed_high must be numeric")
    if low >= high:
        raise ValidationError("allowed_low must be less than allowed_high")
    rule_config = dict(data.get("rule_config") or {})
    return {
        "rule_config": rule_config,
        "rule_version": 1,
        "rule_revisions": [],
    }


def _validate_qc_lot(actor, data, lookup):
    if not _find_one(lookup, "assay", "id", data.get("assay_id")):
        raise ValidationError("assay does not exist")
    try:
        target = float(data.get("target"))
        sd = float(data.get("sd"))
    except (TypeError, ValueError):
        raise ValidationError("target and sd must be numeric")
    if sd <= 0:
        raise ValidationError("sd must be positive")
    duplicate = _find_one(lookup, "qc_lot", "lot_key", "%s:%s" % (data["assay_id"], data["lot_no"]))
    if duplicate:
        raise ConflictError("qc lot already exists for assay")
    return {"lot_key": "%s:%s" % (data["assay_id"], data["lot_no"]), "target": target, "sd": sd}


def _validate_instrument(actor, data, lookup):
    if not str(data.get("serial", "")).strip():
        raise ValidationError("instrument serial is required")
    return {
        "calibration_due": data.get("calibration_due"),
        "calibration_history": [],
    }


def _validate_qc_run(actor, data, lookup):
    assay = _find_one(lookup, "assay", "id", data.get("assay_id"))
    lot = _find_one(lookup, "qc_lot", "id", data.get("qc_lot_id"))
    instrument = _find_one(lookup, "instrument", "id", data.get("instrument_id"))
    if not assay or not lot or not instrument:
        raise ValidationError("assay, qc lot and instrument are required")
    if lot["data"].get("assay_id") != assay["id"]:
        raise ValidationError("qc lot does not belong to the assay")
    try:
        value = float(data.get("value"))
    except (TypeError, ValueError):
        raise ValidationError("qc result value must be numeric")
    return {"value": value}


def _validate_result_batch(actor, data, lookup):
    if not _find_one(lookup, "assay", "id", data.get("assay_id")):
        raise ValidationError("assay does not exist")
    if not _find_one(lookup, "instrument", "id", data.get("instrument_id")):
        raise ValidationError("instrument does not exist")
    if not _find_one(lookup, "qc_run", "id", data.get("qc_run_id")):
        raise ValidationError("qc run does not exist")
    if int(data.get("patient_count", 0)) < 0:
        raise ValidationError("patient_count cannot be negative")
    return {}


def _validate_evaluate(actor, entity, data, lookup):
    assay = _find_one(lookup, "assay", "id", entity["data"].get("assay_id"))
    lot = _find_one(lookup, "qc_lot", "id", entity["data"].get("qc_lot_id"))
    if not assay or not lot:
        raise ValidationError("assay or qc lot disappeared")
    previous = []
    for run in lookup("qc_run", "instrument_id", entity["data"].get("instrument_id")) or []:
        if run["id"] == entity["id"] or run["status"] not in ("accepted", "rejected"):
            continue
        if run["data"].get("qc_lot_id") != entity["data"].get("qc_lot_id"):
            continue
        if str(run["data"].get("run_at", "")) < str(entity["data"].get("run_at", "")):
            previous.append(run["data"]["value"])
    result = evaluate_qc(
        previous,
        entity["data"].get("value"),
        lot["data"].get("target"),
        lot["data"].get("sd"),
        assay["data"].get("rule_config"),
    )
    if not result["accepted"] and not data.get("reject_reason"):
        result["reject_reason"] = "quality control rule violation"
    result["_next_status"] = "accepted" if result["accepted"] else "rejected"
    return result


def _release_basis(entity, lookup, run=None):
    """Freeze the QC lot, calibration certificate and rule version in force at release."""
    data = entity["data"]
    assay = _find_one(lookup, "assay", "id", data.get("assay_id"))
    instrument = _find_one(lookup, "instrument", "id", data.get("instrument_id"))
    run = run or _find_one(lookup, "qc_run", "id", data.get("qc_run_id"))
    lot_id = data.get("qc_lot_id") or (run["data"].get("qc_lot_id") if run else None)
    lot = _find_one(lookup, "qc_lot", "id", lot_id)
    if not assay or not lot or not instrument or not run:
        raise ConflictError("assay, qc lot, instrument and qc run must all be present at release")
    if assay["status"] != "active":
        raise ConflictError("assay is not active")
    if lot["status"] != "active":
        raise ConflictError("qc lot is not active")
    if run["status"] != "accepted":
        raise ConflictError("result batch can only be released with an accepted QC run")
    if run["data"].get("assay_id") != assay["id"]:
        raise ConflictError("qc run belongs to another assay")
    if run["data"].get("instrument_id") != instrument["id"]:
        raise ConflictError("qc run belongs to another instrument")
    if instrument["status"] != "ready":
        raise ConflictError("instrument is not ready")
    if not calibration_is_valid(instrument["data"].get("calibration_due"), data.get("run_at")):
        raise ConflictError("instrument calibration is not valid at result time")
    certificate_id = instrument["data"].get("certificate_id")
    if not certificate_id:
        raise ConflictError("instrument has no calibration certificate on record")
    active_holds = []
    for batch in lookup("result_batch", "instrument_id", instrument["id"]) or []:
        if batch["id"] != entity["id"] and batch["status"] == "intercepted":
            active_holds.append(batch)
    if active_holds:
        raise ConflictError("an intercepted result batch must be resolved first")
    return {
        "captured_at": _iso_now(),
        "rules": {
            "rule_version": assay["data"].get("rule_version", 1),
            "rule_config": dict(assay["data"].get("rule_config") or {}),
        },
        "qc_lot": {
            "qc_lot_id": lot["id"],
            "version": lot["version"],
            "lot_no": lot["data"].get("lot_no"),
            "lot_key": lot["data"].get("lot_key"),
            "target": lot["data"].get("target"),
            "sd": lot["data"].get("sd"),
        },
        "calibration": {
            "certificate_id": certificate_id,
            "version": instrument["version"],
            "calibration_due": instrument["data"].get("calibration_due"),
            "calibrated_at": instrument["data"].get("calibrated_at"),
        },
        "qc_run": {
            "qc_run_id": run["id"],
            "version": run["version"],
            "value": run["data"].get("value"),
        },
    }


def _validate_release(actor, entity, data, lookup):
    basis = _release_basis(entity, lookup)
    return {"released_by": actor.user_id, "release_basis": basis, "basis_status": "valid"}


def _validate_revalidate(actor, entity, data, lookup):
    """Recompute a released batch whose evidence has been invalidated."""
    if entity["data"].get("basis_status") not in ("invalidated", "pending_review"):
        raise ConflictError("release basis is still valid; nothing to recompute")
    patch = {"revalidated_by": actor.user_id}
    candidate = dict(entity["data"])
    if data.get("replacement_run_id"):
        replacement = _find_one(lookup, "qc_run", "id", data.get("replacement_run_id"))
        if not replacement or replacement["status"] != "accepted":
            raise ValidationError("a replacement run must exist and be accepted")
        if replacement["data"].get("assay_id") != entity["data"].get("assay_id"):
            raise ValidationError("replacement run belongs to another assay")
        candidate["qc_lot_id"] = replacement["data"].get("qc_lot_id")
        candidate["qc_run_id"] = replacement["id"]
        patch["qc_lot_id"] = replacement["data"].get("qc_lot_id")
        patch["qc_run_id"] = replacement["id"]
    basis = _release_basis(dict(entity, data=candidate), lookup)
    history = list(entity["data"].get("basis_history") or [])
    history.append(
        {
            "type": "revalidate",
            "actor_id": actor.user_id,
            "at": _iso_now(),
            "note": data.get("note"),
            "previous_basis": entity["data"].get("release_basis"),
        }
    )
    patch["release_basis"] = basis
    patch["basis_status"] = "valid"
    patch["basis_flags"] = []
    patch["basis_history"] = history
    return patch


def _validate_calibrate(actor, entity, data, lookup):
    history = list(entity["data"].get("calibration_history") or [])
    history.append(
        {
            "certificate_id": data["certificate_id"],
            "calibration_due": data["calibration_due"],
            "calibrated_at": data.get("calibrated_at") or _iso_now(),
            "by": actor.user_id,
        }
    )
    return {
        "certificate_id": data["certificate_id"],
        "calibration_due": data["calibration_due"],
        "calibrated_at": data.get("calibrated_at") or _iso_now(),
        "calibration_history": history,
    }


def _validate_revise_rules(actor, entity, data, lookup):
    try:
        new_config = dict(data.get("rule_config") or {})
        for key in ("limit_sd", "consecutive_n", "consecutive_sd", "trend_n"):
            if key in new_config:
                new_config[key] = float(new_config[key])
    except (TypeError, ValueError):
        raise ValidationError("rule_config values must be numeric")
    revisions = list(entity["data"].get("rule_revisions") or [])
    next_version = int(entity["data"].get("rule_version", 1)) + 1
    revisions.append(
        {
            "rule_version": next_version,
            "rule_config": new_config,
            "reason": data.get("reason"),
            "revised_by": actor.user_id,
            "revised_at": data.get("revised_at") or _iso_now(),
        }
    )
    return {
        "rule_config": new_config,
        "rule_version": next_version,
        "rule_revisions": revisions,
    }


def _validate_qc_retest(actor, entity, data, lookup):
    replacement = _find_one(lookup, "qc_run", "id", data.get("replacement_run_id"))
    if not replacement or replacement["status"] != "accepted":
        raise ValidationError("a replacement run must exist and be accepted")
    if replacement["data"].get("assay_id") != entity["data"].get("assay_id"):
        raise ValidationError("replacement run belongs to another assay")
    return {"replacement_run_id": replacement["id"]}


def _validate_switch_lot(actor, entity, data, lookup):
    previous = _find_one(lookup, "qc_lot", "id", data.get("previous_lot_id"))
    if not previous or previous["status"] != "active":
        raise ValidationError("previous active lot is required")
    if previous["data"].get("assay_id") != entity["data"].get("assay_id"):
        raise ValidationError("lots must belong to the same assay")
    return {"replaces_lot_id": previous["id"], "switched_at": data.get("switched_at")}


def _validate_correct(actor, entity, data, lookup):
    if not data.get("reason"):
        raise ValidationError("correction reason is required")
    history = list(entity["data"].get("correction_history") or [])
    history.append({"actor_id": actor.user_id, "reason": data["reason"], "from_status": entity["status"]})
    return {"correction_history": history}


class RuleEngine:
    ALIASES = {
        "assays": "assay",
        "qc_lots": "qc_lot",
        "instruments": "instrument",
        "qc_runs": "qc_run",
        "result_batches": "result_batch",
    }
    INITIAL_STATUS = {
        "assay": "active",
        "qc_lot": "registered",
        "instrument": "ready",
        "qc_run": "pending",
        "result_batch": "waiting",
    }
    TRANSITIONS = {
        "assay": {
            "suspend": (("active",), "suspended"),
            "restore": (("suspended",), "active"),
            "revise_rules": (("active",), "active"),
        },
        "qc_lot": {
            "activate": (("registered", "suspended"), "active"),
            "switch_in": (("registered",), "active"),
            "suspend": (("active",), "suspended"),
            "retire": (("active", "suspended"), "retired"),
        },
        "instrument": {
            "calibrate": (("ready", "maintenance", "failed"), "ready"),
            "fail": (("ready",), "failed"),
            "maintain": (("ready", "failed"), "maintenance"),
            "restore": (("maintenance", "failed"), "ready"),
        },
        "qc_run": {
            "evaluate": (("pending", "accepted", "rejected"), "pending"),
            "retest": (("rejected",), "retesting"),
            "investigate": (("rejected",), "investigated"),
            "resolve": (("investigated", "retesting"), "resolved"),
            "correct": (("accepted", "rejected", "investigated", "resolved"), "pending"),
        },
        "result_batch": {
            "release": (("waiting",), "released"),
            "intercept": (("waiting",), "intercepted"),
            "retest": (("intercepted",), "waiting"),
            "investigate": (("intercepted",), "investigating"),
            "resolve": (("investigating",), "resolved"),
            "revalidate": (("released",), "released"),
            "correct": (("waiting", "intercepted", "investigating", "released", "resolved"), "waiting"),
        },
    }
    CREATE_REQUIRED = {
        "assay": ("name", "unit", "allowed_low", "allowed_high"),
        "qc_lot": ("assay_id", "lot_no", "target", "sd", "expires_at"),
        "instrument": ("name", "serial", "calibration_due"),
        "qc_run": ("assay_id", "qc_lot_id", "instrument_id", "value", "run_at"),
        "result_batch": ("assay_id", "instrument_id", "qc_run_id", "run_at", "patient_count"),
    }
    ACTION_REQUIRED = {
        ("assay", "suspend"): ("reason",),
        ("assay", "revise_rules"): ("rule_config",),
        ("qc_lot", "switch_in"): ("previous_lot_id", "switched_at"),
        ("qc_lot", "suspend"): ("reason",),
        ("qc_lot", "retire"): ("reason",),
        ("instrument", "calibrate"): ("calibration_due", "certificate_id"),
        ("instrument", "fail"): ("reason",),
        ("instrument", "maintain"): ("reason",),
        ("qc_run", "evaluate"): ("evaluated_by",),
        ("qc_run", "retest"): ("reason",),
        ("qc_run", "investigate"): ("reason",),
        ("qc_run", "resolve"): ("resolution",),
        ("qc_run", "correct"): ("reason", "value"),
        ("result_batch", "release"): ("reviewer_id",),
        ("result_batch", "intercept"): ("reason",),
        ("result_batch", "retest"): ("replacement_run_id", "reason"),
        ("result_batch", "investigate"): ("reason",),
        ("result_batch", "resolve"): ("resolution",),
        ("result_batch", "revalidate"): ("reviewer_id",),
        ("result_batch", "correct"): ("reason",),
    }
    CREATE_ROLES = {
        "assay": ("supervisor", "admin"),
        "qc_lot": ("supervisor", "admin"),
        "instrument": ("supervisor", "admin"),
        "qc_run": ("operator", "supervisor", "admin"),
        "result_batch": ("operator", "supervisor", "admin"),
    }
    ROLE_ACTIONS = {
        "suspend": ("supervisor", "admin"),
        "restore": ("supervisor", "admin"),
        "activate": ("supervisor", "admin"),
        "switch_in": ("supervisor", "admin"),
        "retire": ("supervisor", "admin"),
        "calibrate": ("supervisor", "admin"),
        "fail": ("operator", "supervisor", "admin"),
        "maintain": ("operator", "supervisor", "admin"),
        "evaluate": ("operator", "supervisor", "admin"),
        "retest": ("operator", "supervisor", "admin"),
        "investigate": ("supervisor", "admin"),
        "resolve": ("supervisor", "admin"),
        "correct": ("supervisor", "admin"),
        "revise_rules": ("supervisor", "admin"),
        "release": ("supervisor", "admin"),
        "revalidate": ("supervisor", "admin"),
        "intercept": ("operator", "supervisor", "admin"),
    }
    CUSTOM_CREATE = {
        "assay": _validate_assay,
        "qc_lot": _validate_qc_lot,
        "instrument": _validate_instrument,
        "qc_run": _validate_qc_run,
        "result_batch": _validate_result_batch,
    }
    CUSTOM_TRANSITIONS = {
        ("qc_run", "evaluate"): _validate_evaluate,
        ("result_batch", "release"): _validate_release,
        ("result_batch", "revalidate"): _validate_revalidate,
        ("result_batch", "retest"): _validate_qc_retest,
        ("qc_lot", "switch_in"): _validate_switch_lot,
        ("qc_run", "correct"): _validate_correct,
        ("result_batch", "correct"): _validate_correct,
        ("instrument", "calibrate"): _validate_calibrate,
        ("assay", "revise_rules"): _validate_revise_rules,
    }

    def normalize_kind(self, kind):
        return self.ALIASES.get(kind, kind)

    def initial_status(self, kind):
        kind = self.normalize_kind(kind)
        if kind not in self.INITIAL_STATUS:
            raise ValidationError("unknown kind: " + str(kind))
        return self.INITIAL_STATUS[kind]

    @staticmethod
    def _ensure_role(actor, allowed):
        if actor.role not in allowed:
            raise PermissionDenied("role %s is not allowed here" % actor.role)

    @staticmethod
    def _require(data, fields):
        for field in fields:
            value = data.get(field)
            if value is None or value == "" or value == [] or value == {}:
                raise ValidationError("missing required field: " + field)

    def validate_create(self, actor, kind, data, lookup=None):
        kind = self.normalize_kind(kind)
        if kind not in self.INITIAL_STATUS:
            raise ValidationError("unknown kind: " + str(kind))
        self._ensure_role(actor, self.CREATE_ROLES.get(kind, ("admin",)))
        self._require(data, self.CREATE_REQUIRED.get(kind, ()))
        custom = self.CUSTOM_CREATE.get(kind)
        return custom(actor, data, lookup) if custom else {}

    def validate_transition(self, actor, entity, action, data, lookup=None):
        kind = self.normalize_kind(entity["kind"])
        transition = self.TRANSITIONS.get(kind, {}).get(action)
        if not transition:
            raise InvalidTransition("unknown action %s for %s" % (action, kind))
        allowed_statuses, next_status = transition
        if entity["status"] not in allowed_statuses:
            raise InvalidTransition("cannot %s from status %s" % (action, entity["status"]))
        allowed_roles = self.ROLE_ACTIONS.get((kind, action), self.ROLE_ACTIONS.get(action, ("admin",)))
        self._ensure_role(actor, allowed_roles)
        self._require(data, self.ACTION_REQUIRED.get((kind, action), ()))
        custom = self.CUSTOM_TRANSITIONS.get((kind, action))
        extra = custom(actor, entity, data, lookup) if custom else {}
        if extra.get("_next_status"):
            next_status = extra.pop("_next_status")
        patch = dict(data)
        if extra:
            patch.update(extra)
        return next_status, tuple(allowed_statuses), patch
