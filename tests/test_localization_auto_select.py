"""Spec 09 (#867): the automatic selection rule. No DB, no GPU."""
from pathlib import Path

import pytest

from modules import localization as loc
from modules import localization_auto_select as auto

V1 = "synthet/bird-detect-v0/bird_detect_v1.pt"
V0 = "synthet/bird-detect-v0/bird_detect_v0.pt"


def _run(**kw):
    base = {"image_id": 7, "detector_key": "bird", "detector_version": V1, "status": "detected",
            "error_code": None, "detector_config_hash": "h"}
    base.update(kw)
    return base


def _region(conf=0.9, box=(0.1, 0.1, 0.5, 0.5), rid=11):
    return {"id": rid, "confidence": conf, "x1": box[0], "y1": box[1], "x2": box[2], "y2": box[3]}


# ---------------------------------------------------------------------------
# evaluate(): every branch of the rule
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("run,region,active,expected", [
    (_run(), _region(), None, ("select", None)),
    (_run(), _region(), {"selected_by": "v1_regate_rule/3:abc", "localization_run_id": 1},
     ("decline", "other_selection_active")),
    (_run(detector_version=V0), _region(), None, ("decline", "not_v1")),
    (_run(status="disabled", error_code="scene_route"), None, None, ("decline", "scene_route_skip")),
    (_run(status="no_detection"), None, None, ("decline", "no_detection")),
    (_run(), None, None, ("decline", "no_detection")),
    (_run(), _region(box=(0.0, 0.0, 0.05, 0.05)), None, ("decline", "below_min_area")),
    (_run(status="retryable_error"), None, None, ("skip", None)),
    (_run(status="terminal_error"), None, None, ("skip", None)),
    (_run(status="disabled", error_code=None), None, None, ("skip", None)),
])
def test_evaluate_branches(run, region, active, expected):
    assert auto.evaluate(run, region, active, min_confidence=None) == expected


def test_confidence_step_applies_only_when_a_threshold_is_recorded():
    low = _region(conf=0.3)
    assert auto.evaluate(_run(), low, None, min_confidence=None) == ("select", None)
    assert auto.evaluate(_run(), low, None, min_confidence=0.6) == ("decline", "below_confidence")
    assert auto.evaluate(_run(), _region(conf=0.6), None, min_confidence=0.6) == ("select", None)


def test_rule_sha_changes_with_the_threshold():
    assert auto.selected_by(None).startswith("auto_select_rule/1:")
    assert auto.selected_by(None) != auto.selected_by(0.6)
    assert auto.selected_by(0.6) == auto.selected_by(0.6)


# ---------------------------------------------------------------------------
# apply(): supersession, no replacement, recording, the select call
# ---------------------------------------------------------------------------

class _Tx:
    def __init__(self, active=None, region=None):
        self.active, self.region, self.sql = active, region, []

    def query_one(self, sql, params=None):
        self.sql.append(sql)
        if "image_localization_selections" in sql:
            return self.active
        if "image_regions" in sql:
            return self.region
        return None

    def execute(self, sql, params=None):
        self.sql.append((sql, params))
        return 1


@pytest.fixture
def calls(monkeypatch):
    out = {"select": [], "revoke": []}
    monkeypatch.setattr(auto, "select_region",
                        lambda image_id, region_id, selected_by, evidence=None, **kw:
                        out["select"].append((image_id, region_id, selected_by, evidence)) or {"status": "selected"})
    monkeypatch.setattr(auto, "revoke_selection",
                        lambda image_id, reason, **kw: out["revoke"].append((image_id, reason)) or {"status": "revoked"})
    return out


def _decisions(tx):
    return [p for s, p in (x for x in tx.sql if isinstance(x, tuple)) if "selection_decisions" in s]


def test_apply_selects_the_rank0_region_with_evidence(calls):
    tx = _Tx(region=_region(conf=0.91, rid=55))
    assert auto.apply(tx, _run(), 900, min_confidence=None) == "selected"
    image_id, region_id, selected_by, evidence = calls["select"][0]
    assert (image_id, region_id) == (7, 55) and selected_by == auto.selected_by(None)
    assert evidence["run_id"] == 900 and evidence["confidence"] == 0.91 and evidence["detector_version"] == V1
    assert _decisions(tx) == []


def test_apply_never_replaces_another_selector(calls):
    tx = _Tx(active={"selected_by": "v1_regate_rule/3:abc", "localization_run_id": 5}, region=_region())
    assert auto.apply(tx, _run(), 900, min_confidence=None) == "other_selection_active"
    assert calls["select"] == [] and calls["revoke"] == []
    assert _decisions(tx)[0][3] == "other_selection_active"


def test_apply_supersedes_its_own_selection_on_an_older_run(calls):
    own = {"selected_by": auto.selected_by(None), "localization_run_id": 800}
    tx = _Tx(active=own, region=_region(rid=56))
    assert auto.apply(tx, _run(), 900, min_confidence=None) == "selected"
    assert calls["revoke"] == [(7, "superseded")] and calls["select"][0][1] == 56


def test_apply_refuses_v0_and_records_the_reason(calls):
    tx = _Tx(region=_region())
    assert auto.apply(tx, _run(detector_version=V0), 900, min_confidence=None) == "not_v1"
    assert calls["select"] == [] and _decisions(tx)[0][3] == "not_v1"


def test_apply_records_nothing_for_an_error_run(calls):
    tx = _Tx()
    assert auto.apply(tx, _run(status="retryable_error"), 900, min_confidence=None) == "skipped"
    assert _decisions(tx) == [] and calls["select"] == []


