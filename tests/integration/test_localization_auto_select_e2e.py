"""E2E: spec 09's automatic selection rule inside write_run (#867). ``pytest.mark.postgres``."""

from __future__ import annotations

import json
import uuid

import pytest

pytestmark = [pytest.mark.postgres]

from modules import db  # noqa: E402
from modules import localization_auto_select as auto  # noqa: E402
from modules.localization import write_run  # noqa: E402
from modules.localization_selection import active_selection, select_region  # noqa: E402

V1 = "synthet/bird-detect-v0/bird_detect_v1.pt"
V0 = "synthet/bird-detect-v0/bird_detect_v0.pt"
SENTINEL = {"detected": False}


@pytest.fixture(autouse=True)
def _postgres_clean(postgres_test_session, clean_postgres, monkeypatch):
    monkeypatch.setattr(auto, "settings", lambda: {"enabled": True, "min_confidence": None})
    yield


def _image() -> int:
    row = db.get_connector().execute_returning(
        "INSERT INTO images (file_path, bird_bbox) VALUES (?, ?::jsonb) RETURNING id",
        (f"/tmp/auto-{uuid.uuid4().hex}.jpg", json.dumps(SENTINEL)))
    return int(row[0]["id"])


def _run(image_id: int, *, version: str = V1, publish: bool = True, status: str = "detected",
         boxes=((0.1, 0.2, 0.5, 0.6, 0.9),)) -> int:
    run = {"image_id": image_id, "detector_key": "bird", "detector_version": version,
           "detector_config_hash": "h", "coord_space": "display_normalized", "display_width": 1000,
           "display_height": 500, "status": status, "is_retryable": False, "is_current": publish}
    regions = [{"conf": c, "rank": i, "region": (x1, y1, x2, y2)} for i, (x1, y1, x2, y2, c) in enumerate(boxes)]
    return write_run(run, regions if status == "detected" else [], publish=publish)


def _decisions(image_id: int) -> list[str]:
    rows = db.get_connector().query(
        "SELECT reason FROM image_localization_selection_decisions WHERE image_id = ? ORDER BY id", (image_id,))
    return [r["reason"] for r in rows]


def test_a_v1_run_gets_an_active_selection_in_the_same_write():
    iid = _image()
    run_id = _run(iid)
    sel = active_selection(iid)
    assert sel is not None and sel["selected_by"] == auto.selected_by(None)
    assert int(sel["localization_run_id"]) == run_id and sel["evidence"]["run_id"] == run_id
    assert _decisions(iid) == []


def test_a_v0_run_is_declined_and_recorded():
    iid = _image()
    _run(iid, version=V0)
    assert active_selection(iid) is None and _decisions(iid) == ["not_v1"]


def test_a_newer_run_supersedes_the_rules_own_selection():
    iid = _image()
    first = _run(iid)
    second = _run(iid, boxes=((0.2, 0.2, 0.6, 0.6, 0.95),))
    sel = active_selection(iid)
    assert int(sel["localization_run_id"]) == second != first
    revoked = db.get_connector().query_one(
        "SELECT revoked_reason FROM image_localization_selections WHERE image_id = ? AND localization_run_id = ?",
        (iid, first))
    assert revoked["revoked_reason"] == "superseded"


def test_another_selectors_selection_is_never_replaced():
    iid = _image()
    artifact = _run(iid, publish=False)
    region = db.get_connector().query_one(
        "SELECT id FROM image_regions WHERE localization_run_id = ? AND rank = 0", (artifact,))["id"]
    assert select_region(iid, int(region), "v1_regate_rule/3:abc")["status"] == "selected"
    _run(iid)
    assert active_selection(iid)["selected_by"] == "v1_regate_rule/3:abc"
    assert _decisions(iid) == ["other_selection_active"]


def test_no_detection_is_recorded_without_a_selection():
    iid = _image()
    _run(iid, status="no_detection")
    assert active_selection(iid) is None and _decisions(iid) == ["no_detection"]


def test_a_rule_failure_rolls_back_only_the_rule(monkeypatch):
    def half_then_fail(tx, run, run_id, **kw):
        tx.execute("INSERT INTO image_localization_selection_decisions (localization_run_id, image_id, rule_sha, "
                   "reason) VALUES (?, ?, 'x', 'partial')", (run_id, run["image_id"]))
        raise RuntimeError("rule broke")

    monkeypatch.setattr(auto, "apply", half_then_fail)
    iid = _image()
    run_id = _run(iid)
    current = db.get_connector().query_one(
        "SELECT id FROM image_localization_runs WHERE image_id = ? AND is_current", (iid,))
    assert int(current["id"]) == run_id
    assert _decisions(iid) == [] and active_selection(iid) is None
