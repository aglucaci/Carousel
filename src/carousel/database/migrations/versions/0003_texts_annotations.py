"""Full texts kept under a reusable licence, with that licence, and text-mined annotations (Europe PMC).

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-24
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = '0003'
down_revision: Union[str, None] = '0002'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('paper_texts',
    sa.Column('source', sa.String(length=16), nullable=False),
    sa.Column('source_id', sa.Text(), nullable=False),
    sa.Column('kind', sa.String(length=16), nullable=False),
    sa.Column('paper_id', sa.Text(), nullable=False),
    sa.Column('format', sa.String(length=16), nullable=False),
    sa.Column('license', sa.Text(), nullable=True),
    sa.Column('license_url', sa.Text(), nullable=True),
    sa.Column('license_source', sa.String(length=16), nullable=False),
    sa.Column('reuse', sa.String(length=16), nullable=False),
    sa.Column('url', sa.Text(), nullable=True),
    sa.Column('sha256', sa.String(length=64), nullable=False),
    sa.Column('sections', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('retrieved_at', sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint("license_source IN ('text', 'metadata')", name=op.f('ck_paper_texts_license_source')),
    sa.CheckConstraint("reuse IN ('open', 'noncommercial', 'no_derivatives', 'restricted', 'unknown')", name=op.f('ck_paper_texts_reuse')),
    sa.ForeignKeyConstraint(['paper_id'], ['papers.id'], name=op.f('fk_paper_texts_paper_id_papers'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('source', 'source_id', 'kind', name=op.f('pk_paper_texts'))
    )
    op.create_index(op.f('ix_paper_texts_paper_id'), 'paper_texts', ['paper_id'], unique=False)
    op.create_table('paper_annotations',
    sa.Column('source', sa.String(length=16), nullable=False),
    sa.Column('article_id', sa.Text(), nullable=False),
    sa.Column('paper_id', sa.Text(), nullable=False),
    sa.Column('annotations', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('retrieved_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['paper_id'], ['papers.id'], name=op.f('fk_paper_annotations_paper_id_papers'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('source', 'article_id', name=op.f('pk_paper_annotations'))
    )
    op.create_index(op.f('ix_paper_annotations_paper_id'), 'paper_annotations', ['paper_id'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_paper_annotations_paper_id'), table_name='paper_annotations')
    op.drop_table('paper_annotations')
    op.drop_index(op.f('ix_paper_texts_paper_id'), table_name='paper_texts')
    op.drop_table('paper_texts')
