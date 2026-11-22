"""Chat queue: prioritised messages/conversations that are sent automatically.

An item either starts a new conversation or adds a message to an existing one.
The queue runner (``utils/chat_queue.py``) executes items one at a time per user,
highest priority first, and pauses itself while the provider limit is active.

Times are integer epoch seconds. A lower ``priority`` number runs first.
"""

import logging
import time
from typing import Optional
from uuid import uuid4

from open_webui.internal.db import Base, get_async_db_context
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import JSON, BigInteger, Boolean, Column, Index, Integer, Text, delete, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

log = logging.getLogger(__name__)

# Item statuses
STATUS_PENDING = 'pending'  # waiting its turn
STATUS_RUNNING = 'running'  # being sent right now
STATUS_NEEDS_APPROVAL = 'needs_approval'  # the assistant wants to run a tool and waits for the user
STATUS_DONE = 'done'
STATUS_FAILED = 'failed'
STATUS_CANCELLED = 'cancelled'
STATUS_PAUSED = 'paused'  # skipped by the runner until the user re-enables it

ACTIVE_STATUSES = (STATUS_PENDING, STATUS_RUNNING, STATUS_NEEDS_APPROVAL, STATUS_PAUSED)
FINISHED_STATUSES = (STATUS_DONE, STATUS_FAILED, STATUS_CANCELLED)
ALL_STATUSES = ACTIVE_STATUSES + FINISHED_STATUSES

KIND_MESSAGE = 'message'  # normal item: new conversation or new message
KIND_RESUME = 'resume'  # created after a limit hit: retry/continue an existing conversation

DEFAULT_PRIORITY = 100
RESUME_PRIORITY = 0  # resume items jump the queue: they finish interrupted work first

DEFAULT_RESUME_MESSAGE = 'Continue exactly where you stopped. Do not repeat what is already done.'

DEFAULT_STATE_SETTINGS = {
    # When a chat reply fails because of a limit, queue a resume for it automatically.
    'auto_resume': False,
    'resume_message': DEFAULT_RESUME_MESSAGE,
    # Which events notify the user (toast + browser notification + sound + webhook).
    'notify_limit_reached': True,
    'notify_item_done': True,
    'notify_queue_done': True,
    'notify_needs_approval': True,
    'notify_item_failed': True,
}


# DB schema


class ChatQueueItem(Base):
    __tablename__ = 'chat_queue_item'

    id = Column(Text, primary_key=True)
    user_id = Column(Text, nullable=False)
    chat_id = Column(Text, nullable=True)  # existing chat to continue; filled for new ones once created
    title = Column(Text, nullable=True)
    prompt = Column(Text, nullable=False)
    model_id = Column(Text, nullable=True)
    kind = Column(Text, nullable=False, default=KIND_MESSAGE)
    priority = Column(Integer, nullable=False, default=DEFAULT_PRIORITY)
    status = Column(Text, nullable=False, default=STATUS_PENDING)
    attempts = Column(Integer, nullable=False, default=0)
    next_attempt_at = Column(BigInteger, nullable=True)
    last_error = Column(Text, nullable=True)
    result_message_id = Column(Text, nullable=True)
    # {features: {...}, params: {...}, tool_approval_mode, local_agent: {cwd, approval}}
    options = Column(JSON, nullable=True)

    created_at = Column(BigInteger, nullable=False)
    updated_at = Column(BigInteger, nullable=False)
    started_at = Column(BigInteger, nullable=True)
    finished_at = Column(BigInteger, nullable=True)

    __table_args__ = (
        Index('ix_chat_queue_item_user_status', 'user_id', 'status'),
        Index('ix_chat_queue_item_order', 'user_id', 'priority', 'created_at'),
    )


class ChatQueueState(Base):
    __tablename__ = 'chat_queue_state'

    user_id = Column(Text, primary_key=True)
    paused = Column(Boolean, nullable=False, default=False)  # user paused the whole queue
    limit_until = Column(BigInteger, nullable=True)  # provider limit active until (epoch s)
    limit_kind = Column(Text, nullable=True)
    limit_message = Column(Text, nullable=True)
    limit_exact = Column(Boolean, nullable=False, default=False)  # reset time given by the provider (not a guess)
    limit_notified = Column(Boolean, nullable=False, default=False)
    batch_active = Column(Boolean, nullable=False, default=False)  # something ran since the queue was last empty
    batch_started_at = Column(BigInteger, nullable=True)
    settings = Column(JSON, nullable=True)
    updated_at = Column(BigInteger, nullable=False)


# Pydantic models


class ChatQueueItemModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    user_id: str
    chat_id: Optional[str] = None
    title: Optional[str] = None
    prompt: str
    model_id: Optional[str] = None
    kind: str = KIND_MESSAGE
    priority: int = DEFAULT_PRIORITY
    status: str = STATUS_PENDING
    attempts: int = 0
    next_attempt_at: Optional[int] = None
    last_error: Optional[str] = None
    result_message_id: Optional[str] = None
    options: Optional[dict] = None

    created_at: int
    updated_at: int
    started_at: Optional[int] = None
    finished_at: Optional[int] = None


class ChatQueueStateModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    user_id: str
    paused: bool = False
    limit_until: Optional[int] = None
    limit_kind: Optional[str] = None
    limit_message: Optional[str] = None
    limit_exact: bool = False
    limit_notified: bool = False
    batch_active: bool = False
    batch_started_at: Optional[int] = None
    settings: Optional[dict] = None
    updated_at: int = 0


class ChatQueueItemForm(BaseModel):
    prompt: str = Field(min_length=1, max_length=100_000)
    title: Optional[str] = Field(default=None, max_length=200)
    chat_id: Optional[str] = None
    model_id: Optional[str] = None
    kind: str = KIND_MESSAGE
    priority: Optional[int] = Field(default=None, ge=0, le=100_000)
    options: Optional[dict] = None


class ChatQueueItemUpdateForm(BaseModel):
    prompt: Optional[str] = Field(default=None, min_length=1, max_length=100_000)
    title: Optional[str] = Field(default=None, max_length=200)
    model_id: Optional[str] = None
    priority: Optional[int] = Field(default=None, ge=0, le=100_000)
    options: Optional[dict] = None
    status: Optional[str] = None  # only pending <-> paused / cancelled are accepted by the router


def merged_settings(state: Optional['ChatQueueStateModel']) -> dict:
    return {**DEFAULT_STATE_SETTINGS, **((state.settings if state else None) or {})}


# Tables


