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

# 커넥션 풀 설정
#
# 유휴 커넥션은 끊긴다. 원인은 두 가지다.
#   - MariaDB wait_timeout: 아무 쿼리도 안 오면 서버가 먼저 닫는다(기본 8시간)
#   - 중간 네트워크 장비: 방화벽/로드밸런서가 세션 테이블에서 지운다
# 어느 쪽이든 클라이언트는 모른 채로 죽은 소켓을 들고 있다.
#
# 두 설정이 이걸 양쪽에서 막는다.
#   pool_pre_ping  꺼내 쓸 때 한 번 찔러보고, 죽었으면 버리고 새로 맺는다(사후)
#   pool_recycle   1시간 넘은 커넥션은 쓰기 전에 교체한다(사전)
engine = create_engine(
    DATABASE_URL,
    pool_pre_ping=True,
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
