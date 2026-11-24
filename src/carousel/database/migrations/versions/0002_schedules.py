"""Scheduled scans: radars.schedule is 'daily' | 'weekly' (was a free-form cron string), runs record their trigger.

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-24
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = '0002'
down_revision: Union[str, None] = '0001'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Nothing ever ran a cron schedule, so values other than daily/weekly carry no meaning; clear them.
    op.execute("UPDATE radars SET schedule = NULL WHERE schedule IS NOT NULL AND schedule NOT IN ('daily', 'weekly')")
    op.create_check_constraint(op.f('ck_radars_schedule'), 'radars', "schedule IN ('daily', 'weekly')")
    op.add_column('recommendation_runs', sa.Column('trigger', sa.String(length=16), nullable=True))
    op.create_check_constraint(op.f('ck_recommendation_runs_trigger'), 'recommendation_runs',
                               "trigger IN ('manual', 'scheduled')")


def downgrade() -> None:
    op.drop_constraint(op.f('ck_recommendation_runs_trigger'), 'recommendation_runs', type_='check')
    op.drop_column('recommendation_runs', 'trigger')
    op.drop_constraint(op.f('ck_radars_schedule'), 'radars', type_='check')
