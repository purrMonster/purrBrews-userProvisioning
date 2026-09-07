from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from .config import get_settings
from .models import Base

_settings = get_settings()

engine = create_async_engine(
    _settings.database_url,
    echo=False,
    pool_pre_ping=True,
    # SQLite needs this to allow the worker task and request handlers to share
    # a connection pool; harmless on Postgres.
    connect_args={"check_same_thread": False}
    if _settings.database_url.startswith("sqlite")
    else {},
)

SessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


async def init_db() -> None:
    """Create tables.

    Deliberately ``create_all`` rather than Alembic for this testing build —
    the schema is still moving.  Add Alembic before the first deployment that
    holds real accounts, because after that point a schema change has to be a
    migration rather than a drop.
    """
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    async with SessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency."""
    async with SessionLocal() as session:
        yield session
