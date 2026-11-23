"""Automatic-selection decline reasons (spec 09, #867); mirrored in migration 0050."""

SELECTION_DECISIONS_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS image_localization_selection_decisions (
    id BIGSERIAL PRIMARY KEY,
    localization_run_id BIGINT NOT NULL REFERENCES image_localization_runs(id) ON DELETE CASCADE,
    image_id INTEGER NOT NULL REFERENCES images(id) ON DELETE CASCADE,
    rule_sha TEXT NOT NULL,
    reason TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (localization_run_id, rule_sha)
);
CREATE INDEX IF NOT EXISTS ix_ilsd_image ON image_localization_selection_decisions (image_id);
"""


def initialize_selection_decisions_schema(cur):
    cur.execute(SELECTION_DECISIONS_SCHEMA_SQL)
