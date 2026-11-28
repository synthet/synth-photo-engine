"""Automatic selection for new bird runs (spec 09, #867).

One versioned rule runs inside :func:`modules.localization.write_run`'s transaction, after a
published run and its regions are written. For a current v1 ``bird`` run it either selects the
rank-0 region through :func:`modules.localization_selection.select_region`, or records one reason
code in ``image_localization_selection_decisions``. It never replaces a selection written by
another selector, and it revokes its own selection when a newer run replaces the one behind it.

Off by default (``localization.auto_selection.enabled``). ``min_confidence`` is unset until the
owner records a threshold at #638 planning (spec 09 D3); until then there is no confidence step.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

from modules.localization_selection import revoke_selection, select_region

RULE_ID = "auto_select_rule/1"
V1_SUFFIX = "bird_detect_v1.pt"
#: The existing runtime rule's species area floor (localization_policy_approval.RUNTIME_RULE).
MIN_AREA = 0.005
DETECTOR_KEY = "bird"

REASONS = ("other_selection_active", "not_v1", "scene_route_skip", "no_detection",
           "below_min_area", "below_confidence")

_ACTIVE_SQL = (
    "SELECT id, localization_run_id, selected_by FROM image_localization_selections "
    "WHERE image_id = ? AND detector_key = ? AND revoked_at IS NULL"
)
_RANK0_SQL = (
    "SELECT id, confidence, x1, y1, x2, y2 FROM image_regions "
    "WHERE localization_run_id = ? AND rank = 0"
)
_DECISION_SQL = (
    "INSERT INTO image_localization_selection_decisions (localization_run_id, image_id, rule_sha, reason) "
    "VALUES (?, ?, ?, ?) ON CONFLICT (localization_run_id, rule_sha) DO NOTHING"
)


def settings() -> dict[str, Any]:
    """``localization.auto_selection``: ``enabled`` (default off) and ``min_confidence`` (default unset)."""
    from modules import config

    section = (config.get_config_section("localization") or {}).get("auto_selection") or {}
    threshold = section.get("min_confidence")
    return {"enabled": bool(section.get("enabled", False)),
            "min_confidence": float(threshold) if threshold is not None else None}


def rule_sha(min_confidence: float | None) -> str:
    """Hash of the rule's definition; a different threshold is a different rule version."""
    definition = {"id": RULE_ID, "detector_suffix": V1_SUFFIX, "min_area": MIN_AREA,
                  "min_confidence": min_confidence, "region": "rank0"}
    return hashlib.sha256(json.dumps(definition, sort_keys=True).encode()).hexdigest()[:16]


def selected_by(min_confidence: float | None) -> str:
    return f"{RULE_ID}:{rule_sha(min_confidence)}"


def is_own(selection: dict | None) -> bool:
    return bool(selection) and str(selection.get("selected_by") or "").startswith(f"{RULE_ID}:")


def _area(region: dict) -> float:
    return max(0.0, float(region["x2"]) - float(region["x1"])) * max(0.0, float(region["y2"]) - float(region["y1"]))


def evaluate(run: dict, rank0: dict | None, active: dict | None, *,
             min_confidence: float | None) -> tuple[str, str | None]:
    """Decide one run: ``("select", None)``, ``("decline", reason)``, or ``("skip", None)``.

    ``active`` is the image's active selection after the caller has revoked a superseded one of
    this rule's own. ``skip`` covers error runs and a disabled detector: nothing is recorded and
    the image is left for its next attempt.
    """
    status = run.get("status")
    if status in ("retryable_error", "terminal_error"):
        return "skip", None
    if status == "disabled" and run.get("error_code") != "scene_route":
        return "skip", None
    if active:
        return "decline", "other_selection_active"
    if not str(run.get("detector_version") or "").endswith(V1_SUFFIX):
        return "decline", "not_v1"
    if status == "disabled":
        return "decline", "scene_route_skip"
    if status == "no_detection" or rank0 is None:
        return "decline", "no_detection"
    if _area(rank0) < MIN_AREA:
        return "decline", "below_min_area"
    if min_confidence is not None and float(rank0["confidence"]) < min_confidence:
        return "decline", "below_confidence"
    return "select", None


def apply(tx, run: dict, run_id: int, *, min_confidence: float | None) -> str:
    """Run the rule for one just-written run inside ``tx``. Returns the outcome label."""
    image_id = int(run["image_id"])
    sha = rule_sha(min_confidence)
    active = tx.query_one(_ACTIVE_SQL, (image_id, DETECTOR_KEY))
    if is_own(active) and int(active["localization_run_id"]) != int(run_id):
        revoke_selection(image_id, "superseded", detector_key=DETECTOR_KEY, transaction=tx)
        active = None
    rank0 = tx.query_one(_RANK0_SQL, (int(run_id),))
    decision, reason = evaluate(run, rank0, active, min_confidence=min_confidence)
    if decision == "skip":
        return "skipped"
    if decision == "select":
        evidence = {"rule": RULE_ID, "rule_sha": sha, "run_id": int(run_id),
                    "detector_version": run.get("detector_version"),
                    "detector_config_hash": run.get("detector_config_hash"),
                    "confidence": float(rank0["confidence"]), "area": round(_area(rank0), 6)}
        result = select_region(image_id, int(rank0["id"]), selected_by(min_confidence), evidence,
                               detector_key=DETECTOR_KEY, transaction=tx)
        if result.get("status") == "selected":
            return "selected"
        reason = str(result.get("status"))
    reason = str(reason)
    tx.execute(_DECISION_SQL, (int(run_id), image_id, sha, reason))
    return reason
