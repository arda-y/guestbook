from contextlib import asynccontextmanager
import datetime
import ipaddress
import time
import secrets

from fastapi import status
from fastapi.params import Depends
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker, registry
from sqlalchemy import Column, Integer, String, UniqueConstraint, delete, func, select, update
from fastapi import FastAPI, HTTPException, Request
from slowapi import Limiter
from slowapi.util import get_remote_address
import uvicorn
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware


def get_docker_gateway_ip() -> str | None:
    """
    Reads /proc/net/route to find the default gateway IP as seen from inside
    this container. This is the Docker bridge gateway, which nginx (running on
    the host) appears as when it reaches this container through Docker's
    published-port path.

    Docker can reassign this IP whenever the compose network is recreated
    (e.g. after `docker compose down`), so we resolve it at each startup
    instead of hardcoding a value that goes stale.
    """
    try:
        with open("/proc/net/route", encoding="ascii") as f:
            for line in f.readlines()[1:]:
                fields = line.strip().split()
                if len(fields) < 3:
                    continue
                if fields[1] == "00000000":  # destination 0.0.0.0 = default route
                    gateway_int = int(fields[2], 16)
                    # /proc/net/route stores the gateway in little-endian hex
                    gateway_bytes = gateway_int.to_bytes(4, byteorder="little")
                    return ".".join(str(b) for b in gateway_bytes)
    except (OSError, ValueError):
        pass
    return None


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
async def lifespan(app: FastAPI): # ignore: unused-argument
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    
    print("\n" + "="*40, flush=True)
    print(f"DATABASE: Tables verified/created.", flush=True)
    print(f"ADMIN KEY: {get_admin_key()}", flush=True)
    print("="*40 + "\n", flush=True)
    
    yield

app = FastAPI(lifespan=lifespan)

# Trust 127.0.0.1 (direct/local runs) and the current Docker gateway (how nginx
# actually appears when it reaches this container through the published port).
# Resolved at startup instead of hardcoded - see get_docker_gateway_ip().
_trusted_hosts = ["127.0.0.1"]
_gateway_ip = get_docker_gateway_ip()
if _gateway_ip:
    _trusted_hosts.append(_gateway_ip)
print(f"Trusted proxy hosts: {_trusted_hosts}", flush=True)

app.add_middleware(ProxyHeadersMiddleware, trusted_hosts=_trusted_hosts)

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

@app.get("/admin", dependencies=[Depends(check_banned_status)], include_in_schema=False) 
async def edit_entry(
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
                if len(name.strip()) > NAME_MAX_LENGTH:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail=f"name must be at most {NAME_MAX_LENGTH} characters",
                    )
                entry.name = name.strip()
            if message is not None:
                if len(message.strip()) > MESSAGE_MAX_LENGTH:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail=f"message must be at most {MESSAGE_MAX_LENGTH} characters",
                    )
                entry.message = message.strip()
            if stars is not None:
                entry.stars = stars
            await session.commit()
            await session.refresh(entry)
            return {"message": "Entry updated"}

    else:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid action or missing parameters")


NAME_MAX_LENGTH = 100
MESSAGE_MAX_LENGTH = 500

# One like per entry per IP address per week.
LIKE_COOLDOWN = 7 * 24 * 60 * 60
# At most 2 new entries per IP address per day. Both kinds of record are deleted after a week.
POSTS_PER_DAY = 2
DAY_SECONDS = 24 * 60 * 60
RECORD_LIFETIME = 7 * 24 * 60 * 60


def ip_key(ip: str) -> str:
    """The client IP as stored with a like. IPv6 clients are grouped by /64, since one
    home connection controls a whole /64."""
    try:
        addr = ipaddress.ip_address(ip)
        if addr.version == 6:
            return str(ipaddress.ip_network(f"{ip}/64", strict=False).network_address)
    except ValueError:
        pass
    return ip


class Guestbook(Base):
    __tablename__ = "guestbook_entries"

    id = Column(Integer, primary_key=True, index=True)
    # SQLite does not enforce String() length limits by itself - the
    # NAME_MAX_LENGTH/MESSAGE_MAX_LENGTH checks below are what actually
    # enforce these; these column sizes are documentation only unless you
    # switch to a database that enforces VARCHAR length (e.g. Postgres/MySQL).
    name = Column(String(100), nullable=False)
    message = Column(String(500), nullable=False)
    time = Column(String(100), nullable=False)
    stars = Column(Integer, nullable=False)


class EntryLike(Base):
    """One row per (entry, IP) while that IP's weekly like is in force. Rows are deleted after a week."""
    __tablename__ = "entry_likes"
    __table_args__ = (UniqueConstraint("entry_id", "ip"),)

    id = Column(Integer, primary_key=True)
    entry_id = Column(Integer, nullable=False, index=True)
    ip = Column(String(45), nullable=False, index=True)
    liked_at = Column(Integer, nullable=False)


