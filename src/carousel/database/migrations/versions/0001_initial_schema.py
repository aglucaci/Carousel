"""Initial Phase 1B schema: spec §8 tables, engine audit tables, run snapshots.

Revision ID: 0001
Revises: 
Create Date: 2026-11-23 18:35:07.987989
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import pgvector.sqlalchemy
from sqlalchemy.dialects import postgresql

revision: str = '0001'
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.create_table('authors',
    sa.Column('id', sa.BigInteger(), sa.Identity(always=False), nullable=False),
    sa.Column('name', sa.Text(), nullable=False),
    sa.Column('orcid', sa.String(length=19), nullable=True),
    sa.Column('identity', sa.String(length=16), server_default='name_only', nullable=False),
    sa.CheckConstraint("identity IN ('orcid', 'name_only')", name=op.f('ck_authors_identity')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_authors')),
    sa.UniqueConstraint('orcid', name=op.f('uq_authors_orcid'))
    )
    op.create_index('uq_authors_name_only', 'authors', ['name'], unique=True, postgresql_where=sa.text('orcid IS NULL'))
    op.create_table('papers',
    sa.Column('id', sa.Text(), nullable=False),
    sa.Column('status', sa.String(length=16), server_default='active', nullable=False),
    sa.Column('merged_into', sa.Text(), nullable=True),
    sa.Column('title', sa.Text(), nullable=True),
    sa.Column('abstract', sa.Text(), nullable=True),
    sa.Column('venue', sa.Text(), nullable=True),
    sa.Column('language', sa.Text(), nullable=True),
    sa.Column('url', sa.Text(), nullable=True),
    sa.Column('publication_types', postgresql.JSONB(astext_type=sa.Text()), server_default='[]', nullable=False),
    sa.Column('is_retracted', sa.Boolean(), server_default='false', nullable=False),
    sa.Column('published', sa.String(length=10), nullable=True),
    sa.Column('first_online', sa.String(length=10), nullable=True),
    sa.Column('preprint_posted', sa.String(length=10), nullable=True),
    sa.Column('version_date', sa.String(length=10), nullable=True),
    sa.Column('source_updated', sa.String(length=10), nullable=True),
    sa.Column('earliest_public_date', sa.Date(), nullable=True),
    sa.Column('first_seen', sa.DateTime(timezone=True), nullable=True),
    sa.Column('last_verified', sa.DateTime(timezone=True), nullable=True),
    sa.Column('canonical', postgresql.JSONB(astext_type=sa.Text()), server_default='{}', nullable=False),
    sa.Column('search', postgresql.TSVECTOR(), sa.Computed("to_tsvector('english', coalesce(title, '') || ' ' || coalesce(abstract, ''))", persisted=True), nullable=False),
    sa.CheckConstraint("status IN ('active', 'merged')", name=op.f('ck_papers_status')),
    sa.ForeignKeyConstraint(['merged_into'], ['papers.id'], name=op.f('fk_papers_merged_into_papers'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_papers'))
    )
    op.create_index('ix_papers_earliest_public_date', 'papers', ['earliest_public_date'], unique=False)
    op.create_index('ix_papers_first_seen', 'papers', ['first_seen'], unique=False)
    op.create_index('ix_papers_merged_into', 'papers', ['merged_into'], unique=False)
    op.create_index('ix_papers_search', 'papers', ['search'], unique=False, postgresql_using='gin')
    op.create_table('run_snapshots',
    sa.Column('id', sa.BigInteger(), sa.Identity(always=False), nullable=False),
    sa.Column('radar_id', sa.Text(), nullable=False),
    sa.Column('captured_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('source', sa.String(length=32), nullable=False),
    sa.Column('ranking_version', sa.String(length=32), nullable=False),
    sa.Column('run_id', sa.Text(), nullable=True),
    sa.Column('window_days', sa.Integer(), nullable=True),
    sa.Column('window', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('n_items', sa.Integer(), nullable=False),
    sa.Column('payload_sha256', sa.String(length=64), nullable=False),
    sa.Column('path', sa.Text(), nullable=False),
    sa.Column('provenance', postgresql.JSONB(astext_type=sa.Text()), server_default='{}', nullable=False),
    sa.Column('payload', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_run_snapshots')),
    sa.UniqueConstraint('radar_id', 'captured_at', 'payload_sha256', name=op.f('uq_run_snapshots_radar_id_captured_at_payload_sha256'))
    )
    op.create_index(op.f('ix_run_snapshots_radar_id'), 'run_snapshots', ['radar_id'], unique=False)
    op.create_table('topics',
    sa.Column('id', sa.BigInteger(), sa.Identity(always=False), nullable=False),
    sa.Column('label', sa.Text(), nullable=False),
    sa.Column('classifier_version', sa.Text(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_topics'))
    )
    op.create_table('users',
    sa.Column('id', sa.Text(), nullable=False),
    sa.Column('display_name', sa.Text(), nullable=True),
    sa.Column('preferences', postgresql.JSONB(astext_type=sa.Text()), server_default='{}', nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_users'))
    )
    op.create_table('collections',
    sa.Column('id', sa.BigInteger(), sa.Identity(always=False), nullable=False),
    sa.Column('owner_id', sa.Text(), nullable=False),
    sa.Column('title', sa.Text(), nullable=False),
    sa.Column('visibility', sa.String(length=16), server_default='private', nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("visibility IN ('private', 'link', 'public')", name=op.f('ck_collections_visibility')),
    sa.ForeignKeyConstraint(['owner_id'], ['users.id'], name=op.f('fk_collections_owner_id_users'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_collections'))
    )
    op.create_index(op.f('ix_collections_owner_id'), 'collections', ['owner_id'], unique=False)
    op.create_table('dedup_reviews',
    sa.Column('paper_a', sa.Text(), nullable=False),
    sa.Column('paper_b', sa.Text(), nullable=False),
    sa.Column('kind', sa.String(length=32), nullable=False),
    sa.Column('score', sa.Float(), nullable=True),
    sa.Column('reasons', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('features', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('decided_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('decided_by', sa.Text(), nullable=True),
    sa.Column('note', sa.Text(), nullable=True),
    sa.CheckConstraint("status IN ('pending', 'merged', 'distinct', 'superseded')", name=op.f('ck_dedup_reviews_status')),
    sa.CheckConstraint('paper_a < paper_b', name=op.f('ck_dedup_reviews_ordered_pair')),
    sa.ForeignKeyConstraint(['decided_by'], ['users.id'], name=op.f('fk_dedup_reviews_decided_by_users'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['paper_a'], ['papers.id'], name=op.f('fk_dedup_reviews_paper_a_papers')),
    sa.ForeignKeyConstraint(['paper_b'], ['papers.id'], name=op.f('fk_dedup_reviews_paper_b_papers')),
    sa.PrimaryKeyConstraint('paper_a', 'paper_b', name=op.f('pk_dedup_reviews'))
    )
    op.create_index('ix_dedup_reviews_status', 'dedup_reviews', ['status'], unique=False)
    op.create_table('identifier_conflicts',
    sa.Column('identifier_type', sa.String(length=16), nullable=False),
    sa.Column('value', sa.Text(), nullable=False),
    sa.Column('claimed_by_paper_id', sa.Text(), nullable=False),
    sa.Column('provider', sa.String(length=16), nullable=False),
    sa.Column('provider_id', sa.Text(), nullable=False),
    sa.Column('owner_paper_id', sa.Text(), nullable=False),
    sa.Column('observed_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['claimed_by_paper_id'], ['papers.id'], name=op.f('fk_identifier_conflicts_claimed_by_paper_id_papers')),
    sa.ForeignKeyConstraint(['owner_paper_id'], ['papers.id'], name=op.f('fk_identifier_conflicts_owner_paper_id_papers')),
    sa.PrimaryKeyConstraint('identifier_type', 'value', 'claimed_by_paper_id', 'provider', 'provider_id', name=op.f('pk_identifier_conflicts'))
    )
    op.create_table('merge_log',
    sa.Column('id', sa.BigInteger(), sa.Identity(always=False), nullable=False),
    sa.Column('at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('action', sa.String(length=8), nullable=False),
    sa.Column('survivor', sa.Text(), nullable=False),
    sa.Column('other', sa.Text(), nullable=False),
    sa.Column('method', sa.String(length=16), nullable=False),
    sa.Column('score', sa.Float(), nullable=True),
    sa.Column('detail', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('source_log_id', sa.BigInteger(), nullable=True),
    sa.CheckConstraint("action IN ('merge', 'split')", name=op.f('ck_merge_log_action')),
    sa.ForeignKeyConstraint(['other'], ['papers.id'], name=op.f('fk_merge_log_other_papers')),
    sa.ForeignKeyConstraint(['survivor'], ['papers.id'], name=op.f('fk_merge_log_survivor_papers')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_merge_log')),
    sa.UniqueConstraint('at', 'action', 'survivor', 'other', name=op.f('uq_merge_log_at_action_survivor_other'))
    )
    op.create_table('paper_authors',
    sa.Column('paper_id', sa.Text(), nullable=False),
    sa.Column('position', sa.Integer(), nullable=False),
    sa.Column('author_id', sa.BigInteger(), nullable=False),
    sa.ForeignKeyConstraint(['author_id'], ['authors.id'], name=op.f('fk_paper_authors_author_id_authors'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['paper_id'], ['papers.id'], name=op.f('fk_paper_authors_paper_id_papers'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('paper_id', 'position', name=op.f('pk_paper_authors'))
    )
    op.create_index(op.f('ix_paper_authors_author_id'), 'paper_authors', ['author_id'], unique=False)
    op.create_table('paper_embeddings',
    sa.Column('paper_id', sa.Text(), nullable=False),
    sa.Column('model_version', sa.Text(), nullable=False),
    sa.Column('text_sha256', sa.String(length=64), nullable=False),
    sa.Column('dim', sa.Integer(), nullable=False),
    sa.Column('embedding', pgvector.sqlalchemy.vector.VECTOR(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['paper_id'], ['papers.id'], name=op.f('fk_paper_embeddings_paper_id_papers'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('paper_id', 'model_version', name=op.f('pk_paper_embeddings'))
    )
    op.create_table('paper_identifiers',
    sa.Column('identifier_type', sa.String(length=16), nullable=False),
    sa.Column('value', sa.Text(), nullable=False),
    sa.Column('paper_id', sa.Text(), nullable=False),
    sa.ForeignKeyConstraint(['paper_id'], ['papers.id'], name=op.f('fk_paper_identifiers_paper_id_papers'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('identifier_type', 'value', name=op.f('pk_paper_identifiers'))
    )
    op.create_index(op.f('ix_paper_identifiers_paper_id'), 'paper_identifiers', ['paper_id'], unique=False)
    op.create_table('paper_sources',
    sa.Column('provider', sa.String(length=16), nullable=False),
    sa.Column('provider_id', sa.Text(), nullable=False),
    sa.Column('paper_id', sa.Text(), nullable=False),
    sa.Column('first_seen', sa.DateTime(timezone=True), nullable=False),
    sa.Column('last_checked', sa.DateTime(timezone=True), nullable=False),
    sa.Column('content_changed_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('content_sha256', sa.String(length=64), nullable=False),
    sa.Column('record', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.ForeignKeyConstraint(['paper_id'], ['papers.id'], name=op.f('fk_paper_sources_paper_id_papers'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('provider', 'provider_id', name=op.f('pk_paper_sources'))
    )
    op.create_index(op.f('ix_paper_sources_paper_id'), 'paper_sources', ['paper_id'], unique=False)
    op.create_table('paper_topics',
    sa.Column('paper_id', sa.Text(), nullable=False),
    sa.Column('topic_id', sa.BigInteger(), nullable=False),
    sa.Column('confidence', sa.Float(), nullable=False),
    sa.ForeignKeyConstraint(['paper_id'], ['papers.id'], name=op.f('fk_paper_topics_paper_id_papers'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['topic_id'], ['topics.id'], name=op.f('fk_paper_topics_topic_id_topics'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('paper_id', 'topic_id', name=op.f('pk_paper_topics'))
    )
    op.create_table('paper_versions',
    sa.Column('id', sa.BigInteger(), sa.Identity(always=False), nullable=False),
    sa.Column('paper_id', sa.Text(), nullable=False),
    sa.Column('version_type', sa.String(length=16), nullable=False),
    sa.Column('related_paper_id', sa.Text(), nullable=True),
    sa.Column('source', sa.String(length=16), nullable=False),
    sa.Column('date', sa.String(length=10), nullable=True),
    sa.Column('details', postgresql.JSONB(astext_type=sa.Text()), server_default='{}', nullable=False),
    sa.CheckConstraint("version_type IN ('preprint', 'published', 'revision', 'correction', 'retraction')", name=op.f('ck_paper_versions_version_type')),
    sa.ForeignKeyConstraint(['paper_id'], ['papers.id'], name=op.f('fk_paper_versions_paper_id_papers'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['related_paper_id'], ['papers.id'], name=op.f('fk_paper_versions_related_paper_id_papers'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_paper_versions'))
    )
    op.create_index(op.f('ix_paper_versions_paper_id'), 'paper_versions', ['paper_id'], unique=False)
    op.create_table('radars',
    sa.Column('id', sa.Text(), nullable=False),
    sa.Column('owner_id', sa.Text(), nullable=True),
    sa.Column('title', sa.Text(), nullable=False),
    sa.Column('description', sa.Text(), server_default='', nullable=False),
    sa.Column('state', sa.String(length=16), server_default='active', nullable=False),
    sa.Column('schedule', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("state IN ('active', 'paused', 'archived')", name=op.f('ck_radars_state')),
    sa.ForeignKeyConstraint(['owner_id'], ['users.id'], name=op.f('fk_radars_owner_id_users'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_radars'))
    )
    op.create_table('snapshot_items',
    sa.Column('snapshot_id', sa.BigInteger(), nullable=False),
    sa.Column('position', sa.Integer(), nullable=False),
    sa.Column('section', sa.Text(), nullable=True),
    sa.Column('rank', sa.Integer(), nullable=True),
    sa.Column('title', sa.Text(), nullable=True),
    sa.Column('pmid', sa.Text(), nullable=True),
    sa.Column('doi', sa.Text(), nullable=True),
    sa.Column('score', sa.Float(), nullable=True),
    sa.Column('paper_id', sa.Text(), nullable=True),
    sa.Column('raw', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.ForeignKeyConstraint(['paper_id'], ['papers.id'], name=op.f('fk_snapshot_items_paper_id_papers')),
    sa.ForeignKeyConstraint(['snapshot_id'], ['run_snapshots.id'], name=op.f('fk_snapshot_items_snapshot_id_run_snapshots'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('snapshot_id', 'position', name=op.f('pk_snapshot_items'))
    )
    op.create_index(op.f('ix_snapshot_items_doi'), 'snapshot_items', ['doi'], unique=False)
    op.create_index(op.f('ix_snapshot_items_paper_id'), 'snapshot_items', ['paper_id'], unique=False)
    op.create_index(op.f('ix_snapshot_items_pmid'), 'snapshot_items', ['pmid'], unique=False)
    op.create_table('topic_observations',
    sa.Column('id', sa.BigInteger(), sa.Identity(always=False), nullable=False),
    sa.Column('topic_id', sa.BigInteger(), nullable=False),
    sa.Column('window_start', sa.Date(), nullable=False),
    sa.Column('window_end', sa.Date(), nullable=False),
    sa.Column('count', sa.Integer(), nullable=False),
    sa.Column('coverage', postgresql.JSONB(astext_type=sa.Text()), server_default='{}', nullable=False),
    sa.Column('classifier_version', sa.Text(), nullable=False),
    sa.Column('observed_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['topic_id'], ['topics.id'], name=op.f('fk_topic_observations_topic_id_topics'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_topic_observations'))
    )
    op.create_index(op.f('ix_topic_observations_topic_id'), 'topic_observations', ['topic_id'], unique=False)
    op.create_table('collection_papers',
    sa.Column('collection_id', sa.BigInteger(), nullable=False),
    sa.Column('paper_id', sa.Text(), nullable=False),
    sa.Column('notes', sa.Text(), server_default='', nullable=False),
    sa.Column('reading_status', sa.String(length=16), server_default='unread', nullable=False),
    sa.Column('added_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("reading_status IN ('unread', 'reading', 'read')", name=op.f('ck_collection_papers_reading_status')),
    sa.ForeignKeyConstraint(['collection_id'], ['collections.id'], name=op.f('fk_collection_papers_collection_id_collections'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['paper_id'], ['papers.id'], name=op.f('fk_collection_papers_paper_id_papers')),
    sa.PrimaryKeyConstraint('collection_id', 'paper_id', name=op.f('pk_collection_papers'))
    )
    op.create_table('digest_runs',
    sa.Column('id', sa.BigInteger(), sa.Identity(always=False), nullable=False),
    sa.Column('radar_id', sa.Text(), nullable=False),
    sa.Column('channel', sa.String(length=16), nullable=False),
    sa.Column('generated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('delivery_status', sa.String(length=16), nullable=False),
    sa.Column('details', postgresql.JSONB(astext_type=sa.Text()), server_default='{}', nullable=False),
    sa.ForeignKeyConstraint(['radar_id'], ['radars.id'], name=op.f('fk_digest_runs_radar_id_radars'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_digest_runs'))
    )
    op.create_index(op.f('ix_digest_runs_radar_id'), 'digest_runs', ['radar_id'], unique=False)
    op.create_table('radar_versions',
    sa.Column('id', sa.BigInteger(), sa.Identity(always=False), nullable=False),
    sa.Column('radar_id', sa.Text(), nullable=False),
    sa.Column('config_hash', sa.String(length=32), nullable=False),
    sa.Column('schema_version', sa.Integer(), nullable=True),
    sa.Column('config', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('effective_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['radar_id'], ['radars.id'], name=op.f('fk_radar_versions_radar_id_radars'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_radar_versions')),
    sa.UniqueConstraint('radar_id', 'config_hash', name=op.f('uq_radar_versions_radar_id_config_hash'))
    )
    op.create_table('research_events',
    sa.Column('id', sa.BigInteger(), sa.Identity(always=False), nullable=False),
    sa.Column('type', sa.String(length=32), nullable=False),
    sa.Column('radar_id', sa.Text(), nullable=False),
    sa.Column('date', sa.Date(), nullable=False),
    sa.Column('evidence', postgresql.JSONB(astext_type=sa.Text()), server_default='{}', nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['radar_id'], ['radars.id'], name=op.f('fk_research_events_radar_id_radars'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_research_events'))
    )
    op.create_index(op.f('ix_research_events_radar_id'), 'research_events', ['radar_id'], unique=False)
    op.create_table('user_feedback',
    sa.Column('id', sa.BigInteger(), sa.Identity(always=False), nullable=False),
    sa.Column('user_id', sa.Text(), nullable=True),
    sa.Column('radar_id', sa.Text(), nullable=True),
    sa.Column('paper_id', sa.Text(), nullable=False),
    sa.Column('action', sa.String(length=16), nullable=False),
    sa.Column('change', sa.String(length=8), server_default='added', nullable=False),
    sa.Column('ref', sa.Text(), nullable=True),
    sa.Column('origin', sa.String(length=16), server_default='app', nullable=False),
    sa.Column('at', sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint("action IN ('save', 'dismiss', 'relevant', 'irrelevant')", name=op.f('ck_user_feedback_action')),
    sa.CheckConstraint("change IN ('added', 'removed')", name=op.f('ck_user_feedback_change')),
    sa.ForeignKeyConstraint(['paper_id'], ['papers.id'], name=op.f('fk_user_feedback_paper_id_papers')),
    sa.ForeignKeyConstraint(['radar_id'], ['radars.id'], name=op.f('fk_user_feedback_radar_id_radars'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_user_feedback_user_id_users'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_user_feedback')),
    sa.UniqueConstraint('radar_id', 'paper_id', 'action', 'change', 'at', 'origin', name=op.f('uq_user_feedback_radar_id_paper_id_action_change_at_origin'))
    )
    op.create_index(op.f('ix_user_feedback_paper_id'), 'user_feedback', ['paper_id'], unique=False)
    op.create_index(op.f('ix_user_feedback_radar_id'), 'user_feedback', ['radar_id'], unique=False)
    op.create_index(op.f('ix_user_feedback_user_id'), 'user_feedback', ['user_id'], unique=False)
    op.create_table('recommendation_runs',
    sa.Column('id', sa.Text(), nullable=False),
    sa.Column('radar_id', sa.Text(), nullable=False),
    sa.Column('radar_version_id', sa.BigInteger(), nullable=True),
    sa.Column('executed_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('as_of', sa.Date(), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('config_hash', sa.String(length=32), nullable=False),
    sa.Column('ranking_version', sa.String(length=32), nullable=False),
    sa.Column('candidates_version', sa.String(length=32), nullable=False),
    sa.Column('model_version', sa.Text(), nullable=True),
    sa.Column('mode', sa.String(length=16), nullable=False),
    sa.Column('n_observed', sa.Integer(), nullable=False),
    sa.Column('n_candidates', sa.Integer(), nullable=False),
    sa.Column('n_items', sa.Integer(), nullable=False),
    sa.Column('coverage', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('ranking', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('notes', postgresql.JSONB(astext_type=sa.Text()), server_default='[]', nullable=False),
    sa.ForeignKeyConstraint(['radar_id'], ['radars.id'], name=op.f('fk_recommendation_runs_radar_id_radars'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['radar_version_id'], ['radar_versions.id'], name=op.f('fk_recommendation_runs_radar_version_id_radar_versions'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_recommendation_runs'))
    )
    op.create_index('ix_recommendation_runs_radar_executed', 'recommendation_runs', ['radar_id', 'executed_at'], unique=False)
    op.create_table('recommendation_items',
    sa.Column('run_id', sa.Text(), nullable=False),
    sa.Column('rank', sa.Integer(), nullable=False),
    sa.Column('paper_id', sa.Text(), nullable=False),
    sa.Column('score', sa.Float(), nullable=False),
    sa.Column('mmr', sa.Float(), nullable=False),
    sa.Column('features', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('explanation', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('flags', postgresql.JSONB(astext_type=sa.Text()), server_default='[]', nullable=False),
    sa.ForeignKeyConstraint(['paper_id'], ['papers.id'], name=op.f('fk_recommendation_items_paper_id_papers')),
    sa.ForeignKeyConstraint(['run_id'], ['recommendation_runs.id'], name=op.f('fk_recommendation_items_run_id_recommendation_runs'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('run_id', 'rank', name=op.f('pk_recommendation_items'))
    )
    op.create_index(op.f('ix_recommendation_items_paper_id'), 'recommendation_items', ['paper_id'], unique=False)
    op.create_table('run_observations',
    sa.Column('run_id', sa.Text(), nullable=False),
    sa.Column('provider', sa.String(length=16), nullable=False),
    sa.Column('provider_id', sa.Text(), nullable=False),
    sa.Column('paper_id', sa.Text(), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.ForeignKeyConstraint(['paper_id'], ['papers.id'], name=op.f('fk_run_observations_paper_id_papers')),
    sa.ForeignKeyConstraint(['run_id'], ['recommendation_runs.id'], name=op.f('fk_run_observations_run_id_recommendation_runs'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('run_id', 'provider', 'provider_id', name=op.f('pk_run_observations'))
    )
    op.create_index(op.f('ix_run_observations_paper_id'), 'run_observations', ['paper_id'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_run_observations_paper_id'), table_name='run_observations')
    op.drop_table('run_observations')
    op.drop_index(op.f('ix_recommendation_items_paper_id'), table_name='recommendation_items')
    op.drop_table('recommendation_items')
    op.drop_index('ix_recommendation_runs_radar_executed', table_name='recommendation_runs')
    op.drop_table('recommendation_runs')
    op.drop_index(op.f('ix_user_feedback_user_id'), table_name='user_feedback')
    op.drop_index(op.f('ix_user_feedback_radar_id'), table_name='user_feedback')
    op.drop_index(op.f('ix_user_feedback_paper_id'), table_name='user_feedback')
    op.drop_table('user_feedback')
    op.drop_index(op.f('ix_research_events_radar_id'), table_name='research_events')
    op.drop_table('research_events')
    op.drop_table('radar_versions')
    op.drop_index(op.f('ix_digest_runs_radar_id'), table_name='digest_runs')
    op.drop_table('digest_runs')
    op.drop_table('collection_papers')
    op.drop_index(op.f('ix_topic_observations_topic_id'), table_name='topic_observations')
    op.drop_table('topic_observations')
    op.drop_index(op.f('ix_snapshot_items_pmid'), table_name='snapshot_items')
    op.drop_index(op.f('ix_snapshot_items_paper_id'), table_name='snapshot_items')
    op.drop_index(op.f('ix_snapshot_items_doi'), table_name='snapshot_items')
    op.drop_table('snapshot_items')
    op.drop_table('radars')
    op.drop_index(op.f('ix_paper_versions_paper_id'), table_name='paper_versions')
    op.drop_table('paper_versions')
    op.drop_table('paper_topics')
    op.drop_index(op.f('ix_paper_sources_paper_id'), table_name='paper_sources')
    op.drop_table('paper_sources')
    op.drop_index(op.f('ix_paper_identifiers_paper_id'), table_name='paper_identifiers')
    op.drop_table('paper_identifiers')
    op.drop_table('paper_embeddings')
    op.drop_index(op.f('ix_paper_authors_author_id'), table_name='paper_authors')
    op.drop_table('paper_authors')
    op.drop_table('merge_log')
    op.drop_table('identifier_conflicts')
    op.drop_index('ix_dedup_reviews_status', table_name='dedup_reviews')
    op.drop_table('dedup_reviews')
    op.drop_index(op.f('ix_collections_owner_id'), table_name='collections')
    op.drop_table('collections')
    op.drop_table('users')
    op.drop_table('topics')
    op.drop_index(op.f('ix_run_snapshots_radar_id'), table_name='run_snapshots')
    op.drop_table('run_snapshots')
    op.drop_index('ix_papers_search', table_name='papers', postgresql_using='gin')
    op.drop_index('ix_papers_merged_into', table_name='papers')
    op.drop_index('ix_papers_first_seen', table_name='papers')
    op.drop_index('ix_papers_earliest_public_date', table_name='papers')
    op.drop_table('papers')
    op.drop_index('uq_authors_name_only', table_name='authors', postgresql_where=sa.text('orcid IS NULL'))
    op.drop_table('authors')
