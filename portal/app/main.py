"""VAST 고객사 사용현황 포털.

데이터 흐름: 웹 폼 → DB → (추후) Confluence 발행
DB 가 원본이고 위키는 출력물이다.

1단계 범위: 고객사/클러스터 기본 정보 + 장비 구성 입력
"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import quote

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import text
from sqlalchemy.orm import Session

from . import audit
from .db import get_db, one, rows, run, scalar

BASE_DIR = Path(__file__).resolve().parent

# 배포된 코드가 어느 판인지 눈으로 확인하기 위한 표시.
# 이미지 태그만으로는 알 수 없다 — 같은 태그로 덮어 빌드하거나, 예전 소스로
# 새 태그를 빌드하면 태그와 내용이 어긋난다. 실제로 그 때문에 한 번 헤맸다.
#   확인: curl -s http://<호스트>/healthz
APP_VERSION = "2026-10-21.1"   # 전체 현황 본문 단계별 발행(500 원인 추적)

app = FastAPI(title="VAST 고객사 사용현황",
              docs_url="/api/docs", openapi_url="/api/openapi.json")
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "templates")
# 모든 화면 머리글에 판 번호를 띄운다. 라우트마다 넘기지 않아도 되게 전역으로 둔다.
templates.env.globals["app_version"] = APP_VERSION

from . import routes_data, routes_publish   # noqa: E402  (templates 정의 뒤에 와야 한다)
app.include_router(routes_data.router)
app.include_router(routes_publish.router)


@app.get("/healthz")
def healthz(db: Session = Depends(get_db)):
    db.execute(text("SELECT 1"))
    return {"status": "ok", "version": APP_VERSION}


# ===================================================================== 목록

LIST_SQL = """
SELECT b.*, l.license_status, l.end_date AS license_end, l.days_left
FROM v_cluster_basic b
LEFT JOIN license lc ON lc.cluster_id = b.cluster_id
  AND lc.id = (SELECT l2.id FROM license l2
                WHERE l2.cluster_id = b.cluster_id
                ORDER BY (l2.end_date IS NULL), l2.end_date DESC, l2.id DESC
                LIMIT 1)
LEFT JOIN v_license_status l ON l.id = lc.id
WHERE (:q = '' OR b.customer_name LIKE :like OR b.cluster_name LIKE :like
       OR b.psnt LIKE :like OR b.purpose LIKE :like)
ORDER BY b.customer_name, b.cluster_name
"""

STATS_SQL = """
SELECT
  (SELECT COUNT(*) FROM customer WHERE status='ACTIVE')            AS customers,
  (SELECT COUNT(*) FROM cluster  WHERE status<>'DECOMM')           AS clusters,
  (SELECT COUNT(*) FROM v_license_status
     WHERE license_status IN ('EXPIRING','EXPIRED'))               AS license_alert,
  (SELECT COUNT(*) FROM v_stale_clusters)                          AS stale,
  (SELECT COUNT(*) FROM v_publish_pending)                         AS publish_pending
