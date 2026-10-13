import os

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

DB_HOST = os.getenv("DB_HOST", "mariadb-0.mariadb.vast-portal.svc")
DB_PORT = os.getenv("DB_PORT", "3306")
DB_NAME = os.getenv("DB_NAME", "vast_portal")
DB_USER = os.getenv("DB_USER", "portal")
DB_PASSWORD = os.getenv("DB_PASSWORD", "")

DATABASE_URL = (
    f"mysql+pymysql://{DB_USER}:{DB_PASSWORD}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
    "?charset=utf8mb4"
)

engine = create_engine(
    DATABASE_URL,
    pool_pre_ping=True,   # NFS 위 DB라 유휴 커넥션이 끊길 수 있다
    pool_recycle=3600,
    pool_size=5,
    max_overflow=10,
    future=True,
)

SessionLocal = sessionmaker(bind=engine, autoflush=False, future=True)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def rows(db, sql: str, **params) -> list[dict]:
    return [dict(r) for r in db.execute(text(sql), params).mappings()]


def one(db, sql: str, **params) -> dict | None:
    r = rows(db, sql, **params)
    return r[0] if r else None


def scalar(db, sql: str, **params):
    r = db.execute(text(sql), params).first()
    return r[0] if r else None


def run(db, sql: str, **params):
    return db.execute(text(sql), params)
