from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine


def make_engine(url: str, *, pool_size: int = 10) -> AsyncEngine:
    return create_async_engine(url, pool_pre_ping=True, pool_size=pool_size, max_overflow=5)


def make_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)
