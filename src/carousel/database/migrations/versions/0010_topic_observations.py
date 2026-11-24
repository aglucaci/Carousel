"""Topic observations from source count queries: radar, topic lineage key, source, status, query; append-only.

Revision ID: 0010
Revises: 0009
Create Date: 2026-11-25
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = '0010'
down_revision: Union[str, None] = '0009'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column('topic_observations', 'topic_id', existing_type=sa.BigInteger(), nullable=True)
    op.alter_column('topic_observations', 'count', existing_type=sa.Integer(), nullable=True)
    op.add_column('topic_observations', sa.Column('radar_id', sa.Text(), nullable=True))
    op.add_column('topic_observations', sa.Column('topic_key', sa.Text(), nullable=True))
    op.add_column('topic_observations', sa.Column('source', sa.String(length=16), nullable=True))
    op.add_column('topic_observations', sa.Column('status', sa.String(length=8), nullable=True))
    op.add_column('topic_observations', sa.Column('query', sa.Text(), nullable=True))
    op.add_column('topic_observations', sa.Column('cost', sa.Float(), nullable=True))
    op.add_column('topic_observations', sa.Column('error', sa.Text(), nullable=True))
    op.create_foreign_key(op.f('fk_topic_observations_radar_id_radars'), 'topic_observations', 'radars', ['radar_id'],
                          ['id'], ondelete='CASCADE')
    op.create_index(op.f('ix_topic_observations_radar_id'), 'topic_observations', ['radar_id'], unique=False)
    op.create_index('ix_topic_observations_series', 'topic_observations', ['radar_id', 'source', 'window_start'], unique=False)
    op.create_unique_constraint(
        op.f('uq_topic_observations_radar_id_classifier_version_topic_key_source_window_start_observed_at'),
        'topic_observations', ['radar_id', 'classifier_version', 'topic_key', 'source', 'window_start', 'observed_at'])


def downgrade() -> None:
    op.drop_constraint(op.f('uq_topic_observations_radar_id_classifier_version_topic_key_source_window_start_observed_at'),
                       'topic_observations', type_='unique')
    op.drop_index('ix_topic_observations_series', table_name='topic_observations')
    op.drop_index(op.f('ix_topic_observations_radar_id'), table_name='topic_observations')
    op.drop_constraint(op.f('fk_topic_observations_radar_id_radars'), 'topic_observations', type_='foreignkey')
    for col in ('error', 'cost', 'query', 'status', 'source', 'topic_key', 'radar_id'):
        op.drop_column('topic_observations', col)
    op.execute("DELETE FROM topic_observations WHERE topic_id IS NULL OR count IS NULL")
    op.alter_column('topic_observations', 'count', existing_type=sa.Integer(), nullable=False)
    op.alter_column('topic_observations', 'topic_id', existing_type=sa.BigInteger(), nullable=False)
