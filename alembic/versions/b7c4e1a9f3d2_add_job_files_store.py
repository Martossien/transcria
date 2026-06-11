"""add job files store (split web/worker sans filesystem partagé)

Revision ID: b7c4e1a9f3d2
Revises: 8f2b6d0e4a1c
Create Date: 2026-06-11 10:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "b7c4e1a9f3d2"
down_revision = "8f2b6d0e4a1c"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "job_files",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("job_id", sa.String(length=36), nullable=False),
        sa.Column("relpath", sa.String(length=512), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("chunk_count", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["job_id"], ["jobs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("job_id", "relpath", name="uq_job_files_job_relpath"),
    )
    op.create_index("ix_job_files_job_id", "job_files", ["job_id"])

    op.create_table(
        "job_file_chunks",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("file_id", sa.Integer(), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("data", sa.LargeBinary(), nullable=False),
        sa.ForeignKeyConstraint(["file_id"], ["job_files.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("file_id", "seq", name="uq_job_file_chunks_file_seq"),
    )
    op.create_index("ix_job_file_chunks_file_id", "job_file_chunks", ["file_id"])


def downgrade() -> None:
    op.drop_index("ix_job_file_chunks_file_id", table_name="job_file_chunks")
    op.drop_table("job_file_chunks")
    op.drop_index("ix_job_files_job_id", table_name="job_files")
    op.drop_table("job_files")