class PostRecord(Base):
    """One row per new entry: which IP sent it and when. It is only used to enforce the daily
    limit, and is never linked to the entry itself. Rows are deleted after a week."""
    __tablename__ = "post_records"

    id = Column(Integer, primary_key=True)
    ip = Column(String(45), nullable=False, index=True)
    posted_at = Column(Integer, nullable=False, index=True)


async def purge_old_records(session: AsyncSession, now: int):
    """Forget IP addresses that are older than a week."""
    await session.execute(delete(EntryLike).where(EntryLike.liked_at <= now - RECORD_LIFETIME))
    await session.execute(delete(PostRecord).where(PostRecord.posted_at <= now - RECORD_LIFETIME))


class RevalidatingStatic(StaticFiles):
    """Static files that the browser re-checks on every visit (cheap, they answer 304 when unchanged),
    so a change to the colours or the script shows up without a hard refresh."""
    def file_response(self, *args, **kwargs):
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-cache"
        return response


app.mount("/static", RevalidatingStatic(directory="static"), name="static")


@app.get("/")
async def root():
    return FileResponse("index.html", media_type="text/html")

@app.get("/faq")
async def faq():
    return FileResponse("faq.html", media_type="text/html")

@app.get("/root")
@limiter.limit("5/minute")
async def api_read_root(request: Request):
    return request.client.host


async def post_entry(name: str, message: str, ip: str):
    if len(name.strip()) > NAME_MAX_LENGTH:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"name must be at most {NAME_MAX_LENGTH} characters",
        )
    if len(message.strip()) > MESSAGE_MAX_LENGTH:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"message must be at most {MESSAGE_MAX_LENGTH} characters",
        )

    now = int(time.time())
    async with AsyncSessionLocal() as q:
        session: AsyncSession = q

        await purge_old_records(session, now)
        sent = await session.execute(
            select(func.count()).select_from(PostRecord).where(
                PostRecord.ip == ip_key(ip), PostRecord.posted_at > now - DAY_SECONDS
            )
        )
        if sent.scalar_one() >= POSTS_PER_DAY:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=f"You can send {POSTS_PER_DAY} entries per day. Try again tomorrow.",
            )
        session.add(PostRecord(ip=ip_key(ip), posted_at=now))

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
async def api_post_entry(request: Request, name: str, message: str):
    return await post_entry(name, message, request.client.host)


async def get_entries(ip: str, index: int = 0, limit: int = 10):
    async with AsyncSessionLocal() as q:
        session: AsyncSession = q

        # get 10 newest entries starting from index
        statement = select(Guestbook).order_by(Guestbook.id.desc()).offset(index).limit(limit)
        entries = (await session.execute(statement)).scalars().all()

        # which of these entries this IP address has already liked this week
        liked_ids = set()
        if entries:
            liked = await session.execute(
                select(EntryLike.entry_id).where(
                    EntryLike.ip == ip_key(ip),
                    EntryLike.liked_at > int(time.time()) - LIKE_COOLDOWN,
                    EntryLike.entry_id.in_([e.id for e in entries]),
                )
            )
            liked_ids = set(liked.scalars().all())

        return [
            {"id": e.id, "name": e.name, "message": e.message, "time": e.time, "stars": e.stars,
             "liked": e.id in liked_ids}
            for e in entries
        ]


@app.get("/get/entry")
@limiter.limit("15/minute")
async def api_get_entries(request: Request, index: int = 0, limit: int = 10):
    if limit != 10:
        return {"message": "stop snooping around!!!"}
    # "liked" depends on who asks, so no cache may keep this answer
    return JSONResponse(await get_entries(request.client.host, index=index, limit=limit),
                        headers={"Cache-Control": "private, no-store"})


async def add_star(entry_id: int, ip: str):
    now = int(time.time())
    async with AsyncSessionLocal() as q:
        session: AsyncSession = q

        statement = select(Guestbook).where(Guestbook.id == entry_id)
        result = await session.execute(statement)
        entry = result.scalar_one_or_none()

        if entry is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Entry not found")

        # forget likes older than a week, then record this one. The unique
        # (entry, ip) key rejects a second like while the first is still in force,
        # even if two requests arrive at the same moment.
        await purge_old_records(session, now)
        await session.execute(delete(EntryLike).where(EntryLike.liked_at <= now - LIKE_COOLDOWN))
        session.add(EntryLike(entry_id=entry_id, ip=ip_key(ip), liked_at=now))
        try:
            await session.flush()
        except IntegrityError:
            await session.rollback()
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="You already liked this entry. You can like it again after a week.",
            ) from None

        await session.execute(update(Guestbook).where(Guestbook.id == entry_id).values(stars=Guestbook.stars + 1))
        await session.commit()
        await session.refresh(entry)
        return {"message": "Liked", "stars": entry.stars}


@app.post("/post/star")
@limiter.limit("60/minute")
async def api_add_star(request: Request, entry_id: int):
    return await add_star(entry_id, request.client.host)


if __name__ == "__main__":
    
    uvicorn.run(
        "main:app", host="0.0.0.0", port=2004, log_level="info"
    )