"""


@app.get("/", response_class=HTMLResponse)
def index(request: Request, q: str = "", db: Session = Depends(get_db)):
    ctx = {
        "request": request,
        "clusters": rows(db, LIST_SQL, q=q, like=f"%{q}%"),
        "stats": one(db, STATS_SQL),
        "q": q,
    }
    tpl = "_table.html" if request.headers.get("HX-Request") else "index.html"
    return templates.TemplateResponse(request=request, name=tpl, context=ctx)


# ===================================================================== 폼

KIND_LABEL = {"C": "CBox", "D": "DBox", "E": "EBox", "S": "스위치"}


def _form_context(db, cluster=None):
    """신규/수정 폼이 공통으로 쓰는 컨텍스트."""
    return {
        "cluster": cluster,
        # 고객사 목록은 넘기지 않는다. 폼이 드롭다운이 아니라 자동완성 입력란이고,
        # 후보는 /api/customers/suggest 가 입력에 맞춰 그때그때 돌려준다.
        "models": rows(db, """
            SELECT id, kind, name, vendor FROM hardware_model
            WHERE is_active = 1 ORDER BY kind, sort_order, name
        """),
        "kind_label": KIND_LABEL,
        "protocol_options": ["NFS", "SMB", "S3"],
        "service_type_options": ["Ethernet", "RoCE", "IB"],
    }


@app.get("/cluster/new", response_class=HTMLResponse)
def cluster_new(request: Request, db: Session = Depends(get_db)):
    ctx = _form_context(db)
    ctx.update({"hardware": [], "protocols": [], "service_types": [], "mode": "new"})
    return templates.TemplateResponse(request=request, name="cluster_form.html", context=ctx)


@app.get("/cluster/{cluster_id}/edit", response_class=HTMLResponse)
def cluster_edit(cluster_id: int, request: Request, db: Session = Depends(get_db)):
    c = one(db, """
        SELECT c.*, cu.name AS customer_name
        FROM cluster c JOIN customer cu ON cu.id = c.customer_id
        WHERE c.id = :id
    """, id=cluster_id)
    if not c:
        raise HTTPException(404, "클러스터를 찾을 수 없습니다")

    ctx = _form_context(db, c)
    ctx.update({
        "mode": "edit",
        "tab": "basic",
        "hardware": rows(db, """
            SELECT * FROM cluster_hardware WHERE cluster_id = :id
            ORDER BY FIELD(kind,'C','D','E','S'), sort_order, id
        """, id=cluster_id),
        "protocols": [r["protocol"] for r in rows(
            db, "SELECT protocol FROM cluster_protocol WHERE cluster_id=:id", id=cluster_id)],
        "service_types": [r["service_type"] for r in rows(
            db, "SELECT service_type FROM cluster_service_type WHERE cluster_id=:id", id=cluster_id)],
    })
    return templates.TemplateResponse(request=request, name="cluster_form.html", context=ctx)


# ===================================================================== 저장


def _blank_to_none(v):
    if v is None:
        return None
    v = v.strip() if isinstance(v, str) else v
    return v if v != "" else None


@app.post("/cluster/save")
async def cluster_save(request: Request, db: Session = Depends(get_db)):
    """기본 정보 + 장비를 한 번에 저장한다.

    장비는 폼에서 행을 자유롭게 추가/삭제하므로 전체 교체한다.
    부분 병합은 '폼에서 지운 행'을 표현할 방법이 없어 맞지 않는다.
    """
    f = await request.form()

    cluster_id = _blank_to_none(f.get("cluster_id"))
    cluster_id = int(cluster_id) if cluster_id else None
    by = (f.get("updated_by") or "").strip()

    # ---- 고객사: 이름으로 찾고, 없으면 만든다.
    #
    # 드롭다운과 '새 고객사' 입력란을 자동완성 입력란 하나로 합쳤다. 고르는 것과
    # 새로 적는 것이 같은 칸이 되어 customer_id 를 폼에서 받지 않는다.
    # 이름이 곧 식별자다 — customer.uk_customer_name 이 중복을 막아준다.
    #
    # 오타가 새 고객사를 만드는 문제는 폼에서 막는다. 입력 중에 기존 고객사와
    # 맞는지 보여주므로, 저장 전에 눈으로 확인할 수 있다.
    cname = _blank_to_none(f.get("customer_name"))
    if not cname:
        raise HTTPException(400, "고객사명을 입력하세요")

    customer_id = scalar(db, "SELECT id FROM customer WHERE name = :n", n=cname)
    if customer_id:
        # 숨겨둔(INACTIVE) 고객사에 클러스터를 다시 붙이면 쓰는 중인 것이다.
        run(db, "UPDATE customer SET status='ACTIVE' WHERE id=:i AND status<>'ACTIVE'",
            i=customer_id)
    else:
        run(db, "INSERT INTO customer (name, updated_by) VALUES (:n, NULLIF(:by,''))",
            n=cname, by=by)
        customer_id = scalar(db, "SELECT LAST_INSERT_ID()")
    customer_id = int(customer_id)

    payload = dict(
        customer_id=customer_id,
        name=(f.get("name") or "").strip(),
        psnt=_blank_to_none(f.get("psnt")),
        purpose=_blank_to_none(f.get("purpose")),
        site=_blank_to_none(f.get("site")),
        model_type=f.get("model_type") or "CD",
        cur_release=_blank_to_none(f.get("cur_release")),
        service_nic=_blank_to_none(f.get("service_nic")),
        mgmt_type=_blank_to_none(f.get("mgmt_type")),
        ipmi_type=_blank_to_none(f.get("ipmi_type")),
        guaranteed_drr=_blank_to_none(f.get("guaranteed_drr")),
        install_date=_blank_to_none(f.get("install_date")),
        status=f.get("status") or "PROD",
        support_status=f.get("support_status") or "DIRECT",
        remark=_blank_to_none(f.get("remark")),
        confluence_space=(f.get("confluence_space") or "CPFDS").strip(),
        confluence_page_id=_blank_to_none(f.get("confluence_page_id")),
    )
    if not payload["name"]:
        raise HTTPException(400, "Cluster Name 은 필수입니다")

    before = None
    if cluster_id:
        before = one(db, "SELECT * FROM cluster WHERE id = :id", id=cluster_id)
        if not before:
            raise HTTPException(404)
        run(db, """
            UPDATE cluster SET
              customer_id=:customer_id, name=:name, psnt=:psnt, purpose=:purpose, site=:site,
              model_type=:model_type, cur_release=:cur_release, service_nic=:service_nic,
              mgmt_type=:mgmt_type, ipmi_type=:ipmi_type, guaranteed_drr=:guaranteed_drr,
              install_date=:install_date, status=:status, support_status=:support_status,
              remark=:remark, confluence_space=:confluence_space,
              confluence_page_id=:confluence_page_id, updated_by=NULLIF(:by,'')
            WHERE id=:id
        """, **payload, by=by, id=cluster_id)
    else:
        run(db, """
            INSERT INTO cluster
              (customer_id, name, psnt, purpose, site, model_type, cur_release,
               service_nic, mgmt_type, ipmi_type, guaranteed_drr, install_date,
               status, support_status, remark, confluence_space, confluence_page_id, updated_by)
            VALUES
              (:customer_id,:name,:psnt,:purpose,:site,:model_type,:cur_release,
               :service_nic,:mgmt_type,:ipmi_type,:guaranteed_drr,:install_date,
               :status,:support_status,:remark,:confluence_space,:confluence_page_id,NULLIF(:by,''))
        """, **payload, by=by)
        cluster_id = int(scalar(db, "SELECT LAST_INSERT_ID()"))

    audit.log_diff(db, cluster_id, "cluster", cluster_id, before, payload, by)

    # ---- 프로토콜 / 서비스 타입: 전체 교체
    run(db, "DELETE FROM cluster_protocol WHERE cluster_id=:id", id=cluster_id)
    for p in f.getlist("protocol"):
        run(db, "INSERT IGNORE INTO cluster_protocol (cluster_id, protocol) VALUES (:c,:p)",
            c=cluster_id, p=p)

    run(db, "DELETE FROM cluster_service_type WHERE cluster_id=:id", id=cluster_id)
    for s in f.getlist("service_type"):
        run(db, "INSERT IGNORE INTO cluster_service_type (cluster_id, service_type) VALUES (:c,:s)",
            c=cluster_id, s=s)

    # ---- 장비: 전체 교체
    old_hw = rows(db, """
        SELECT kind, model_name, quantity, capacity_note FROM cluster_hardware
        WHERE cluster_id=:id ORDER BY FIELD(kind,'C','D','E','S'), sort_order, id
    """, id=cluster_id)
    run(db, "DELETE FROM cluster_hardware WHERE cluster_id=:id", id=cluster_id)

    kinds = f.getlist("hw_kind")
    model_ids = f.getlist("hw_model_id")     # '' 또는 '__other__' 또는 숫자
    model_etc = f.getlist("hw_model_etc")
    quantities = f.getlist("hw_quantity")
    capacities = f.getlist("hw_capacity")

    new_hw = []
    for i, kind in enumerate(kinds):
        raw_id = model_ids[i] if i < len(model_ids) else ""
        etc = (model_etc[i] if i < len(model_etc) else "").strip()
        qty = (quantities[i] if i < len(quantities) else "").strip()
        cap = _blank_to_none(capacities[i] if i < len(capacities) else "")

        if raw_id == "__other__":
            model_id, model_name = None, etc
        elif raw_id.isdigit():
            model_id = int(raw_id)
            model_name = scalar(db, "SELECT name FROM hardware_model WHERE id=:i", i=model_id)
        else:
            continue                       # 모델을 고르지 않은 빈 행은 건너뛴다

        if not model_name or not qty.isdigit() or int(qty) < 1:
            continue

        run(db, """
            INSERT INTO cluster_hardware
              (cluster_id, kind, model_id, model_name, quantity, capacity_note, sort_order, updated_by)
            VALUES (:c,:k,:mid,:mn,:q,:cap,:so,NULLIF(:by,''))
        """, c=cluster_id, k=kind, mid=model_id, mn=model_name,
             q=int(qty), cap=cap, so=i, by=by)
        new_hw.append({"kind": kind, "model_name": model_name,
                       "quantity": int(qty), "capacity_note": cap})

    def hw_label(items):
        return ", ".join(f"{h['model_name']}({h['quantity']})" for h in items) or "(없음)"

    if hw_label(old_hw) != hw_label(new_hw):
        audit.log(db, cluster_id, "UPDATE", "cluster_hardware", None, "구성",
                  hw_label(old_hw), hw_label(new_hw), by)

    db.commit()
    return RedirectResponse(f"/cluster/{cluster_id}", status_code=303)


# ===================================================================== 상세


@app.get("/cluster/{cluster_id}", response_class=HTMLResponse)
def cluster_detail(cluster_id: int, request: Request, db: Session = Depends(get_db)):
    basic = one(db, "SELECT * FROM v_cluster_basic WHERE cluster_id = :id", id=cluster_id)
    if not basic:
        raise HTTPException(404, "클러스터를 찾을 수 없습니다")

    return templates.TemplateResponse(request=request, name="cluster.html", context={
        "basic": basic,
        "spec": one(db, """
            SELECT c.*, cu.name AS customer_name
            FROM cluster c JOIN customer cu ON cu.id = c.customer_id
            WHERE c.id = :id
        """, id=cluster_id),
        "kind_label": KIND_LABEL,
        "hardware": rows(db, """
            SELECT * FROM cluster_hardware WHERE cluster_id = :id
            ORDER BY FIELD(kind,'C','D','E','S'), sort_order, id
        """, id=cluster_id),
        "units": rows(db, """
            SELECT kind, model_name, seq, serial_no, part_no, remark
            FROM hardware_unit WHERE cluster_id = :id
            ORDER BY FIELD(kind,'C','D','E','S'), model_name, seq
        """, id=cluster_id),
        # 장비 행마다 '몇 대 중 몇 대 등록' 을 보여주기 위한 집계
        "fill": {(r["kind"], r["model_name"]): r["filled"] for r in rows(db, """
            SELECT kind, model_name, filled FROM v_hardware_unit_fill
            WHERE cluster_id = :id
        """, id=cluster_id)},
        "protocols": [r["protocol"] for r in rows(
            db, "SELECT protocol FROM cluster_protocol WHERE cluster_id=:id", id=cluster_id)],
        "service_types": [r["service_type"] for r in rows(
            db, "SELECT service_type FROM cluster_service_type WHERE cluster_id=:id", id=cluster_id)],
        "network": rows(db, """
            SELECT wiki_label, role, fabric, addresses
            FROM v_network_label WHERE cluster_id = :id ORDER BY sort_order
        """, id=cluster_id),
        "licenses": rows(db, """
            SELECT l.*, s.license_status, s.days_left
            FROM license l LEFT JOIN v_license_status s ON s.id = l.id
            WHERE l.cluster_id = :id ORDER BY l.sort_order, l.id
        """, id=cluster_id),
        "firmware": rows(db, """
            SELECT component, model, version, checked_at FROM v_firmware_current
            WHERE cluster_id = :id
            ORDER BY FIELD(component,'OS','SCM','SSD','SWITCH','BMC','NIC','OTHER')
        """, id=cluster_id),
        "snapshots": rows(db, """
            SELECT * FROM capacity_snapshot WHERE cluster_id = :id
            ORDER BY measured_at DESC LIMIT 12
        """, id=cluster_id),
        "changes": rows(db, """
            SELECT changed_at, changed_by, action, table_name, field_name, old_value, new_value
            FROM change_log WHERE cluster_id = :id
            ORDER BY changed_at DESC, id DESC LIMIT 20
        """, id=cluster_id),
    })


# ===================================================================== 삭제 / 철거
#
# 두 갈래로 나눈 이유
#   운영에서 필요한 건 대부분 '목록에서 치우기' 이지 '기록 지우기' 가 아니다.
#   철거한 클러스터도 언제 무엇이 있었는지는 남아야 한다. 그래서 DECOMM 을
#   기본 경로로 두고, 완전 삭제는 PSNT 를 직접 입력해야 되게 했다.
#
#   인증이 없는 사내 포털이라 버튼 한 번에 지워지면 안 된다.
#   되돌릴 수 없는 작업에는 그만큼의 손이 가야 한다.


DELETE_COUNT_SQL = """
SELECT
  (SELECT COUNT(*) FROM cluster_hardware     WHERE cluster_id=:id) AS hardware,
  (SELECT COUNT(*) FROM cluster_network      WHERE cluster_id=:id) AS network,
  (SELECT COUNT(*) FROM license              WHERE cluster_id=:id) AS license,
  (SELECT COUNT(*) FROM cluster_firmware     WHERE cluster_id=:id) AS firmware,
  (SELECT COUNT(*) FROM capacity_snapshot    WHERE cluster_id=:id) AS capacity,
  (SELECT COUNT(*) FROM change_log           WHERE cluster_id=:id) AS changes,
  (SELECT COUNT(*) FROM publish_log          WHERE cluster_id=:id) AS publishes