class ChatQueueTable:
    # items

    async def insert(
        self, user_id: str, form: ChatQueueItemForm, db: Optional[AsyncSession] = None
    ) -> ChatQueueItemModel:
        async with get_async_db_context(db) as db:
            now = int(time.time())
            if form.priority is not None:
                priority = form.priority
            elif form.kind == KIND_RESUME:
                priority = RESUME_PRIORITY
            else:
                priority = DEFAULT_PRIORITY
            row = ChatQueueItem(
                id=str(uuid4()),
                user_id=user_id,
                chat_id=form.chat_id,
                title=form.title,
                prompt=form.prompt,
                model_id=form.model_id,
                kind=form.kind,
                priority=priority,
                status=STATUS_PENDING,
                attempts=0,
                options=form.options,
                created_at=now,
                updated_at=now,
            )
            db.add(row)
            await db.commit()
            return ChatQueueItemModel.model_validate(row)

    async def get_by_id(self, id: str, db: Optional[AsyncSession] = None) -> Optional[ChatQueueItemModel]:
        async with get_async_db_context(db) as db:
            row = await db.get(ChatQueueItem, id)
            return ChatQueueItemModel.model_validate(row) if row else None

    async def get_for_user(
        self, user_id: str, id: str, db: Optional[AsyncSession] = None
    ) -> Optional[ChatQueueItemModel]:
        item = await self.get_by_id(id, db=db)
        return item if item and item.user_id == user_id else None

    async def list_by_user(
        self,
        user_id: str,
        statuses: Optional[list[str]] = None,
        limit: int = 500,
        db: Optional[AsyncSession] = None,
    ) -> list[ChatQueueItemModel]:
        async with get_async_db_context(db) as db:
            stmt = select(ChatQueueItem).where(ChatQueueItem.user_id == user_id)
            if statuses:
                stmt = stmt.where(ChatQueueItem.status.in_(statuses))
            stmt = stmt.order_by(ChatQueueItem.priority.asc(), ChatQueueItem.created_at.asc()).limit(limit)
            result = await db.execute(stmt)
            return [ChatQueueItemModel.model_validate(r) for r in result.scalars().all()]

    async def update_by_id(
        self, id: str, fields: dict, db: Optional[AsyncSession] = None
    ) -> Optional[ChatQueueItemModel]:
        async with get_async_db_context(db) as db:
            row = await db.get(ChatQueueItem, id)
            if not row:
                return None
            for key, value in fields.items():
                setattr(row, key, value)
            row.updated_at = int(time.time())
            await db.commit()
            return ChatQueueItemModel.model_validate(row)

    async def delete_by_id(self, user_id: str, id: str, db: Optional[AsyncSession] = None) -> bool:
        async with get_async_db_context(db) as db:
            result = await db.execute(
                delete(ChatQueueItem).where(ChatQueueItem.id == id, ChatQueueItem.user_id == user_id)
            )
            await db.commit()
            return (result.rowcount or 0) > 0

    async def delete_finished(self, user_id: str, db: Optional[AsyncSession] = None) -> int:
        async with get_async_db_context(db) as db:
            result = await db.execute(
                delete(ChatQueueItem).where(
                    ChatQueueItem.user_id == user_id, ChatQueueItem.status.in_(FINISHED_STATUSES)
                )
            )
            await db.commit()
            return result.rowcount or 0

    async def reorder(self, user_id: str, ordered_ids: list[str], db: Optional[AsyncSession] = None) -> None:
        """Renumber priorities 10, 20, 30... following ``ordered_ids`` (unknown ids are ignored)."""
        async with get_async_db_context(db) as db:
            now = int(time.time())
            for index, item_id in enumerate(ordered_ids):
                await db.execute(
                    update(ChatQueueItem)
                    .where(ChatQueueItem.id == item_id, ChatQueueItem.user_id == user_id)
                    .values(priority=(index + 1) * 10, updated_at=now)
                )
            await db.commit()

    async def claim_next(
        self, user_id: str, now: Optional[int] = None, db: Optional[AsyncSession] = None
    ) -> Optional[ChatQueueItemModel]:
        """Atomically turn the best eligible pending item into ``running`` and return it."""
        now = int(now if now is not None else time.time())
        async with get_async_db_context(db) as db:
            for _ in range(5):  # another instance may win the race: try the next candidate
                result = await db.execute(
                    select(ChatQueueItem)
                    .where(
                        ChatQueueItem.user_id == user_id,
                        ChatQueueItem.status == STATUS_PENDING,
                        or_(ChatQueueItem.next_attempt_at.is_(None), ChatQueueItem.next_attempt_at <= now),
                    )
                    .order_by(ChatQueueItem.priority.asc(), ChatQueueItem.created_at.asc())
                    .limit(1)
                )
                candidate = result.scalars().first()
                if not candidate:
                    return None
                claimed = await db.execute(
                    update(ChatQueueItem)
                    .where(ChatQueueItem.id == candidate.id, ChatQueueItem.status == STATUS_PENDING)
                    .values(
                        status=STATUS_RUNNING,
                        started_at=now,
                        updated_at=now,
                        attempts=(candidate.attempts or 0) + 1,
                    )
                )
                await db.commit()
                if (claimed.rowcount or 0) > 0:
                    row = await db.get(ChatQueueItem, candidate.id)
                    await db.refresh(row)
                    return ChatQueueItemModel.model_validate(row)
            return None

    async def count_by_status(self, user_id: str, db: Optional[AsyncSession] = None) -> dict[str, int]:
        async with get_async_db_context(db) as db:
            result = await db.execute(
                select(ChatQueueItem.status, func.count())
                .where(ChatQueueItem.user_id == user_id)
                .group_by(ChatQueueItem.status)
            )
            return {status: count for status, count in result.all()}

    async def user_ids_with_pending(self, now: Optional[int] = None, db: Optional[AsyncSession] = None) -> list[str]:
        now = int(now if now is not None else time.time())
        async with get_async_db_context(db) as db:
            result = await db.execute(
                select(ChatQueueItem.user_id)
                .where(
                    ChatQueueItem.status == STATUS_PENDING,
                    or_(ChatQueueItem.next_attempt_at.is_(None), ChatQueueItem.next_attempt_at <= now),
                )
                .distinct()
            )
            return [row[0] for row in result.all()]

    async def list_needing_approval(self, db: Optional[AsyncSession] = None) -> list[ChatQueueItemModel]:
        async with get_async_db_context(db) as db:
            result = await db.execute(select(ChatQueueItem).where(ChatQueueItem.status == STATUS_NEEDS_APPROVAL))
            return [ChatQueueItemModel.model_validate(r) for r in result.scalars().all()]

    async def reset_interrupted(self, db: Optional[AsyncSession] = None) -> int:
        """At startup: items left ``running`` by a previous process go back to ``pending``."""
        async with get_async_db_context(db) as db:
            result = await db.execute(
                update(ChatQueueItem)
                .where(ChatQueueItem.status == STATUS_RUNNING)
                .values(status=STATUS_PENDING, updated_at=int(time.time()))
            )
            await db.commit()
            return result.rowcount or 0

    # per-user state

    async def get_state(self, user_id: str, db: Optional[AsyncSession] = None) -> ChatQueueStateModel:
        async with get_async_db_context(db) as db:
            row = await db.get(ChatQueueState, user_id)
            if row:
                return ChatQueueStateModel.model_validate(row)
            return ChatQueueStateModel(user_id=user_id)

    async def set_state(self, user_id: str, db: Optional[AsyncSession] = None, **fields) -> ChatQueueStateModel:
        async with get_async_db_context(db) as db:
            row = await db.get(ChatQueueState, user_id)
            if not row:
                row = ChatQueueState(
                    user_id=user_id,
                    paused=False,
                    limit_exact=False,
                    limit_notified=False,
                    batch_active=False,
                    updated_at=int(time.time()),
                )
                db.add(row)
            for key, value in fields.items():
                setattr(row, key, value)
            row.updated_at = int(time.time())
            await db.commit()
            return ChatQueueStateModel.model_validate(row)


ChatQueue = ChatQueueTable()
