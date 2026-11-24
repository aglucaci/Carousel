"""Research Delta events from the engine: the run and paper each event is about, its kind, and a stable key.

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-24
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = '0007'
down_revision: Union[str, None] = '0006'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('research_events', sa.Column('event_key', sa.String(length=32), nullable=True))
    op.add_column('research_events', sa.Column('kind', sa.String(length=32), nullable=True))
    op.add_column('research_events', sa.Column('run_id', sa.Text(), nullable=True))
    op.add_column('research_events', sa.Column('paper_id', sa.Text(), nullable=True))
    op.add_column('research_events', sa.Column('events_version', sa.String(length=16), nullable=True))
    op.create_unique_constraint(op.f('uq_research_events_event_key'), 'research_events', ['event_key'])
    op.create_foreign_key(op.f('fk_research_events_run_id_recommendation_runs'), 'research_events',
                          'recommendation_runs', ['run_id'], ['id'], ondelete='CASCADE')
    op.create_foreign_key(op.f('fk_research_events_paper_id_papers'), 'research_events', 'papers', ['paper_id'],
                          ['id'], ondelete='CASCADE')
    op.create_index(op.f('ix_research_events_run_id'), 'research_events', ['run_id'], unique=False)
    op.create_index(op.f('ix_research_events_paper_id'), 'research_events', ['paper_id'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_research_events_paper_id'), table_name='research_events')
    op.drop_index(op.f('ix_research_events_run_id'), table_name='research_events')
    op.drop_constraint(op.f('fk_research_events_paper_id_papers'), 'research_events', type_='foreignkey')
    op.drop_constraint(op.f('fk_research_events_run_id_recommendation_runs'), 'research_events', type_='foreignkey')
    op.drop_constraint(op.f('uq_research_events_event_key'), 'research_events', type_='unique')
    for col in ('events_version', 'paper_id', 'run_id', 'kind', 'event_key'):
        op.drop_column('research_events', col)
