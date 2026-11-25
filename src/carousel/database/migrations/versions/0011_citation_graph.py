"""Citation graph: append-only citation edges and reference-list reads (plan task 32).

Revision ID: 0011
Revises: 0010
Create Date: 2026-11-25
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = '0011'
down_revision: Union[str, None] = '0010'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'citation_edges',
        sa.Column('id', sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column('citing', sa.Text(), nullable=False),
        sa.Column('cited', sa.Text(), nullable=False),
        sa.Column('source', sa.String(length=16), nullable=False),
        sa.Column('observed_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_citation_edges')),
        sa.UniqueConstraint('citing', 'cited', 'source', name=op.f('uq_citation_edges_citing_cited_source')),
    )
    op.create_index(op.f('ix_citation_edges_cited'), 'citation_edges', ['cited'], unique=False)
    op.create_table(
        'reference_lists',
        sa.Column('id', sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column('work_id', sa.Text(), nullable=False),
        sa.Column('source', sa.String(length=16), nullable=False),
        sa.Column('n_refs', sa.Integer(), nullable=False),
        sa.Column('observed_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_reference_lists')),
        sa.UniqueConstraint('work_id', 'source', 'observed_at', name=op.f('uq_reference_lists_work_id_source_observed_at')),
    )
    op.create_index(op.f('ix_reference_lists_work_id'), 'reference_lists', ['work_id'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_reference_lists_work_id'), table_name='reference_lists')
    op.drop_table('reference_lists')
    op.drop_index(op.f('ix_citation_edges_cited'), table_name='citation_edges')
    op.drop_table('citation_edges')
