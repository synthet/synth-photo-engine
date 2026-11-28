#!/usr/bin/env python3
"""Operator commands for spec 09's automatic selection rule (#867).

  dry-run      AC-8: what the rule would do for every current v1 bird run without a
               selection: the select count and the count per reason. Writes nothing.
  backfill     D5: apply the rule to those runs. Needs --confirmed (owner go).
  revoke       AC-9: revoke this rule's selections, optionally only those below a
               confidence. Dry run unless --confirmed.
  check-638    AC-12: fails (exit 1) unless a threshold is recorded and no active rule
               selection sits below it. Run before the embedded detector is turned off.

Usage (gpu-shell, project root):
  python scripts/maintenance/auto_select_selections.py dry-run
  python scripts/maintenance/auto_select_selections.py backfill --confirmed [--limit N]
  python scripts/maintenance/auto_select_selections.py revoke [--below-confidence 0.6] [--confirmed]
  python scripts/maintenance/auto_select_selections.py check-638
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from collections import Counter
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from modules import localization_auto_select as auto  # noqa: E402

log = logging.getLogger(__name__)

_CANDIDATES_SQL = """
SELECT r.id AS run_id, r.image_id, r.detector_key, r.detector_version, r.detector_config_hash,
       r.status, r.error_code,
       g.id AS region_id, g.confidence, g.x1, g.y1, g.x2, g.y2,
       s.selected_by, s.localization_run_id AS selected_run_id
FROM image_localization_runs r
LEFT JOIN image_regions g ON g.localization_run_id = r.id AND g.rank = 0
LEFT JOIN image_localization_selections s
       ON s.image_id = r.image_id AND s.detector_key = r.detector_key AND s.revoked_at IS NULL
WHERE r.detector_key = 'bird' AND r.is_current AND r.detector_version LIKE ?
ORDER BY r.image_id
"""
_V1_PATTERN = f"%{auto.V1_SUFFIX}"

_OWN_SQL = """
SELECT image_id, id, selected_by, (evidence->>'confidence')::float AS confidence
FROM image_localization_selections
WHERE detector_key = 'bird' AND revoked_at IS NULL AND selected_by LIKE ?
"""


def _split(row: dict) -> tuple[dict, dict | None, dict | None]:
    run = {k: row[k] for k in ("image_id", "detector_key", "detector_version", "detector_config_hash",
                               "status", "error_code")}
    rank0 = None if row.get("region_id") is None else {
        "id": row["region_id"], "confidence": row["confidence"],
        "x1": row["x1"], "y1": row["y1"], "x2": row["x2"], "y2": row["y2"]}
    active = None if row.get("selected_by") is None else {
        "selected_by": row["selected_by"], "localization_run_id": row["selected_run_id"]}
    if auto.is_own(active) and int(active["localization_run_id"]) != int(row["run_id"]):
        active = None  # apply() would revoke it as superseded
    return run, rank0, active


def dry_run(rows: list[dict], min_confidence: float | None) -> Counter:
    counts: Counter = Counter()
    for row in rows:
        run, rank0, active = _split(row)
        if auto.is_own(active):
            counts["already_selected"] += 1
            continue
        decision, reason = auto.evaluate(run, rank0, active, min_confidence=min_confidence)
        counts["select" if decision == "select" else (reason or "skipped")] += 1
    return counts


def check_638(threshold: float | None, own: list[dict]) -> tuple[bool, str]:
    if threshold is None:
        return False, "no threshold recorded (localization.auto_selection.min_confidence is unset)"
    below = [s for s in own if s["confidence"] is None or float(s["confidence"]) < threshold]
    if below:
        return False, f"{len(below)} active rule selections below {threshold}; revoke them first"
    return True, f"ok: {len(own)} active rule selections, none below {threshold}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="mode", required=True)
    sub.add_parser("dry-run")
    b = sub.add_parser("backfill")
    b.add_argument("--confirmed", action="store_true")
    b.add_argument("--limit", type=int, default=0)
    r = sub.add_parser("revoke")
    r.add_argument("--below-confidence", type=float)
    r.add_argument("--confirmed", action="store_true")
    sub.add_parser("check-638")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    from modules import db

    conn = db.get_connector()
    threshold = auto.settings()["min_confidence"]

    if args.mode == "dry-run":
        rows = conn.query(_CANDIDATES_SQL, (_V1_PATTERN,))
        print(json.dumps({"rule": auto.selected_by(threshold), "runs": len(rows),
                          "outcomes": dict(dry_run(rows, threshold))}, indent=1))
        return 0

    if args.mode == "backfill":
        if not args.confirmed:
            parser.error("backfill writes selections; pass --confirmed (owner go, spec 09 D5)")
        rows = [r for r in conn.query(_CANDIDATES_SQL, (_V1_PATTERN,)) if not auto.is_own(_split(r)[2])]
        if args.limit > 0:
            rows = rows[:args.limit]
        counts: Counter = Counter()
        for n, row in enumerate(rows, 1):
            run, _rank0, _active = _split(row)
            counts[conn.run_transaction(
                lambda tx, run=run, run_id=row["run_id"]: auto.apply(tx, run, run_id, min_confidence=threshold))] += 1
            if n % 1000 == 0 or n == len(rows):
                log.info("%d/%d %s", n, len(rows), dict(counts))
        return 0

    own = conn.query(_OWN_SQL, (f"{auto.RULE_ID}:%",))
    if args.mode == "check-638":
        ok, message = check_638(threshold, own)
        print(message)
        return 0 if ok else 1

    targets = [s for s in own if args.below_confidence is None
               or s["confidence"] is None or float(s["confidence"]) < args.below_confidence]
    print(f"{len(targets)} of {len(own)} active rule selections to revoke"
          + ("" if args.confirmed else " (dry run; pass --confirmed to revoke)"))
    if args.confirmed:
        from modules.localization_selection import revoke_selection

        def _revoke(tx, sel: dict) -> str:
            # revoke_selection has no selector guard: revoke only if the rule's row is still the active one.
            active = tx.query_one(
                "SELECT id FROM image_localization_selections "
                "WHERE image_id = ? AND detector_key = 'bird' AND revoked_at IS NULL FOR UPDATE",
                (int(sel["image_id"]),))
            if not active or int(active["id"]) != int(sel["id"]):
                return "changed_since_listing"
            return revoke_selection(int(sel["image_id"]), "auto_select_revoke", transaction=tx)["status"]

        statuses = Counter(conn.run_transaction(lambda tx, s=s: _revoke(tx, s)) for s in targets)
        print(dict(statuses))
    return 0


if __name__ == "__main__":
    sys.exit(main())
