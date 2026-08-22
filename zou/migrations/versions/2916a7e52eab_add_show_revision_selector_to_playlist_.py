"""Add show_revision_selector to playlist_share_link

Revision ID: 2916a7e52eab
Revises: 01b6bba78bc0
Create Date: 2026-08-22 02:14:11.647506

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '2916a7e52eab'
down_revision = '01b6bba78bc0'
branch_labels = None
depends_on = None


def upgrade():
    # server_default backfills existing links as off, matching current
    # behavior (single pinned revision, no switcher) until a studio opts a
    # link in explicitly.
    op.add_column(
        'playlist_share_link',
        sa.Column(
            'show_revision_selector',
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )


def downgrade():
    op.drop_column('playlist_share_link', 'show_revision_selector')
