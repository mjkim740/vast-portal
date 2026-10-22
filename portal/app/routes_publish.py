"""3단계: Confluence 발행.

흐름
  GET  /cluster/{id}/publish   미리보기 — 생성될 본문과 대상 페이지를 보여준다
  POST /cluster/{id}/publish   발행 — 성공/실패 모두 publish_log 에 남긴다
  GET  /publish                발행이 밀린 클러스터 목록 (v_publish_pending)

설계 판단
  - 발행은 성공해도 실패해도 반드시 publish_log 에 한 줄 남는다.
    실패를 조용히 넘기면 "위키가 최신인가" 를 아무도 알 수 없게 된다.
  - 첫 발행에서 만들어진 page_id 를 cluster 에 저장한다. 그 다음부터는 갱신이라
    위키 페이지의 버전 기록이 이어진다(새 페이지가 계속 생기지 않는다).
  - 인증 정보가 없는 환경(로컬/개발)에서도 미리보기는 동작해야 한다.
    토큰 없이 띄워도 생성 결과를 눈으로 검증할 수 있어야 하기 때문이다.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from . import audit, confluence, page_body
from .db import get_db, one, rows, run, scalar

router = APIRouter()


def _gather(db: Session, cluster_id: int) -> dict:
    """발행에 필요한 값을 한 번에 모은다. 상세 화면과 같은 소스를 쓴다."""
    basic = one(db, "SELECT * FROM v_cluster_basic WHERE cluster_id = :id", id=cluster_id)
    if not basic:
        raise HTTPException(404, "클러스터를 찾을 수 없습니다")

    return {
        "basic": basic,
        "spec": one(db, "SELECT * FROM cluster WHERE id = :id", id=cluster_id),
        "protocols": [r["protocol"] for r in rows(
            db, "SELECT protocol FROM cluster_protocol WHERE cluster_id=:id "
                "ORDER BY protocol", id=cluster_id)],
        "service_types": [r["service_type"] for r in rows(
            db, "SELECT service_type FROM cluster_service_type WHERE cluster_id=:id "
                "ORDER BY service_type", id=cluster_id)],
        "network": rows(db, """
            SELECT wiki_label, role, fabric, addresses, sort_order
            FROM v_network_label WHERE cluster_id = :id ORDER BY sort_order
        """, id=cluster_id),
        "licenses": rows(db, """
            SELECT * FROM license WHERE cluster_id = :id ORDER BY sort_order, id
        """, id=cluster_id),
        "firmware": rows(db, """
            SELECT component, model, version, checked_at FROM v_firmware_current
            WHERE cluster_id = :id
        """, id=cluster_id),
        "units": rows(db, """
            SELECT kind, model_name, seq, serial_no, part_no, remark
            FROM hardware_unit WHERE cluster_id = :id
        """, id=cluster_id),
    }


def _build(data: dict) -> tuple[str, str]:
    body = page_body.build(
        basic=data["basic"], spec=data["spec"], licenses=data["licenses"],
        firmware=data["firmware"], protocols=data["protocols"],
        service_types=data["service_types"], network=data["network"],
        units=data["units"],
    )
    # 제목은 wiki_title(cluster 테이블) 과 고객사/용도(뷰) 를 둘 다 본다.
    return page_body.page_title({**data["basic"], **data["spec"]}), body


def _title_conflict(db: Session, cluster_id: int, title: str) -> dict | None:
    """같은 제목이 될 다른 클러스터를 찾는다.

    DB 의 UNIQUE 는 wiki_title 을 직접 적은 경우만 막는다(NULL 은 여러 개 허용).
    자동 생성 제목끼리의 충돌은 여기서 잡는다. 안 잡으면 두 번째 발행이
    첫 번째의 위키 페이지를 조용히 덮어쓴다 — 가장 위험한 실패 방식이다.

    클러스터가 수십 개 규모라 전체를 훑어도 부담이 없고, 제목 생성 규칙을
    SQL 에 복제하지 않아 한 곳만 고치면 된다.
    """
    for r in rows(db, """
        SELECT c.id, c.name AS cluster_name, c.purpose, c.wiki_title,
               c.confluence_page_id, cu.name AS customer_name
        FROM cluster c JOIN customer cu ON cu.id = c.customer_id
        WHERE c.id <> :id AND c.status <> 'DECOMM'
    """, id=cluster_id):
        if page_body.page_title(r) == title:
            return r
    return None


def _log(db: Session, cluster_id: int, status: str, who: str | None,
         page_id: str | None = None, version: int | None = None,
         message: str | None = None) -> None:
    run(db, """
        INSERT INTO publish_log
          (cluster_id, page_id, page_version, status, message, published_by)
        VALUES (:c, :p, :v, :s, :m, :w)
    """, c=cluster_id, p=page_id, v=version, s=status,
         m=(message or "")[:2000] or None, w=who)


# ===================================================================== 미리보기


@router.get("/cluster/{cluster_id}/publish", response_class=HTMLResponse)
def publish_preview(cluster_id: int, request: Request, db: Session = Depends(get_db)):
    from .main import templates

    data = _gather(db, cluster_id)
    title, body = _build(data)

    return templates.TemplateResponse(request=request, name="publish.html", context={
        "basic": data["basic"],
        "spec": data["spec"],
        "title": title,
        "body": body,
        "missing": _missing(data),
        "conflict": _title_conflict(db, cluster_id, title),
        "configured": confluence.configured(),
        "base_url": confluence.BASE_URL,
        "history": rows(db, """
            SELECT published_at, page_id, page_version, status, message, published_by
            FROM publish_log WHERE cluster_id = :id
            ORDER BY id DESC LIMIT 10
        """, id=cluster_id),
    })


def _missing(data: dict) -> list[str]:
    """비어 있으면 위키에 '-' 로 나갈 항목. 막지는 않고 알려만 준다."""
    b, out = data["basic"], []
    if not b.get("cluster_size"):      out.append("Cluster size (장비 구성)")
    if not b.get("vast_sw_release"):   out.append("VAST SW Release")
    if b.get("usable_total_tb") is None: out.append("용량 (Usable)")
    if not data["licenses"]:           out.append("라이선스")
    if not data["firmware"]:           out.append("펌웨어")
    if not data["network"]:            out.append("네트워크")
    if not data["protocols"]:          out.append("Protocol")
    return out


# ===================================================================== 발행


@router.post("/cluster/{cluster_id}/publish")
def publish_now(cluster_id: int, db: Session = Depends(get_db),
                published_by: str = Form(""), note: str = Form("")):
    data = _gather(db, cluster_id)
    title, body = _build(data)
    spec = data["spec"]
    who = (published_by or "").strip() or None

    # 제목이 겹치면 발행하지 않는다. 그대로 두면 남의 페이지를 덮어쓴다.
    other = _title_conflict(db, cluster_id, title)
    if other:
        _log(db, cluster_id, "FAILED", who,
             page_id=spec.get("confluence_page_id"),
             message=(f"페이지 제목 '{title}' 이 "
                      f"{other['customer_name']} / {other['cluster_name']} 와 겹칩니다. "
                      "위키 페이지 제목을 직접 지정하세요."))
        db.commit()
        return RedirectResponse(f"/cluster/{cluster_id}/publish?err=1", status_code=303)

    try:
        result = confluence.publish(
            space_key=spec.get("confluence_space") or "CPFDS",
            page_id=spec.get("confluence_page_id"),
            title=title,
            body=body,
            note=(note or "").strip() or "포털에서 발행",
        )
    except confluence.ConfluenceError as e:
        _log(db, cluster_id, "FAILED", who,
             page_id=spec.get("confluence_page_id"), message=str(e))
        db.commit()
        return RedirectResponse(
            f"/cluster/{cluster_id}/publish?err=1", status_code=303)

    # page_id 는 첫 발행에서만 바뀐다. 매번 UPDATE 하면 cluster.updated_at 이
    # 올라가서 v_publish_pending 이 '발행 필요' 로 되돌아간다.
    if result["id"] != (spec.get("confluence_page_id") or ""):
        run(db, "UPDATE cluster SET confluence_page_id = :p WHERE id = :id",
            p=result["id"], id=cluster_id)

    _log(db, cluster_id, "OK", who,
         page_id=result["id"], version=result["version"],
         message=("새 페이지 생성" if result.get("created") else "갱신"))
    db.commit()
    return RedirectResponse(f"/cluster/{cluster_id}/publish?ok=1", status_code=303)


# ===================================================================== 제목 지정


@router.post("/cluster/{cluster_id}/wiki-title")
def set_wiki_title(cluster_id: int, db: Session = Depends(get_db),
                   wiki_title: str = Form("")):
    """페이지 제목을 직접 정한다. 비우면 자동 생성으로 돌아간다.

    이미 발행한 페이지의 제목을 바꾸면, 다음 발행 때 page_id 를 따라가
    그 페이지의 제목이 바뀐다(새 페이지가 생기지 않는다).
    """
    t = (wiki_title or "").strip() or None

    if t:
        dup = one(db, """
            SELECT c.name AS cluster_name, cu.name AS customer_name
            FROM cluster c JOIN customer cu ON cu.id = c.customer_id
            WHERE c.wiki_title = :t AND c.id <> :id
        """, t=t, id=cluster_id)
        if dup:
            return RedirectResponse(
                f"/cluster/{cluster_id}/publish?titledup=1", status_code=303)

    old = scalar(db, "SELECT wiki_title FROM cluster WHERE id = :id", id=cluster_id)
    run(db, "UPDATE cluster SET wiki_title = :t WHERE id = :id", t=t, id=cluster_id)
    audit.log(db, cluster_id, "UPDATE", "cluster", cluster_id,
              "wiki_title", old, t)
    db.commit()
    return RedirectResponse(f"/cluster/{cluster_id}/publish", status_code=303)


# ===================================================================== 발행 대기


@router.get("/publish", response_class=HTMLResponse)
def publish_queue(request: Request, db: Session = Depends(get_db)):
    from .main import templates

    return templates.TemplateResponse(request=request, name="publish_queue.html", context={
        "pending": rows(db, "SELECT * FROM v_publish_pending ORDER BY customer_name"),
        "failed": rows(db, """
            SELECT p.cluster_id, cu.name AS customer_name, c.name AS cluster_name,
                   p.published_at, p.message, p.published_by
            FROM publish_log p
            JOIN cluster c  ON c.id = p.cluster_id
            JOIN customer cu ON cu.id = c.customer_id
            WHERE p.status = 'FAILED'
              AND p.id = (SELECT MAX(p2.id) FROM publish_log p2
                           WHERE p2.cluster_id = p.cluster_id)
            ORDER BY p.published_at DESC
        """),
        "configured": confluence.configured(),
    })


# ===================================================================== 연결 점검


@router.get("/api/confluence/check")
def confluence_check():
    """설정이 맞는지 화면에서 바로 확인한다. 토큰은 응답에 담지 않는다."""
    if not confluence.configured():
        return {"ok": False, "reason": "CONFLUENCE_EMAIL / CONFLUENCE_TOKEN 미설정"}
    try:
        me = confluence.whoami()
    except confluence.ConfluenceError as e:
        return {"ok": False, "reason": str(e)}
    return {
        "ok": True,
        "base_url": confluence.BASE_URL,
        "account": me.get("email") or me.get("username"),
        "display_name": me.get("displayName"),
    }


# ===================================================================== 전체 현황
#
# 클러스터별 페이지와 달리 이 페이지는 '한 장에 전부' 다. 그래서 다르게 다룬다.
#
#   대상 페이지가 고정이다        클러스터처럼 새로 만들지 않는다. 이미 있는
#                                 페이지(PARENT)를 갱신한다.
#   통째로 교체한다               포털에 없는 고객사의 행은 사라진다. 지금 위키에
#                                 몇 행이 있는지 읽어와 비교해서 보여준다.
#   줄어들 때만 확인을 받는다     행이 줄지 않는 발행은 버튼 한 번이면 된다.
#                                 줄어들 때만 OVERWRITE 를 입력하게 한다.
#
# 쿼리는 포털 목록(main.LIST_SQL)과 같은 뷰·같은 열을 쓴다. 위키 표를 포털
# 목록과 같게 맞췄으므로, 두 화면이 다른 값을 보일 여지를 남기지 않는다.

OVERVIEW_SQL = """
SELECT b.cluster_id, b.customer_name, b.cluster_name, b.psnt, b.status,
       b.cluster_size, b.cluster_detail, b.vast_sw_release,
       b.usable_total_tb, b.used_pct,
       b.drr, b.drr_status, b.last_update, b.days_since_update,
       c.remark, c.confluence_page_id
