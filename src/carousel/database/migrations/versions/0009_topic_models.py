"""Topics v1: versioned topic models per radar, topic lineage keys, assignments, and refit mappings.

Revision ID: 0009
Revises: 0008
Create Date: 2026-11-25
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = '0009'
down_revision: Union[str, None] = '0008'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('topic_models',
    sa.Column('classifier_version', sa.Text(), nullable=False),
    sa.Column('radar_id', sa.Text(), nullable=False),
    sa.Column('embedder', sa.Text(), nullable=False),
    sa.Column('params', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('fitted_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('previous_version', sa.Text(), nullable=True),
    sa.Column('active', sa.Boolean(), nullable=False),
    sa.ForeignKeyConstraint(['radar_id'], ['radars.id'], name=op.f('fk_topic_models_radar_id_radars'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('classifier_version', name=op.f('pk_topic_models'))
    )
    op.create_index(op.f('ix_topic_models_radar_id'), 'topic_models', ['radar_id'], unique=False)
    op.add_column('topics', sa.Column('radar_id', sa.Text(), nullable=True))
    op.add_column('topics', sa.Column('topic_key', sa.Text(), nullable=True))
    op.add_column('topics', sa.Column('key_terms', postgresql.JSONB(astext_type=sa.Text()), server_default='[]', nullable=False))
    op.add_column('topics', sa.Column('size', sa.Integer(), nullable=True))
    op.add_column('topics', sa.Column('threshold', sa.Float(), nullable=True))
    op.create_foreign_key(op.f('fk_topics_radar_id_radars'), 'topics', 'radars', ['radar_id'], ['id'], ondelete='CASCADE')
    op.create_index(op.f('ix_topics_radar_id'), 'topics', ['radar_id'], unique=False)
    op.create_unique_constraint(op.f('uq_topics_classifier_version_topic_key'), 'topics', ['classifier_version', 'topic_key'])
    op.add_column('paper_topics', sa.Column('origin', sa.String(length=16), nullable=True))
    op.add_column('paper_topics', sa.Column('assigned_at', sa.DateTime(timezone=True), nullable=True))
    op.create_table('topic_mappings',
    sa.Column('id', sa.BigInteger(), sa.Identity(always=False), nullable=False),
    sa.Column('from_version', sa.Text(), nullable=True),
    sa.Column('to_version', sa.Text(), nullable=False),
    sa.Column('relation', sa.String(length=16), nullable=False),
    sa.Column('from_topic', sa.Text(), nullable=True),
    sa.Column('to_topic', sa.Text(), nullable=True),
    sa.Column('shared_members', sa.Integer(), nullable=False),
    sa.Column('centroid_similarity', sa.Float(), nullable=True),
    sa.CheckConstraint("relation IN ('same', 'split', 'merge', 'new', 'retired')", name=op.f('ck_topic_mappings_relation')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_topic_mappings')),
    sa.UniqueConstraint('to_version', 'relation', 'from_topic', 'to_topic',
                        name=op.f('uq_topic_mappings_to_version_relation_from_topic_to_topic'))
    )
    op.create_index(op.f('ix_topic_mappings_to_version'), 'topic_mappings', ['to_version'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_topic_mappings_to_version'), table_name='topic_mappings')
    op.drop_table('topic_mappings')
    op.drop_column('paper_topics', 'assigned_at')
    op.drop_column('paper_topics', 'origin')
    op.drop_constraint(op.f('uq_topics_classifier_version_topic_key'), 'topics', type_='unique')
    op.drop_index(op.f('ix_topics_radar_id'), table_name='topics')
    op.drop_constraint(op.f('fk_topics_radar_id_radars'), 'topics', type_='foreignkey')
    for col in ('threshold', 'size', 'key_terms', 'topic_key', 'radar_id'):
        op.drop_column('topics', col)
    op.drop_index(op.f('ix_topic_models_radar_id'), table_name='topic_models')
    op.drop_table('topic_models')
