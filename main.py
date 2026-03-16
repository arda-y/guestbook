import asyncio
from contextlib import asynccontextmanager
import datetime
import os
import time
import secrets

from fastapi import status
from fastapi.params import Depends
from fastapi.responses import FileResponse
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker, registry
from sqlalchemy import Column, Integer, String, select
from fastapi import FastAPI, HTTPException, Request
from slowapi import Limiter
from slowapi.util import get_remote_address

# The 'sqlite+aiosqlite' driver is key for async SQLite
DATABASE_URL = "sqlite+aiosqlite:///./mountpoint/guestbook.db"

engine = create_async_engine(DATABASE_URL, echo=False)
AsyncSessionLocal = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
limiter = Limiter(key_func=get_remote_address)


_ADMIN_KEY = None

def get_admin_key():
    global _ADMIN_KEY
    if _ADMIN_KEY is None:
        _ADMIN_KEY = secrets.token_urlsafe(32)
    return _ADMIN_KEY

@asynccontextmanager
async def lifespan(app: FastAPI):
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    
    print("\n" + "="*40, flush=True)
    print(f"DATABASE: Tables verified/created.", flush=True)
    print(f"ADMIN KEY: {get_admin_key()}", flush=True)
    print("="*40 + "\n", flush=True)
    
    yield

app = FastAPI(lifespan=lifespan)


mapper_registry = registry()
Base = mapper_registry.generate_base()

engine = create_async_engine(DATABASE_URL, echo=True)
AsyncSessionLocal = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


soft_banned_ips = {}
BAN_DURATION = 60 * 60 * 24

def check_banned_status(request: Request):
    client_ip = request.client.host
    if client_ip in soft_banned_ips:
        # Check if the ban has expired
        if time.time() < soft_banned_ips[client_ip]:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Access denied due to previous unauthorized attempts."
            )
        else:
            # Ban expired, clean up
            del soft_banned_ips[client_ip]

@app.get("/admin/panel", dependencies=[Depends(check_banned_status)], include_in_schema=False) 
async def admin_panel(
    request: Request, 
    action: str = None,
    key: str = None, 
    entry_id: int = None, 
    name: str = None, 
    message: str = None, 
    stars: int = None, 
    ):
    
    client_ip = request.client.host

    print(f"Admin panel access attempt from IP: {client_ip} with action: {action}")
    print(f"received key: {key}")
    
    if key.strip() != get_admin_key().strip():
        # trigger soft ban for the offending IP
        soft_banned_ips[client_ip] = time.time() + BAN_DURATION
        print(f"!!! ALERT: Soft-banning IP {client_ip} for invalid key.")
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)

    if action == "delete" and entry_id is not None:
        async with AsyncSessionLocal() as q:
            session: AsyncSession = q
            statement = select(Guestbook).where(Guestbook.id == entry_id)
            result = await session.execute(statement)
            entry = result.scalar_one_or_none()
            if entry is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Entry not found")
            await session.delete(entry)
            await session.commit()
            return {"message": "Entry deleted"}

    elif action == "update" and entry_id is not None:
        async with AsyncSessionLocal() as q:
            session: AsyncSession = q
            statement = select(Guestbook).where(Guestbook.id == entry_id)
            result = await session.execute(statement)
            entry = result.scalar_one_or_none()
            if entry is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Entry not found")
            if name is not None:
                entry.name = name.strip()
            if message is not None:
                entry.message = message.strip()
            if stars is not None:
                entry.stars = stars
            await session.commit()
            await session.refresh(entry)
            return {"message": "Entry updated"}

    else:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid action or missing parameters")


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


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "main:app", host="0.0.0.0", port=2004, log_level="info"
    )
