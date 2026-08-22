"""Add is_favorite to task

Revision ID: 9c2f4a1b6d3e
Revises: 658a43b77ebb
Create Date: 2026-08-22 22:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '9c2f4a1b6d3e'
down_revision = '658a43b77ebb'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        'task',
        sa.Column(
            'is_favorite',
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )


def downgrade():
    op.drop_column('task', 'is_favorite')
