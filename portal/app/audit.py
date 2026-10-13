"""변경 이력 기록.

트리거가 아니라 앱에서 남긴다. 트리거는 '누가 바꿨는지'를 알 수 없고,
세션 변수로 넘기면 커넥션 풀에서 섞일 위험이 있다.

인증이 없으므로 changed_by 는 폼에서 입력받은 이름일 뿐이다.
감사 추적이 아니라 '언제 무엇이 바뀌었나'를 되짚기 위한 기록이다.
"""

from __future__ import annotations

from typing import Any

from .db import run

# 이력에 남길 필요가 없는 컬럼
SKIP = {"id", "created_at", "updated_at", "updated_by", "customer_id"}


def _norm(v: Any) -> str:
    """비교용 정규화. None 과 빈 문자열을 같게 보고, 숫자는 문자열로 맞춘다."""
    if v is None:
        return ""
    return str(v).strip()


def log(db, cluster_id: int, action: str, table_name: str,
        row_id: int | None = None, field: str | None = None,
        old: Any = None, new: Any = None, by: str = "") -> None:
    run(db, """
        INSERT INTO change_log
          (cluster_id, changed_by, action, table_name, row_id, field_name, old_value, new_value)
        VALUES (:c, NULLIF(:by,''), :a, :t, :r, :f, :o, :n)
    """, c=cluster_id, by=by or "", a=action, t=table_name, r=row_id,
         f=field, o=None if old is None else str(old),
         n=None if new is None else str(new))


def log_diff(db, cluster_id: int, table_name: str, row_id: int | None,
             before: dict | None, after: dict, by: str = "") -> int:
    """before/after 를 비교해 바뀐 컬럼만 기록한다. 반환값은 기록된 건수."""
    if before is None:
        log(db, cluster_id, "CREATE", table_name, row_id, by=by)
        return 1

    n = 0
    for k, new_v in after.items():
        if k in SKIP:
            continue
        old_v = before.get(k)
        if _norm(old_v) != _norm(new_v):
            log(db, cluster_id, "UPDATE", table_name, row_id, k, old_v, new_v, by)
            n += 1
    return n


def log_delete(db, cluster_id: int, table_name: str, row_id: int,
               label: str = "", by: str = "") -> None:
    log(db, cluster_id, "DELETE", table_name, row_id, old=label or None, by=by)
