"""Preprint <-> publication links: a ``linked`` review decision, one paper_versions row per link and direction.

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-24
"""
from typing import Sequence, Union

from alembic import op

revision: str = '0004'
down_revision: Union[str, None] = '0003'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_constraint(op.f('ck_dedup_reviews_status'), 'dedup_reviews', type_='check')
    op.create_check_constraint(op.f('ck_dedup_reviews_status'), 'dedup_reviews',
                               "status IN ('pending', 'merged', 'distinct', 'linked', 'superseded')")
    op.create_unique_constraint(op.f('uq_paper_versions_paper_id_version_type_related_paper_id'), 'paper_versions',
                                ['paper_id', 'version_type', 'related_paper_id'])


def downgrade() -> None:
    op.drop_constraint(op.f('uq_paper_versions_paper_id_version_type_related_paper_id'), 'paper_versions', type_='unique')
    op.execute("UPDATE dedup_reviews SET status = 'distinct' WHERE status = 'linked'")
    op.drop_constraint(op.f('ck_dedup_reviews_status'), 'dedup_reviews', type_='check')
    op.create_check_constraint(op.f('ck_dedup_reviews_status'), 'dedup_reviews',
                               "status IN ('pending', 'merged', 'distinct', 'superseded')")