def test_apply_records_a_non_selected_status_from_select_region(monkeypatch):
    monkeypatch.setattr(auto, "select_region", lambda *a, **k: {"status": "production_changed"})
    tx = _Tx(region=_region())
    assert auto.apply(tx, _run(), 900, min_confidence=None) == "production_changed"
    assert _decisions(tx)[0][3] == "production_changed"


# ---------------------------------------------------------------------------
# write_run hook: off switch (AC-6), publish=False (AC-5), failure isolation (AC-10)
# ---------------------------------------------------------------------------

class _WriteTx(_Tx):
    def execute_returning(self, sql, params=None):
        self.sql.append((sql, params))
        return [{"id": 900}]


class _Conn:
    def __init__(self):
        self.tx = _WriteTx()

    def run_transaction(self, cb):
        return cb(self.tx)


@pytest.fixture
def conn(monkeypatch):
    from modules import db
    c = _Conn()
    monkeypatch.setattr(db, "get_connector", lambda: c, raising=False)
    return c


def _write(publish=True):
    run = {col: None for col in loc._RUN_COLUMNS}
    run.update(_run(), is_current=publish)
    return loc.write_run(run, [{"conf": 0.9, "rank": 0, "region": (0.1, 0.1, 0.5, 0.5)}], publish=publish)


def test_hook_is_off_by_default(conn, monkeypatch):
    seen = []
    monkeypatch.setattr(auto, "apply", lambda *a, **k: seen.append(a) or "selected")
    monkeypatch.setattr(auto, "settings", lambda: {"enabled": False, "min_confidence": None})
    assert _write() == 900 and seen == []


def test_hook_runs_inside_a_savepoint_when_enabled(conn, monkeypatch):
    seen = []
    monkeypatch.setattr(auto, "apply", lambda tx, run, run_id, **k: seen.append(run_id) or "selected")
    monkeypatch.setattr(auto, "settings", lambda: {"enabled": True, "min_confidence": None})
    assert _write() == 900 and seen == [900]
    stmts = [s for s, _ in (x for x in conn.tx.sql if isinstance(x, tuple))]
    assert "SAVEPOINT auto_select" in stmts and "RELEASE SAVEPOINT auto_select" in stmts


def test_hook_skips_unpublished_runs(conn, monkeypatch):
    seen = []
    monkeypatch.setattr(auto, "apply", lambda *a, **k: seen.append(a) or "selected")
    monkeypatch.setattr(auto, "settings", lambda: {"enabled": True, "min_confidence": None})
    _write(publish=False)
    assert seen == []


def test_a_rule_failure_rolls_back_to_the_savepoint_and_keeps_the_run(conn, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("rule broke")
    monkeypatch.setattr(auto, "apply", boom)
    monkeypatch.setattr(auto, "settings", lambda: {"enabled": True, "min_confidence": None})
    assert _write() == 900
    stmts = [s for s, _ in (x for x in conn.tx.sql if isinstance(x, tuple))]
    assert "ROLLBACK TO SAVEPOINT auto_select" in stmts


# ---------------------------------------------------------------------------
# Operator commands: dry run (AC-8) and the #638 precheck (AC-12)
# ---------------------------------------------------------------------------

def _row(**kw):
    base = {"run_id": 900, "image_id": 7, "detector_key": "bird", "detector_version": V1,
            "detector_config_hash": "h", "status": "detected", "error_code": None,
            "region_id": 11, "confidence": 0.9, "x1": 0.1, "y1": 0.1, "x2": 0.5, "y2": 0.5,
            "selected_by": None, "selected_run_id": None}
    base.update(kw)
    return base


def test_dry_run_counts_outcomes_without_writing():
    from scripts.maintenance import auto_select_selections as cmd

    rows = [_row(), _row(image_id=8, status="no_detection", region_id=None),
            _row(image_id=9, selected_by="v1_regate_rule/3:abc", selected_run_id=5),
            _row(image_id=10, selected_by=auto.selected_by(None), selected_run_id=900),
            _row(image_id=11, selected_by=auto.selected_by(None), selected_run_id=800)]
    counts = cmd.dry_run(rows, None)
    assert counts == {"select": 2, "no_detection": 1, "other_selection_active": 1, "already_selected": 1}


def test_check_638_needs_a_threshold_and_no_selection_below_it():
    from scripts.maintenance import auto_select_selections as cmd

    own = [{"image_id": 1, "confidence": 0.9}, {"image_id": 2, "confidence": 0.5}]
    assert cmd.check_638(None, own)[0] is False
    assert cmd.check_638(0.6, own)[0] is False
    assert cmd.check_638(0.4, own)[0] is True


# ---------------------------------------------------------------------------
# AC-7: every lane reaches the rule through write_run
# ---------------------------------------------------------------------------

def test_write_run_is_the_only_writer_of_localization_runs():
    root = Path(__file__).resolve().parents[1]
    writers = []
    for base in ("modules", "scripts"):
        for path in (root / base).rglob("*.py"):
            if "research" in path.parts or "__pycache__" in path.parts:
                continue
            if "INSERT INTO image_localization_runs" in path.read_text(encoding="utf-8", errors="ignore"):
                writers.append(path.relative_to(root).as_posix())
    # The legacy importer writes only ``legacy_unversioned`` runs copied from images.bird_bbox,
    # which the rule would always decline as ``not_v1``; every detector lane uses write_run.
    assert sorted(writers) == ["modules/localization.py", "modules/localization_legacy.py"]