FROM v_cluster_basic b
JOIN cluster c ON c.id = b.cluster_id
WHERE b.status <> 'DECOMM'
ORDER BY b.customer_name, b.cluster_name
"""

# 고객사 단위 메모. 클러스터가 아직 없는 고객사도 나와야 하므로 customer 에서 뽑는다.
MEMO_SQL = """
SELECT name, remark FROM customer
WHERE status = 'ACTIVE' AND remark IS NOT NULL AND TRIM(remark) <> ''
ORDER BY name
"""


def _overview(db: Session) -> tuple[list[dict], list[dict], str]:
    clusters = rows(db, OVERVIEW_SQL)
    memos = rows(db, MEMO_SQL)
    return clusters, memos, page_body.overview_build(clusters, memos)


def _wiki_state(page_id: str) -> dict:
    """대상 페이지의 현재 상태. 읽기 실패는 발행을 막지 않고 알리기만 한다."""
    if not page_id:
        return {"error": "대상 페이지가 지정되지 않았습니다. "
                         "CONFLUENCE_PARENT_ID 또는 CONFLUENCE_OVERVIEW_ID 를 확인하세요."}
    if not confluence.configured():
        return {"error": "Confluence 인증 정보가 없습니다."}
    try:
        cur = confluence.get_page(page_id)
        body = confluence.get_page_body(page_id)
    except confluence.ConfluenceError as e:
        return {"error": str(e)}
    return {
        "id": cur["id"], "title": cur["title"], "url": cur["url"],
        "version": cur["version"], "status": cur["status"],
        "rows": page_body.count_table_rows(body),
    }


def _overview_log(db: Session, status: str, who: str | None,
                  page_id: str | None = None, version: int | None = None,
                  message: str | None = None) -> None:
    """cluster_id 가 NULL 인 발행 기록. 전체 현황 발행이라는 뜻이다."""
    run(db, """
        INSERT INTO publish_log
          (cluster_id, page_id, page_version, status, message, published_by)
        VALUES (NULL, :p, :v, :s, :m, :w)
    """, p=page_id, v=version, s=status,
         m=(message or "")[:2000] or None, w=who)


@router.get("/publish/overview", response_class=HTMLResponse)
def overview_preview(request: Request, db: Session = Depends(get_db)):
    from .main import templates

    clusters, memos, body = _overview(db)
    target = confluence.overview_page_id()
    wiki = _wiki_state(target)

    return templates.TemplateResponse(request=request, name="publish_overview.html",
        context={
            "clusters": clusters,
            "memos": memos,
            "body": body,
            "wiki": wiki,
            "target": target,
            "configured": confluence.configured(),
            "base_url": confluence.BASE_URL,
            "cols": [h for h, _ in page_body.OVERVIEW_COLS],
            "history": rows(db, """
                SELECT published_at, page_id, page_version, status, message, published_by
                FROM publish_log WHERE cluster_id IS NULL
                ORDER BY id DESC LIMIT 10
            """),
        })


@router.post("/publish/overview")
def overview_publish(db: Session = Depends(get_db),
                     published_by: str = Form(""), note: str = Form(""),
                     confirm: str = Form("")):
    clusters, _memos, body = _overview(db)
    who = (published_by or "").strip() or None
    target = confluence.overview_page_id()
    wiki = _wiki_state(target)

    if wiki.get("error"):
        _overview_log(db, "FAILED", who, page_id=target or None,
                      message=f"대상 페이지를 읽을 수 없습니다: {wiki['error']}")
        db.commit()
        return RedirectResponse("/publish/overview?err=1", status_code=303)

    # 행이 줄어드는 발행만 확인 문구를 받는다. 이관이 끝나 행 수가 안정되면
    # 매번 타이핑할 이유가 없다 — 위험한 경우에만 손이 가게 한다.
    losing = wiki["rows"] - len(clusters)
    if losing > 0 and confirm.strip().upper() != "OVERWRITE":
        return RedirectResponse(
            f"/publish/overview?needconfirm={losing}", status_code=303)

    try:
        result = confluence.update_page(
            wiki["id"], wiki["title"], body, wiki["version"],
            note=(note or "").strip() or "포털에서 전체 현황 발행",
        )
    except confluence.ConfluenceError as e:
        _overview_log(db, "FAILED", who, page_id=wiki["id"], message=str(e))
        db.commit()
        return RedirectResponse("/publish/overview?err=1", status_code=303)

    _overview_log(db, "OK", who, page_id=result["id"], version=result["version"],
                  message=f"{len(clusters)}행 발행"
                          + (f" (이전 {wiki['rows']}행)" if wiki["rows"] else ""))
    db.commit()
    return RedirectResponse("/publish/overview?ok=1", status_code=303)
