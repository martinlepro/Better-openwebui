"""add chat queue tables

Revision ID: b7c1e9a4d2f0
Revises: 9f6c2a8d1b43
Create Date: 2026-11-19
"""

from typing import Union

import sqlalchemy as sa
from alembic import op

revision: str = 'b7c1e9a4d2f0'
down_revision: Union[str, None] = '9f6c2a8d1b43'
branch_labels = None
depends_on = None


def _index_exists(inspector, index_name, table_name):
    return any(idx['name'] == index_name for idx in inspector.get_indexes(table_name))


def upgrade():
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    tables = inspector.get_table_names()

    if 'chat_queue_item' not in tables:
        op.create_table(
            'chat_queue_item',
            sa.Column('id', sa.Text(), primary_key=True),
            sa.Column('user_id', sa.Text(), nullable=False),
            sa.Column('chat_id', sa.Text(), nullable=True),
            sa.Column('title', sa.Text(), nullable=True),
            sa.Column('prompt', sa.Text(), nullable=False),
            sa.Column('model_id', sa.Text(), nullable=True),
            sa.Column('kind', sa.Text(), nullable=False, server_default='message'),
            sa.Column('priority', sa.Integer(), nullable=False, server_default='100'),
            sa.Column('status', sa.Text(), nullable=False, server_default='pending'),
            sa.Column('attempts', sa.Integer(), nullable=False, server_default='0'),
            sa.Column('next_attempt_at', sa.BigInteger(), nullable=True),
            sa.Column('last_error', sa.Text(), nullable=True),
            sa.Column('result_message_id', sa.Text(), nullable=True),
            sa.Column('options', sa.JSON(), nullable=True),
            sa.Column('created_at', sa.BigInteger(), nullable=False),
            sa.Column('updated_at', sa.BigInteger(), nullable=False),
            sa.Column('started_at', sa.BigInteger(), nullable=True),
            sa.Column('finished_at', sa.BigInteger(), nullable=True),
        )

    inspector.clear_cache()
    if 'chat_queue_item' in inspector.get_table_names():
        if not _index_exists(inspector, 'ix_chat_queue_item_user_status', 'chat_queue_item'):
            op.create_index('ix_chat_queue_item_user_status', 'chat_queue_item', ['user_id', 'status'])
        if not _index_exists(inspector, 'ix_chat_queue_item_order', 'chat_queue_item'):
            op.create_index('ix_chat_queue_item_order', 'chat_queue_item', ['user_id', 'priority', 'created_at'])

    if 'chat_queue_state' not in tables:
        op.create_table(
            'chat_queue_state',
            sa.Column('user_id', sa.Text(), primary_key=True),
            sa.Column('paused', sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column('limit_until', sa.BigInteger(), nullable=True),
            sa.Column('limit_kind', sa.Text(), nullable=True),
            sa.Column('limit_message', sa.Text(), nullable=True),
            sa.Column('limit_exact', sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column('limit_notified', sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column('batch_active', sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column('batch_started_at', sa.BigInteger(), nullable=True),
            sa.Column('settings', sa.JSON(), nullable=True),
            sa.Column('updated_at', sa.BigInteger(), nullable=False),
        )


def downgrade():
    op.drop_table('chat_queue_state')
    op.drop_index('ix_chat_queue_item_order', table_name='chat_queue_item')
    op.drop_index('ix_chat_queue_item_user_status', table_name='chat_queue_item')
    op.drop_table('chat_queue_item')
