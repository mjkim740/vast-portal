"""2단계: 네트워크 · 라이선스 · 펌웨어 · 용량 입력.

저장 단위를 넷으로 나눈 이유
  - 네트워크/라이선스는 '행 목록'이라 전체 교체가 맞다
  - 펌웨어는 확인일 기준으로 한 벌씩 쌓인다
  - 용량은 수정이 아니라 측정일마다 추가다
한 폼에 몰면 저장 한 번이 전부를 덮어써서 위험하고, 화면도 감당이 안 된다.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from . import audit, netparse
from .db import get_db, one, rows, run, scalar

router = APIRouter()

FW_COMPONENTS = ["OS", "SCM", "SSD", "SWITCH", "BMC", "NIC", "OTHER"]


def _cluster_or_404(db, cluster_id: int) -> dict:
    c = one(db, """
        SELECT c.*, cu.name AS customer_name
        FROM cluster c JOIN customer cu ON cu.id = c.customer_id
        WHERE c.id = :id
    """, id=cluster_id)
    if not c:
        raise HTTPException(404, "클러스터를 찾을 수 없습니다")
    return c


def _blank(v):
    v = (v or "").strip()
    return v if v else None


# ===================================================================== 네트워크


@router.get("/cluster/{cluster_id}/edit/network", response_class=HTMLResponse)
def network_form(cluster_id: int, request: Request, db: Session = Depends(get_db)):
    from .main import templates

    cluster = _cluster_or_404(db, cluster_id)
    saved = rows(db, """
        SELECT id, wiki_label, role, fabric, addr_start_txt, addr_end_txt,
               prefix_len, vlan, mtu, remark, sort_order
        FROM cluster_network WHERE cluster_id = :id
        ORDER BY sort_order, id
    """, id=cluster_id)

    # 같은 라벨의 주소들을 한 줄로 합쳐 보여준다. (dns 2개 → '1.1.1.1, 2.2.2.2')
    merged: list[dict] = []
    for r in saved:
        addr = netparse.format_address(r["addr_start_txt"], r["addr_end_txt"], r["prefix_len"])
        if merged and merged[-1]["wiki_label"] == r["wiki_label"] \
                and merged[-1]["role"] == r["role"] and merged[-1]["fabric"] == r["fabric"]:
            merged[-1]["addr"] += f", {addr}"
        else:
            merged.append({"wiki_label": r["wiki_label"], "role": r["role"],
                           "fabric": r["fabric"], "addr": addr,
                           "vlan": r["vlan"], "mtu": r["mtu"], "remark": r["remark"]})

    # 위키 표준 행은 항상 전부 띄운다. 값이 있는 행은 값을 채우고, 없는 행은 빈칸.
    #
    # 왜 '비어 있을 때만' 이 아니라 '항상' 인가
    #   저장은 전체 교체다. 입력 중간에 저장되면(Enter 등) 채운 행만 남는다.
    #   표준 행을 '행이 하나도 없을 때만' 깔면, 그 다음 수정 화면에는 남은
    #   행만 보이고 못 채운 표준 행은 사라진다. 사용자는 행 추가로 직접
    #   라벨을 다시 적게 되는데, 철자가 조금만 달라도 위키에서 같은 항목이
    #   두 줄(표준 행은 '-', 새 행은 값)로 나온다. 실제로 그렇게 깨졌다.
    #   표준 행을 늘 띄우면 다시 적을 일이 없어 애초에 생기지 않는다.
    # 화면 순서는 위키에 나갈 순서와 같게 맞춘다. 화면에서 본 순서대로
    # 위키에 나와야 미리보기를 믿고 입력할 수 있다.
    by_key = {netparse.label_key(m["wiki_label"]): m for m in merged}
    defaults = {lbl: (role, fab) for lbl, role, fab in netparse.DEFAULT_ROWS}

    netrows: list[dict] = []
    for label in netparse.order_labels([m["wiki_label"] for m in merged]):
        hit = by_key.get(netparse.label_key(label))
        std = netparse.is_standard(label)
        if hit:
            hit["wiki_label"] = label        # 철자를 표준 표기로 맞춘다
            hit["is_std"] = std
            netrows.append(hit)
        else:
            role, fab = defaults.get(label, ("OTHER", "MGMT"))
            netrows.append({"wiki_label": label, "role": role, "fabric": fab,
                            "addr": "", "vlan": None, "mtu": None, "remark": "",
                            "is_std": std})

    return templates.TemplateResponse(request=request, name="edit_network.html", context={
        "cluster": cluster, "tab": "network", "netrows": netrows,
        "std_labels": netparse.STD_LABELS,
        "roles": netparse.ROLES, "fabrics": netparse.FABRICS,
        "errors": request.query_params.get("err", ""),
    })


@router.post("/cluster/{cluster_id}/edit/network")
async def network_save(cluster_id: int, request: Request, db: Session = Depends(get_db)):
    _cluster_or_404(db, cluster_id)
    f = await request.form()
    by = (f.get("updated_by") or "").strip()

    labels = f.getlist("net_label")
    roles = f.getlist("net_role")
    fabrics = f.getlist("net_fabric")
    addrs = f.getlist("net_addr")
    vlans = f.getlist("net_vlan")
    mtus = f.getlist("net_mtu")
    remarks = f.getlist("net_remark")

    parsed: list[dict] = []
    bad: list[str] = []
    for i, label in enumerate(labels):
        # 표준 행과 철자만 다른 라벨은 표준 표기로 맞춘다.
        # 안 맞추면 위키에서 같은 항목이 두 줄로 나온다.
        label = netparse.canonical_label(label)
        raw = addrs[i] if i < len(addrs) else ""
        if not label or netparse.is_blank(raw):
            continue
        items, errs = netparse.parse_addresses(raw)
        bad += [f"{label}: {e}" for e in errs]
        for it in items:
            parsed.append({
                **it,
                "wiki_label": label,
                "role": roles[i] if i < len(roles) else "OTHER",
                "fabric": fabrics[i] if i < len(fabrics) else "MGMT",
                "vlan": _blank(vlans[i] if i < len(vlans) else ""),
                "mtu": _blank(mtus[i] if i < len(mtus) else ""),
                "remark": _blank(remarks[i] if i < len(remarks) else ""),
                "sort_order": i,
            })

    if bad:
        # 하나라도 못 읽으면 저장하지 않는다. 일부만 들어가면 더 헷갈린다.
        from urllib.parse import quote
        return RedirectResponse(
            f"/cluster/{cluster_id}/edit/network?err={quote('; '.join(bad))}",
            status_code=303)

    before = rows(db, """
        SELECT wiki_label, addr_start_txt, addr_end_txt, prefix_len FROM cluster_network
        WHERE cluster_id=:id ORDER BY sort_order, id
    """, id=cluster_id)
    run(db, "DELETE FROM cluster_network WHERE cluster_id=:id", id=cluster_id)

    for p in parsed:
        run(db, """
            INSERT INTO cluster_network
              (cluster_id, role, fabric, addr_start, addr_end, prefix_len,
               vlan, mtu, wiki_label, sort_order, remark, updated_by)
            VALUES (:c,:r,:f, INET6_ATON(:a), INET6_ATON(:e), :p,
                    :v,:m,:lbl,:so,:rm, NULLIF(:by,''))
        """, c=cluster_id, r=p["role"], f=p["fabric"], a=p["addr_start"], e=p["addr_end"],
             p=p["prefix_len"], v=p["vlan"], m=p["mtu"], lbl=p["wiki_label"],
             so=p["sort_order"], rm=p["remark"], by=by)

    def label_of(items, key="addr_start_txt"):
        return f"{len(items)}건"

    if len(before) != len(parsed):
        audit.log(db, cluster_id, "UPDATE", "cluster_network", None, "주소",
                  label_of(before), f"{len(parsed)}건", by)
    db.commit()
    return RedirectResponse(f"/cluster/{cluster_id}", status_code=303)


# ===================================================================== 라이선스


@router.get("/cluster/{cluster_id}/edit/license", response_class=HTMLResponse)
def license_form(cluster_id: int, request: Request, db: Session = Depends(get_db)):
    from .main import templates

    return templates.TemplateResponse(request=request, name="edit_license.html", context={
        "cluster": _cluster_or_404(db, cluster_id),
        "tab": "license",
        "licenses": rows(db, """
            SELECT l.*, s.license_status, s.days_left
            FROM license l LEFT JOIN v_license_status s ON s.id = l.id
            WHERE l.cluster_id = :id ORDER BY l.sort_order, l.id
        """, id=cluster_id),
    })


@router.post("/cluster/{cluster_id}/edit/license")
async def license_save(cluster_id: int, request: Request, db: Session = Depends(get_db)):
    _cluster_or_404(db, cluster_id)
    f = await request.form()
    by = (f.get("updated_by") or "").strip()

    types = f.getlist("lic_type")
    months = f.getlist("lic_months")
    starts = f.getlist("lic_start")
    ends = f.getlist("lic_end")
    caps = f.getlist("lic_capacity")
    nos = f.getlist("lic_contract")
    purposes = f.getlist("lic_purpose")

    before = rows(db, """
        SELECT license_type, start_date, end_date FROM license
        WHERE cluster_id=:id ORDER BY sort_order, id
    """, id=cluster_id)
    run(db, "DELETE FROM license WHERE cluster_id=:id", id=cluster_id)

    n = 0
    for i, t in enumerate(types):
        t = t.strip()
        if not t:
            continue
        run(db, """
            INSERT INTO license
              (cluster_id, license_type, term_months, start_date, end_date,
               capacity_tb, contract_no, purpose, sort_order, updated_by)
            VALUES (:c,:t,:tm,:sd,:ed,:cap,:no,:pp,:so, NULLIF(:by,''))
        """, c=cluster_id, t=t,
             tm=_blank(months[i] if i < len(months) else ""),
             sd=_blank(starts[i] if i < len(starts) else ""),
             ed=_blank(ends[i] if i < len(ends) else ""),
             cap=_blank(caps[i] if i < len(caps) else ""),
             no=_blank(nos[i] if i < len(nos) else ""),
             pp=_blank(purposes[i] if i < len(purposes) else ""),
             so=i, by=by)
        n += 1

    if len(before) != n:
        audit.log(db, cluster_id, "UPDATE", "license", None, "건수",
                  f"{len(before)}건", f"{n}건", by)
    db.commit()
    return RedirectResponse(f"/cluster/{cluster_id}", status_code=303)


# ===================================================================== 펌웨어


@router.get("/cluster/{cluster_id}/edit/firmware", response_class=HTMLResponse)
def firmware_form(cluster_id: int, request: Request, db: Session = Depends(get_db)):
    from .main import templates

    current = {r["component"]: r for r in rows(db, """
        SELECT component, model, version, checked_at FROM v_firmware_current
        WHERE cluster_id=:id
    """, id=cluster_id)}

    return templates.TemplateResponse(request=request, name="edit_firmware.html", context={
        "cluster": _cluster_or_404(db, cluster_id),
        "tab": "firmware",
        "components": FW_COMPONENTS,
        "current": current,
        "history": rows(db, """
            SELECT component, model, version, checked_at FROM cluster_firmware
            WHERE cluster_id=:id ORDER BY checked_at DESC,
                  FIELD(component,'OS','SCM','SSD','SWITCH','BMC','NIC','OTHER')
            LIMIT 40
        """, id=cluster_id),
    })


@router.post("/cluster/{cluster_id}/edit/firmware")
async def firmware_save(cluster_id: int, request: Request, db: Session = Depends(get_db)):
    _cluster_or_404(db, cluster_id)
    f = await request.form()
    by = (f.get("updated_by") or "").strip()
    checked_at = _blank(f.get("checked_at"))
    if not checked_at:
        raise HTTPException(400, "확인일은 필수입니다")

    n = 0
    for comp in FW_COMPONENTS:
        v = _blank(f.get(f"fw_{comp}"))
        m = _blank(f.get(f"fwm_{comp}"))
        # 버전이 없으면 그 줄은 없는 것으로 본다. model 만 적어도 저장하지 않는다 —
        # cluster_firmware.version 이 NOT NULL 이고, 모델만 있는 행은 의미가 없다.
        if not v:
            continue
        # 같은 날 같은 컴포넌트는 덮어쓰고, 날짜가 다르면 이력으로 쌓인다.
        # model 은 uk_fw 에 없으므로, 모델명을 고쳐도 행이 새로 생기지 않는다.
        run(db, """
            INSERT INTO cluster_firmware
              (cluster_id, component, model, version, checked_at, updated_by)
            VALUES (:c,:comp,:m,:v,:at, NULLIF(:by,''))
            ON DUPLICATE KEY UPDATE model=VALUES(model), version=VALUES(version),
                                    updated_by=VALUES(updated_by)
        """, c=cluster_id, comp=comp, m=m, v=v, at=checked_at, by=by)
        n += 1

    if n:
        audit.log(db, cluster_id, "UPDATE", "cluster_firmware", None,
                  f"펌웨어 {checked_at}", None, f"{n}건", by)
    db.commit()
    return RedirectResponse(f"/cluster/{cluster_id}", status_code=303)


# ===================================================================== 용량


@router.get("/cluster/{cluster_id}/edit/capacity", response_class=HTMLResponse)
def capacity_form(cluster_id: int, request: Request, db: Session = Depends(get_db)):
    from .main import templates

    cluster = _cluster_or_404(db, cluster_id)
    return templates.TemplateResponse(request=request, name="edit_capacity.html", context={
        "cluster": cluster,
        "tab": "capacity",
        "snapshots": rows(db, """
            SELECT * FROM capacity_snapshot WHERE cluster_id=:id
            ORDER BY measured_at DESC LIMIT 36
        """, id=cluster_id),
        "last_release": cluster.get("cur_release") or "",
    })


@router.post("/cluster/{cluster_id}/edit/capacity")
async def capacity_save(cluster_id: int, request: Request, db: Session = Depends(get_db)):
    _cluster_or_404(db, cluster_id)
    f = await request.form()
    by = (f.get("updated_by") or "").strip()
    measured_at = _blank(f.get("measured_at"))
    if not measured_at:
        raise HTTPException(400, "측정일은 필수입니다")

    run(db, """
        INSERT INTO capacity_snapshot
          (cluster_id, measured_at, release_at_time,
           usable_total_tb, usable_used_tb, usable_free_tb,
           logical_total_tb, logical_used_tb, logical_free_tb,
           drr, source, remark, updated_by)
        VALUES (:c,:at,:rel,:ut,:uu,:uf,:lt,:lu,:lf,:drr,'MANUAL',:rm, NULLIF(:by,''))
        ON DUPLICATE KEY UPDATE
          release_at_time=VALUES(release_at_time),
          usable_total_tb=VALUES(usable_total_tb),
          usable_used_tb=VALUES(usable_used_tb),
          usable_free_tb=VALUES(usable_free_tb),
          logical_total_tb=VALUES(logical_total_tb),
          logical_used_tb=VALUES(logical_used_tb),
          logical_free_tb=VALUES(logical_free_tb),
          drr=VALUES(drr), remark=VALUES(remark), updated_by=VALUES(updated_by)
    """, c=cluster_id, at=measured_at, rel=_blank(f.get("release_at_time")),
         ut=_blank(f.get("usable_total_tb")), uu=_blank(f.get("usable_used_tb")),
         uf=_blank(f.get("usable_free_tb")),
         lt=_blank(f.get("logical_total_tb")), lu=_blank(f.get("logical_used_tb")),
         lf=_blank(f.get("logical_free_tb")),
         drr=_blank(f.get("drr")), rm=_blank(f.get("remark")), by=by)

    audit.log(db, cluster_id, "UPDATE", "capacity_snapshot", None,
              f"용량 {measured_at}", None, _blank(f.get("usable_total_tb")), by)
    db.commit()
    return RedirectResponse(f"/cluster/{cluster_id}", status_code=303)


@router.post("/cluster/{cluster_id}/capacity/{snapshot_id}/delete")
def capacity_delete(cluster_id: int, snapshot_id: int, db: Session = Depends(get_db)):
    run(db, "DELETE FROM capacity_snapshot WHERE id=:s AND cluster_id=:c",
        s=snapshot_id, c=cluster_id)
    db.commit()
    return RedirectResponse(f"/cluster/{cluster_id}/edit/capacity", status_code=303)


# ===================================================================== 장비 개체
#
# 시리얼과 파트넘버를 장비 한 대씩 기록한다.
#
# 화면 구성
#   장비 목록(cluster_hardware)의 수량만큼 칸을 만들어 둔다. 'SMC 3대' 면 1~3호기
#   세 줄이다. 아는 것만 채우면 되고, 빈 줄은 저장하지 않는다.
#
# 저장
#   장비 행 단위로 전체 교체한다. 수량을 줄이면 넘치는 번호는 사라진다.
#   (수량 3 → 2 로 줄이면 3호기 시리얼은 지워진다. 화면에서 보이므로 알 수 있다.)


def _hw_rows(db, cluster_id: int) -> list[dict]:
    return rows(db, """
        SELECT kind, model_name, quantity
        FROM cluster_hardware WHERE cluster_id = :id
        ORDER BY FIELD(kind,'C','D','E','S'), sort_order, id
    """, id=cluster_id)


@router.get("/cluster/{cluster_id}/edit/units", response_class=HTMLResponse)
def units_form(cluster_id: int, request: Request, db: Session = Depends(get_db)):
    from .main import templates, KIND_LABEL

    cluster = _cluster_or_404(db, cluster_id)
    saved = rows(db, """
        SELECT kind, model_name, seq, serial_no, part_no, remark
        FROM hardware_unit WHERE cluster_id = :id
    """, id=cluster_id)
    by_key = {(u["kind"], u["model_name"], u["seq"]): u for u in saved}

    # 장비 목록의 수량만큼 칸을 깔고, 저장된 값이 있으면 채운다
    groups: list[dict] = []
    used: set[tuple] = set()
    for h in _hw_rows(db, cluster_id):
        units = []
        for seq in range(1, int(h["quantity"]) + 1):
            key = (h["kind"], h["model_name"], seq)
            used.add(key)
            u = by_key.get(key) or {}
            units.append({"seq": seq,
                          "serial_no": u.get("serial_no") or "",
                          "part_no":   u.get("part_no") or "",
                          "remark":    u.get("remark") or ""})
        groups.append({"kind": h["kind"], "kind_label": KIND_LABEL[h["kind"]],
                       "model_name": h["model_name"], "quantity": h["quantity"],
                       "filled": sum(1 for x in units if x["serial_no"]),
                       "units": units})

    # 장비 목록에 더 이상 없는 시리얼. 모델명을 고쳤거나 행을 지운 경우다.
    # 조용히 감추면 "어디 갔지" 가 되므로 따로 보여주고 지울 수 있게 한다.
    orphans = [u for u in saved
               if (u["kind"], u["model_name"], u["seq"]) not in used
               and (u["serial_no"] or u["part_no"])]

    return templates.TemplateResponse(request=request, name="edit_units.html", context={
        "cluster": cluster, "tab": "units", "groups": groups, "orphans": orphans,
        "kind_label": KIND_LABEL,
        "errors": request.query_params.get("err", ""),
    })


@router.post("/cluster/{cluster_id}/edit/units")
async def units_save(cluster_id: int, request: Request, db: Session = Depends(get_db)):
    _cluster_or_404(db, cluster_id)
    f = await request.form()
    by = (f.get("updated_by") or "").strip()

    keys     = f.getlist("u_key")        # "kind|model_name|seq"
    serials  = f.getlist("u_serial")
    parts    = f.getlist("u_part")
    remarks  = f.getlist("u_remark")

    parsed: list[dict] = []
    seen: dict[str, str] = {}
    for i, raw in enumerate(keys):
        try:
            kind, model_name, seq = raw.rsplit("|", 2)
        except ValueError:
            continue
        serial = (serials[i] if i < len(serials) else "").strip()
        part   = (parts[i]   if i < len(parts)   else "").strip()
        remark = (remarks[i] if i < len(remarks) else "").strip()
        if not (serial or part or remark):
            continue                      # 빈 줄은 저장하지 않는다

        # 같은 폼 안에서 시리얼이 겹치는 건 대개 복사 실수다. 저장 전에 잡는다.
        if serial:
            if serial in seen:
                return _units_err(cluster_id,
                                  f"시리얼 '{serial}' 이 {seen[serial]} 와 "
                                  f"{model_name} {seq}호기에 중복 입력되었습니다")
            seen[serial] = f"{model_name} {seq}호기"

        parsed.append({"kind": kind, "model_name": model_name, "seq": int(seq),
                       "serial": serial or None, "part": part or None,
                       "remark": remark or None})

    # 다른 클러스터가 이미 쓰는 시리얼인지 확인한다. DB 의 UNIQUE 로도 막히지만,
    # 그 오류 메시지로는 어느 고객사와 겹치는지 알 수 없다.
    for p in parsed:
        if not p["serial"]:
            continue
        dup = one(db, """
            SELECT cu.name AS customer_name, c.name AS cluster_name, u.model_name, u.seq
            FROM hardware_unit u
            JOIN cluster  c  ON c.id  = u.cluster_id
            JOIN customer cu ON cu.id = c.customer_id
            WHERE u.serial_no = :s AND u.cluster_id <> :c
        """, s=p["serial"], c=cluster_id)
        if dup:
            return _units_err(cluster_id,
                              f"시리얼 '{p['serial']}' 은 이미 "
                              f"{dup['customer_name']} / {dup['cluster_name']} "
                              f"({dup['model_name']} {dup['seq']}호기) 에 등록되어 있습니다")

    before = scalar(db, """
        SELECT COUNT(*) FROM hardware_unit
        WHERE cluster_id = :c AND serial_no IS NOT NULL AND serial_no <> ''
    """, c=cluster_id) or 0

    run(db, "DELETE FROM hardware_unit WHERE cluster_id = :c", c=cluster_id)
    for p in parsed:
        run(db, """
            INSERT INTO hardware_unit
              (cluster_id, kind, model_name, seq, serial_no, part_no, remark, updated_by)
            VALUES (:c,:k,:m,:q,:s,:p,:r, NULLIF(:by,''))
        """, c=cluster_id, k=p["kind"], m=p["model_name"], q=p["seq"],
             s=p["serial"], p=p["part"], r=p["remark"], by=by)

    after = sum(1 for p in parsed if p["serial"])
    if before != after:
        audit.log(db, cluster_id, "UPDATE", "hardware_unit", None,
                  "시리얼", f"{before}대", f"{after}대", by)
    db.commit()
    return RedirectResponse(f"/cluster/{cluster_id}", status_code=303)


def _units_err(cluster_id: int, msg: str) -> RedirectResponse:
    """하나라도 틀리면 저장하지 않는다. 일부만 들어가면 더 헷갈린다."""
    from urllib.parse import quote
    return RedirectResponse(f"/cluster/{cluster_id}/edit/units?err={quote(msg)}",
                            status_code=303)
