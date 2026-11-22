"""In-memory async SQLite standing in for open_webui.internal.db (tests only)."""

import sys
import types
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import declarative_base
from sqlalchemy.pool import StaticPool

Base = declarative_base()
engine = create_async_engine('sqlite+aiosqlite://', poolclass=StaticPool, connect_args={'check_same_thread': False})
SessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


@asynccontextmanager
async def get_async_db():
    async with SessionLocal() as session:
        yield session


@asynccontextmanager
async def get_async_db_context(db=None):
    async with get_async_db() as session:
        yield session


def install():
    for name in ('open_webui', 'open_webui.internal'):
        sys.modules.setdefault(name, types.ModuleType(name))
    module = types.ModuleType('open_webui.internal.db')
    module.Base = Base
    module.get_async_db = get_async_db
    module.get_async_db_context = get_async_db_context
    sys.modules['open_webui.internal.db'] = module
    return module


async def create_tables():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
