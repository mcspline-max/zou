"""Add auto_advance to playlist

Revision ID: 658a43b77ebb
Revises: 2916a7e52eab
Create Date: 2026-08-22 16:45:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '658a43b77ebb'
down_revision = '2916a7e52eab'
branch_labels = None
depends_on = None


def upgrade():
    # server_default backfills every existing playlist as on, matching
    # current behavior (auto-advance to the next entry on playback end)
    # until a studio opts a playlist out explicitly.
    op.add_column(
        'playlist',
        sa.Column(
            'auto_advance',
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
    )


def downgrade():
    op.drop_column('playlist', 'auto_advance')
