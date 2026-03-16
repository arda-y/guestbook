import asyncio
import datetime

from fastapi.responses import FileResponse
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker, registry
from sqlalchemy import Column, Integer, String, select
from fastapi import FastAPI, Request
from slowapi import Limiter
from slowapi.util import get_remote_address

# The 'sqlite+aiosqlite' driver is key for async SQLite
DATABASE_URL = "sqlite+aiosqlite:///./mountpoint/guestbook.db"

engine = create_async_engine(DATABASE_URL, echo=False)
AsyncSessionLocal = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
limiter = Limiter(key_func=get_remote_address)

app = FastAPI()
mapper_registry = registry()
Base = mapper_registry.generate_base()

engine = create_async_engine(DATABASE_URL, echo=True)
AsyncSessionLocal = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


class Guestbook(Base):
    __tablename__ = "guestbook_entries"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(100), nullable=False)
    message = Column(String(500), nullable=False)
    time = Column(String(100), nullable=False)
    stars = Column(Integer, nullable=False)

@app.get("/")
async def root():
    return FileResponse("index.html", media_type="text/html")

@app.get("/root")
@limiter.limit("5/minute")
async def api_read_root(request: Request):
    return get_remote_address(request)


async def post_entry(name: str, message: str):
    async with AsyncSessionLocal() as q:
        session: AsyncSession = q

        new_entry = Guestbook(
            name=name.strip(),
            message=message.strip(),
            time=str(datetime.datetime.now().isoformat()),
            stars=0,
        )
        session.add(new_entry)
        await session.commit()
        await session.refresh(new_entry)
        return {"message": "success"}


@app.post("/post/entry")
@limiter.limit("2/day")
async def api_post_entry(request: Request, name: str, message: str):
    return await post_entry(name, message)


async def get_entries(index: int = 0, limit: int = 10):
    async with AsyncSessionLocal() as q:
        session: AsyncSession = q

        statement = select(Guestbook).offset(index).limit(limit)
        result = await session.execute(statement)

        return result.scalars().all()


@app.get("/get/entry")
@limiter.limit("15/minute")
async def api_get_entries(request: Request, index: int = 0, limit: int = 10):
    if limit != 10:
        return {"message": "stop snooping around!!!"}
    return await get_entries(index=index, limit=limit)


async def add_star(entry_id: int):
    async with AsyncSessionLocal() as q:
        session: AsyncSession = q

        statement = select(Guestbook).where(Guestbook.id == entry_id)
        result = await session.execute(statement)
        entry = result.scalar_one_or_none()

        if entry is None:
            return {"message": "Entry not found"}

        entry.stars += 1
        await session.commit()
        await session.refresh(entry)
        return {"message": "Star added", "entry": entry}


@app.post("/post/star")
@limiter.limit("3/day")
async def api_add_star(request: Request, entry_id: int):
    return await add_star(entry_id)


async def async_main():
    async with engine.begin() as conn:
        # This actually creates the tables defined in your Base
        await conn.run_sync(Base.metadata.create_all)
    print("Database tables created.")


if __name__ == "__main__":
    import uvicorn

    asyncio.run(async_main())

    uvicorn.run(
        "main:app", host="0.0.0.0", port=2004, loop="asyncio", log_level="info"
    )
