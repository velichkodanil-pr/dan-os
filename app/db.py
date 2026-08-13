"""Async database engine/session for DAN.OS."""
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings

engine = None
SessionLocal: async_sessionmaker[AsyncSession] | None = None


def init_engine(url: str | None = None):
    """Create the global engine/session factory. Called from lifespan or tests."""
    global engine, SessionLocal
    engine = create_async_engine(url or settings.sqlalchemy_url, pool_pre_ping=True, pool_size=5)
    SessionLocal = async_sessionmaker(engine, expire_on_commit=False)
    return engine


def session() -> AsyncSession:
    assert SessionLocal is not None, "init_engine() was not called"
    return SessionLocal()
