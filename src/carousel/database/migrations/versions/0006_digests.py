"""Digests: recipients per radar, and each delivery linked to its run with a checked channel and status.

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-24
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = '0006'
down_revision: Union[str, None] = '0005'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('radars', sa.Column('digest_emails', postgresql.JSONB(astext_type=sa.Text()), server_default='[]',
                                      nullable=False))
    op.add_column('digest_runs', sa.Column('run_id', sa.Text(), nullable=True))
    op.create_foreign_key(op.f('fk_digest_runs_run_id_recommendation_runs'), 'digest_runs', 'recommendation_runs',
                          ['run_id'], ['id'], ondelete='SET NULL')
    op.create_index(op.f('ix_digest_runs_run_id'), 'digest_runs', ['run_id'], unique=False)
    op.create_check_constraint(op.f('ck_digest_runs_channel'), 'digest_runs', "channel IN ('email', 'rss', 'markdown')")
    op.create_check_constraint(op.f('ck_digest_runs_delivery_status'), 'digest_runs',
                               "delivery_status IN ('delivered', 'failed')")


def downgrade() -> None:
    op.drop_constraint(op.f('ck_digest_runs_delivery_status'), 'digest_runs', type_='check')
    op.drop_constraint(op.f('ck_digest_runs_channel'), 'digest_runs', type_='check')
    op.drop_index(op.f('ix_digest_runs_run_id'), table_name='digest_runs')
    op.drop_constraint(op.f('fk_digest_runs_run_id_recommendation_runs'), 'digest_runs', type_='foreignkey')
    op.drop_column('digest_runs', 'run_id')
    op.drop_column('radars', 'digest_emails')
