"""add timecode to comment

Revision ID: 01b6bba78bc0
Revises: b7d419c25e08
Create Date: 2026-08-21
"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = '01b6bba78bc0'
down_revision = 'b7d419c25e08'
branch_labels = None
depends_on = None

def upgrade():
    op.add_column('comment', sa.Column('timecode', sa.Float(), nullable=True))

def downgrade():
    op.drop_column('comment', 'timecode')
