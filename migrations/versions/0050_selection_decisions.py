"""Add the automatic-selection decision table (spec 09, #867)."""
from alembic import op

revision = "0050"
down_revision = "0049"
branch_labels = None
depends_on = None

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


def upgrade():
    op.get_bind().exec_driver_sql(SELECTION_DECISIONS_SCHEMA_SQL)


def downgrade():
    op.get_bind().exec_driver_sql("DROP TABLE IF EXISTS image_localization_selection_decisions")
