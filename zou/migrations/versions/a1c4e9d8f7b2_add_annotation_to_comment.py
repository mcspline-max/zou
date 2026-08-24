"""Add annotation to comment

Revision ID: a1c4e9d8f7b2
Revises: 658a43b77ebb
Create Date: 2026-08-24 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision = 'a1c4e9d8f7b2'
down_revision = '658a43b77ebb'
branch_labels = None
depends_on = None


def upgrade():
    # Annotations move from a shared per-time bucket on preview_file to a
    # property of the comment that drew them. preview_file.annotations is
    # left in place (unused after this) as a historical backup for the
    # data the backfill command reads from.
    op.add_column(
        'comment',
        sa.Column(
            'annotation',
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
    )


def downgrade():
    op.drop_column('comment', 'annotation')
