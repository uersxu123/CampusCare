"""Add durable three-layer memory state and jobs."""

import sqlalchemy as sa
from alembic import op


revision = "0015_three_layer_memory_v3"
down_revision = "0014_five_intent_route_v3"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("user_memories") as batch:
        batch.add_column(sa.Column("origin", sa.String(length=32), nullable=False, server_default="EXPLICIT"))
        batch.add_column(sa.Column("version", sa.Integer(), nullable=False, server_default="1"))
        batch.add_column(sa.Column("updated_from_turn_id", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("evidence_message_ids_json", sa.Text(), nullable=False, server_default="[]"))
        batch.add_column(sa.Column("sensitivity", sa.String(length=32), nullable=False, server_default="NORMAL"))
        batch.add_column(sa.Column("visibility_scope", sa.String(length=32), nullable=False, server_default="ALL_AGENTS"))
        batch.add_column(sa.Column("index_status", sa.String(length=32), nullable=False, server_default="PENDING"))
        batch.add_column(sa.Column("memory_epoch", sa.Integer(), nullable=False, server_default="0"))
        batch.create_foreign_key(
            "fk_user_memories_updated_from_turn", "chat_turns", ["updated_from_turn_id"], ["id"]
        )
        batch.create_index("ix_user_memories_origin", ["origin"], unique=False)
        batch.create_index("ix_user_memories_updated_from_turn_id", ["updated_from_turn_id"], unique=False)
        batch.create_index("ix_user_memories_sensitivity", ["sensitivity"], unique=False)
        batch.create_index("ix_user_memories_visibility_scope", ["visibility_scope"], unique=False)
        batch.create_index("ix_user_memories_index_status", ["index_status"], unique=False)

    op.create_table(
        "conversation_episodes",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("public_id", sa.String(length=96), nullable=False),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("user_accounts.id"), nullable=False),
        sa.Column("session_id", sa.Integer(), sa.ForeignKey("chat_sessions.id"), nullable=False),
        sa.Column("source_start_message_id", sa.Integer(), sa.ForeignKey("chat_messages.id"), nullable=False),
        sa.Column("source_end_message_id", sa.Integer(), sa.ForeignKey("chat_messages.id"), nullable=False),
        sa.Column("summary_text", sa.Text(), nullable=False),
        sa.Column("facts_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("schema_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="ACTIVE"),
        sa.Column("index_status", sa.String(length=32), nullable=False, server_default="PENDING"),
        sa.Column("embedding_version", sa.String(length=160), nullable=False, server_default="unindexed"),
        sa.Column("is_tail", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint(
            "session_id", "source_start_message_id", "source_end_message_id", "schema_version",
            name="uq_conversation_episode_source_range",
        ),
    )
    op.create_index("ix_conversation_episodes_public_id", "conversation_episodes", ["public_id"], unique=True)
    op.create_index("ix_conversation_episodes_user_id", "conversation_episodes", ["user_id"])
    op.create_index("ix_conversation_episodes_session_id", "conversation_episodes", ["session_id"])
    op.create_index("ix_conversation_episodes_source_start_message_id", "conversation_episodes", ["source_start_message_id"])
    op.create_index("ix_conversation_episodes_source_end_message_id", "conversation_episodes", ["source_end_message_id"])
    op.create_index("ix_conversation_episodes_content_hash", "conversation_episodes", ["content_hash"])
    op.create_index("ix_conversation_episodes_status", "conversation_episodes", ["status"])
    op.create_index("ix_conversation_episodes_index_status", "conversation_episodes", ["index_status"])
    op.create_index("ix_conversation_episodes_is_tail", "conversation_episodes", ["is_tail"])
    op.create_index("ix_conversation_episodes_expires_at", "conversation_episodes", ["expires_at"])
    op.create_index(
        "ix_conversation_episodes_user_status_created", "conversation_episodes", ["user_id", "status", "created_at"]
    )
    op.create_index(
        "ix_conversation_episodes_session_range", "conversation_episodes",
        ["session_id", "source_start_message_id", "source_end_message_id"],
    )

    op.create_table(
        "user_profile_states",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("user_accounts.id"), nullable=False),
        sa.Column("profile_version", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("extracted_until_message_id", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("memory_epoch", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_user_profile_states_user_id", "user_profile_states", ["user_id"], unique=True)

    op.create_table(
        "memory_jobs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("public_id", sa.String(length=64), nullable=False),
        sa.Column("dedupe_key", sa.String(length=191), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("user_accounts.id"), nullable=False),
        sa.Column("session_id", sa.Integer(), sa.ForeignKey("chat_sessions.id"), nullable=True),
        sa.Column("turn_id", sa.Integer(), sa.ForeignKey("chat_turns.id"), nullable=True),
        sa.Column("target_message_id", sa.Integer(), sa.ForeignKey("chat_messages.id"), nullable=True),
        sa.Column("payload_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="PENDING"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="5"),
        sa.Column("run_after", sa.DateTime(), nullable=False),
        sa.Column("lease_owner", sa.String(length=128), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(), nullable=True),
        sa.Column("expected_version", sa.Integer(), nullable=True),
        sa.Column("memory_epoch", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_error", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
    )
    for name, columns, unique in (
        ("ix_memory_jobs_public_id", ["public_id"], True),
        ("ix_memory_jobs_dedupe_key", ["dedupe_key"], True),
        ("ix_memory_jobs_kind", ["kind"], False),
        ("ix_memory_jobs_user_id", ["user_id"], False),
        ("ix_memory_jobs_session_id", ["session_id"], False),
        ("ix_memory_jobs_turn_id", ["turn_id"], False),
        ("ix_memory_jobs_target_message_id", ["target_message_id"], False),
        ("ix_memory_jobs_status", ["status"], False),
        ("ix_memory_jobs_run_after", ["run_after"], False),
        ("ix_memory_jobs_lease_owner", ["lease_owner"], False),
        ("ix_memory_jobs_lease_expires_at", ["lease_expires_at"], False),
        ("ix_memory_jobs_ready", ["status", "run_after", "lease_expires_at"], False),
        ("ix_memory_jobs_scope", ["user_id", "session_id", "kind"], False),
    ):
        op.create_index(name, "memory_jobs", columns, unique=unique)


def downgrade():
    op.drop_table("memory_jobs")
    op.drop_table("user_profile_states")
    op.drop_table("conversation_episodes")
    with op.batch_alter_table("user_memories") as batch:
        batch.drop_index("ix_user_memories_index_status")
        batch.drop_index("ix_user_memories_visibility_scope")
        batch.drop_index("ix_user_memories_sensitivity")
        batch.drop_index("ix_user_memories_updated_from_turn_id")
        batch.drop_index("ix_user_memories_origin")
        batch.drop_constraint("fk_user_memories_updated_from_turn", type_="foreignkey")
        batch.drop_column("memory_epoch")
        batch.drop_column("index_status")
        batch.drop_column("visibility_scope")
        batch.drop_column("sensitivity")
        batch.drop_column("evidence_message_ids_json")
        batch.drop_column("updated_from_turn_id")
        batch.drop_column("version")
        batch.drop_column("origin")