"""


@app.get("/cluster/{cluster_id}/delete", response_class=HTMLResponse)
def cluster_delete_confirm(cluster_id: int, request: Request,
                           db: Session = Depends(get_db)):
    c = one(db, """
        SELECT c.*, cu.name AS customer_name,
               (SELECT COUNT(*) FROM cluster c2 WHERE c2.customer_id = c.customer_id)
                 AS sibling_count
        FROM cluster c JOIN customer cu ON cu.id = c.customer_id
        WHERE c.id = :id
    """, id=cluster_id)
    if not c:
        raise HTTPException(404, "클러스터를 찾을 수 없습니다")

    return templates.TemplateResponse(request=request, name="cluster_delete.html",
        context={"c": c, "counts": one(db, DELETE_COUNT_SQL, id=cluster_id)})


@app.post("/cluster/{cluster_id}/decomm")
def cluster_decomm(cluster_id: int, db: Session = Depends(get_db),
                   changed_by: str = Form("")):
    """철거 처리. 행은 그대로 두고 상태만 바꾼다.

    목록과 발행 대기에서 빠지고, 상세 화면에서는 계속 볼 수 있다.
    """
    old = scalar(db, "SELECT status FROM cluster WHERE id = :id", id=cluster_id)
    if old is None:
        raise HTTPException(404, "클러스터를 찾을 수 없습니다")
    if old != "DECOMM":
        run(db, "UPDATE cluster SET status='DECOMM', updated_by=NULLIF(:by,'') "
                "WHERE id = :id", id=cluster_id, by=changed_by.strip())
        audit.log(db, cluster_id, "UPDATE", "cluster", cluster_id,
                  "status", old, "DECOMM", by=changed_by.strip())
        db.commit()
    return RedirectResponse(f"/cluster/{cluster_id}", status_code=303)


@app.post("/cluster/{cluster_id}/delete")
def cluster_delete(cluster_id: int, db: Session = Depends(get_db),
                   confirm: str = Form("")):
    """완전 삭제. 자녀 행은 FK CASCADE 로 함께 지워진다(이력 포함).

    confirm 에 'DELETE' 를 입력해야 진행한다.
    PSNT 를 쓰지 않는 이유: 미입력인 클러스터가 있어 확인 문구가 없을 수 있다.
    고정 문구면 어떤 클러스터에서나 같은 절차가 된다.
    """
    c = one(db, "SELECT id, name, psnt, customer_id FROM cluster WHERE id = :id",
            id=cluster_id)
    if not c:
        raise HTTPException(404, "클러스터를 찾을 수 없습니다")

    if confirm.strip().upper() != "DELETE":
        return RedirectResponse(f"/cluster/{cluster_id}/delete?mismatch=1",
                                status_code=303)

    customer_id = c["customer_id"]
    run(db, "DELETE FROM cluster WHERE id = :id", id=cluster_id)

    # 고객사는 FK RESTRICT 라 자동으로 지워지지 않는다. 클러스터가 하나도
    # 남지 않으면 빈 고객사가 목록에 떠다니므로 같이 정리한다.
    left = scalar(db, "SELECT COUNT(*) FROM cluster WHERE customer_id = :c",
                  c=customer_id)
    if not left:
        run(db, "DELETE FROM customer WHERE id = :c", c=customer_id)

    db.commit()
    return RedirectResponse("/", status_code=303)


# ===================================================================== 모델 관리


@app.get("/models", response_class=HTMLResponse)
def models_page(request: Request, db: Session = Depends(get_db)):
    return templates.TemplateResponse(request=request, name="models.html", context={
        "kind_label": KIND_LABEL,
        "models": rows(db, """
            SELECT m.*, (SELECT COUNT(*) FROM cluster_hardware h WHERE h.model_id = m.id) AS used
            FROM hardware_model m ORDER BY m.kind, m.sort_order, m.name
        """),
    })


@app.post("/models/add")
def model_add(kind: str = Form(...), name: str = Form(...),
              vendor: str = Form(""), db: Session = Depends(get_db)):
    name = name.strip()
    if name:
        run(db, """
            INSERT IGNORE INTO hardware_model (kind, name, vendor, sort_order)
            VALUES (:k, :n, NULLIF(:v,''),
                    COALESCE((SELECT mx FROM (SELECT MAX(sort_order)+10 AS mx
                              FROM hardware_model WHERE kind=:k) t), 10))
        """, k=kind, n=name, v=vendor.strip())
        db.commit()
    return RedirectResponse("/models", status_code=303)


@app.post("/models/{model_id}/toggle")
def model_toggle(model_id: int, db: Session = Depends(get_db)):
    """목록에서 숨기거나 다시 표시한다. 행은 그대로 둔다.

    쓰던 모델을 단종 처리할 때 쓴다. 드롭다운에서만 빠지고, 이미 그 모델로
    등록된 클러스터는 아무 영향이 없다.
    """
    run(db, "UPDATE hardware_model SET is_active = 1 - is_active WHERE id = :i", i=model_id)
    db.commit()
    return RedirectResponse("/models", status_code=303)


@app.post("/models/{model_id}/delete")
def model_delete(model_id: int, db: Session = Depends(get_db)):
    """모델 행을 지운다.

    쓰고 있는 클러스터가 있어도 데이터는 잃지 않는다.
    cluster_hardware 는 model_id(참조)와 model_name(이름)을 따로 갖고 있고,
    FK 가 ON DELETE SET NULL 이라 참조만 끊기고 이름은 그대로 남는다.
    그 장비는 화면에서 '직접 입력' 으로 표시된다.

    그래도 쓰는 중이면 드롭다운에서만 빼는 '숨기기' 가 대개 맞다.
    어느 쪽인지는 화면에서 사용 건수를 보고 사람이 정한다.
    """
    used = scalar(db, "SELECT COUNT(*) FROM cluster_hardware WHERE model_id = :i", i=model_id)
    run(db, "DELETE FROM hardware_model WHERE id = :i", i=model_id)
    db.commit()
    return RedirectResponse(f"/models?deleted={used or 0}", status_code=303)


# ===================================================================== 고객사 관리
#
# 고객사는 클러스터를 저장할 때 자동으로 생긴다. 그래서 이 화면에 '추가' 는 없고,
# 잘못 생긴 것을 고치거나 치우는 일만 한다.
#
# 수정과 삭제를 둘 다 두는 이유
#   오타 고객사가 두 모양으로 나온다.
#     (1) 클러스터가 붙어 있는 오타  — 클러스터를 다른 고객사로 옮기면 생긴다.
#         이때 필요한 건 삭제가 아니라 이름 고치기다. 지우면 클러스터가 떠버린다.
#     (2) 클러스터가 0 건인 오타     — 지우면 된다.
#   둘이 섞여 있어서 한 화면에서 건수를 보고 사람이 고르게 한다.


@app.get("/customers", response_class=HTMLResponse)
def customers_page(request: Request, db: Session = Depends(get_db)):
    return templates.TemplateResponse(request=request, name="customers.html", context={
        "customers": rows(db, """
            SELECT cu.id, cu.name, cu.status, cu.remark,
                   (SELECT COUNT(*) FROM cluster c WHERE c.customer_id = cu.id) AS clusters
            FROM customer cu ORDER BY cu.name
        """),
    })


@app.post("/customers/{customer_id}/update")
def customer_update(customer_id: int, name: str = Form(...), remark: str = Form(""),
                    db: Session = Depends(get_db)):
    """고객사명과 메모를 고친다.

    이름 변경은 클러스터가 붙어 있어도 안전하다. 클러스터는 customer_id 로
    연결돼 있어서 이름만 바뀌고 연결은 그대로다. 다만 발행된 위키 페이지의
    제목은 자동으로 바뀌지 않으므로, 제목에 고객사명이 들어간 클러스터는
    다시 발행해야 한다.

    메모는 전체 현황 페이지의 표 아래 '고객사 메모' 로 나간다. 그 페이지는
    통째로 덮어쓰기라, 위키에 손으로 적은 메모는 다음 발행 때 사라진다.
    남아야 하는 메모가 들어올 자리가 여기다 — 특히 포털에 아직 클러스터가
    없는 고객사는 표에 행이 없어서 이 메모가 유일한 통로다.
    """
    cur = one(db, "SELECT name, remark FROM customer WHERE id = :i", i=customer_id)
    if not cur:
        raise HTTPException(404, "고객사를 찾을 수 없습니다")

    new = name.strip()
    memo = remark.strip() or None
    if not new:
        return RedirectResponse("/customers?err=empty", status_code=303)

    renamed = new != cur["name"]
    if renamed and scalar(db, "SELECT id FROM customer WHERE name = :n AND id <> :i",
                          n=new, i=customer_id):
        # uk_customer_name 이 막아주지만, 500 대신 이유를 보여준다.
        return RedirectResponse(f"/customers?err=dup&name={quote(new)}", status_code=303)

    if not renamed and memo == (cur["remark"] or None):
        return RedirectResponse("/customers", status_code=303)

    run(db, "UPDATE customer SET name = :n, remark = :r WHERE id = :i",
        n=new, r=memo, i=customer_id)
    db.commit()

    if renamed:
        return RedirectResponse(
            f"/customers?renamed={quote(cur['name'])}&to={quote(new)}", status_code=303)
    return RedirectResponse(f"/customers?saved={quote(new)}", status_code=303)


@app.post("/customers/{customer_id}/delete")
def customer_delete(customer_id: int, db: Session = Depends(get_db)):
    """클러스터가 하나도 없는 고객사만 지운다.

    FK 가 RESTRICT 라 DB 도 거절하지만, 에러 화면보다 이유를 보여주는 쪽이 낫다.
    """
    c = one(db, """
        SELECT cu.name,
               (SELECT COUNT(*) FROM cluster c WHERE c.customer_id = cu.id) AS clusters
        FROM customer cu WHERE cu.id = :i
    """, i=customer_id)
    if not c:
        raise HTTPException(404, "고객사를 찾을 수 없습니다")
    if c["clusters"]:
        return RedirectResponse(f"/customers?err=inuse&name={quote(c['name'])}",
                                status_code=303)

    run(db, "DELETE FROM customer WHERE id = :i", i=customer_id)
    db.commit()
    return RedirectResponse(f"/customers?deleted={quote(c['name'])}", status_code=303)


# ===================================================================== API


@app.get("/api/customers/suggest", response_class=HTMLResponse)
def customer_suggest(request: Request, customer_name: str = "",
                     db: Session = Depends(get_db)):
    """클러스터 등록 폼의 고객사 자동완성 후보.

    HTMX 가 입력란의 값을 그 이름(customer_name)으로 보내므로 파라미터 이름이
    폼 필드와 같다. HTML 조각을 돌려주는 이유는, 후보 목록과 '기존/신규' 표시를
    한 번의 응답으로 갈아끼우기 때문이다. JSON 으로 받으면 그 조립을 브라우저
    쪽 JS 로 다시 써야 한다.

    정렬: 정확히 같은 이름 → 앞글자가 맞는 것 → 가나다. 'LX' 를 치면
    'LX세미콘' 이 'ABC LX' 보다 먼저 와야 한다.
    """
    q = customer_name.strip()
    matches = rows(db, """
        SELECT name FROM customer
        WHERE status = 'ACTIVE' AND (:q = '' OR name LIKE :like)
        ORDER BY (name = :q) DESC, (name LIKE :pre) DESC, name
        LIMIT 8
    """, q=q, like=f"%{q}%", pre=f"{q}%")

    # DB 콜레이션(utf8mb4_unicode_ci)은 대소문자를 구분하지 않는다. 화면 표시도
    # 같은 기준이어야 'lgcns' 를 쳤을 때 '새 고객사' 로 잘못 뜨지 않는다.
    exact = bool(q) and any(m["name"].casefold() == q.casefold() for m in matches)

    return templates.TemplateResponse(request=request, name="_customer_ac.html",
        context={"matches": matches, "q": q, "exact": exact})


@app.get("/api/clusters")
def api_clusters(q: str = "", db: Session = Depends(get_db)):
    return rows(db, LIST_SQL, q=q, like=f"%{q}%")


@app.get("/api/clusters/{cluster_id}")
def api_cluster(cluster_id: int, db: Session = Depends(get_db)):
    basic = one(db, "SELECT * FROM v_cluster_basic WHERE cluster_id=:id", id=cluster_id)
    if not basic:
        raise HTTPException(404)
    basic["hardware"] = rows(db, """
        SELECT kind, model_name, quantity, capacity_note FROM cluster_hardware
        WHERE cluster_id=:id ORDER BY FIELD(kind,'C','D','E','S'), sort_order
    """, id=cluster_id)
    return basic
