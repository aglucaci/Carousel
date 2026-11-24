"""Grounded extractions: claims that quote their passages, cached per paper, provider, model, schema, and input.

Revision ID: 0008
Revises: 0007
Create Date: 2026-11-24
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = '0008'
down_revision: Union[str, None] = '0007'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('paper_extractions',
    sa.Column('id', sa.BigInteger(), sa.Identity(always=False), nullable=False),
    sa.Column('paper_id', sa.Text(), nullable=False),
    sa.Column('provider', sa.String(length=32), nullable=False),
    sa.Column('requested_model', sa.Text(), nullable=False),
    sa.Column('model_version', sa.Text(), nullable=False),
    sa.Column('schema_version', sa.String(length=16), nullable=False),
    sa.Column('input_sha256', sa.String(length=64), nullable=False),
    sa.Column('location', sa.String(length=16), nullable=False),
    sa.Column('coverage', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('fields', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('passages', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('rejected', postgresql.JSONB(astext_type=sa.Text()), server_default='[]', nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint("location IN ('local', 'external')", name=op.f('ck_paper_extractions_location')),
    sa.ForeignKeyConstraint(['paper_id'], ['papers.id'], name=op.f('fk_paper_extractions_paper_id_papers'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_paper_extractions')),
    sa.UniqueConstraint('paper_id', 'provider', 'requested_model', 'schema_version', 'input_sha256',
                        name=op.f('uq_paper_extractions_paper_id_provider_requested_model_schema_version_input_sha256'))
    )
    op.create_index(op.f('ix_paper_extractions_paper_id'), 'paper_extractions', ['paper_id'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_paper_extractions_paper_id'), table_name='paper_extractions')
    op.drop_table('paper_extractions')
