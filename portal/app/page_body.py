"""DB 값 → Confluence 페이지 본문(storage XHTML) 생성.

템플릿은 'LGCNS - SLM (To-Be)' 페이지(id 308019202)를 그대로 재현한다.

  [2단 레이아웃]
    왼쪽  : [기본 정보] 표  +  [펌웨어] 표
    오른쪽: [구성 정보] 표 (기본 항목 + 네트워크 행 + 추가 정보)
    아래  : 🟧 문서 (수기 영역 — 발행이 덮어쓰지 않도록 안내만 남긴다)

왜 storage 형식인가
  Confluence Cloud REST v2 는 storage(XHTML)를 받아 에디터 형식으로 변환해 준다.
  ADF(JSON)로 직접 만들면 변환이 없어 더 정확하지만, 표 한 칸이 dict 5겹이라
  사람이 읽고 고칠 수 없는 코드가 된다. 이 파일은 위키 담당자가 직접 손볼
  가능성이 높아 가독성을 택했다.

2단 레이아웃이 깨질 경우
  TWO_COLUMN = False 로 두면 표를 위에서 아래로 나열한다(변환 위험 없음).
"""

from __future__ import annotations

import re
from html import escape

from . import netparse

TWO_COLUMN = True

# 표 폭. Confluence 의 breakout 설정이다.
#   default    본문 폭(760px 정도)
#   wide       본문보다 조금 넓게
#   full-width 창 폭까지
# 2단 레이아웃 안에서는 한 칸이 절반이라 default 면 표가 좁아 라벨이 줄바꿈된다.
LAYOUT_BREAKOUT = "full-width"
TABLE_LAYOUT    = "full-width"

# 열 폭(px). 왼쪽(항목명)이 줄바꿈되지 않을 만큼은 줘야 한다.
COLW_BASIC = (340, 420)   # [기본 정보] — '용량 - Usable (Total/Used/System/Free)' 가 길다
COLW_SPEC  = (200, 560)   # [구성 정보]
COLW_FW    = (200, 560)        # [펌웨어] — Model 열이 없을 때
COLW_FW_M  = (160, 220, 380)   # [펌웨어] — Model 열이 있을 때. 합은 같게 둔다

# 위키 표의 행 순서. 입력 폼의 표준 행과 같은 목록을 쓴다.
# 두 곳에 따로 적어 두면 한쪽만 고쳐져 행이 어긋난다.
NET_ORDER = netparse.STD_LABELS

# 표준 행은 저장된 철자가 아니라 위키 표기로 출력한다
_STD_NAME = {netparse.label_key(lb): lb for lb in NET_ORDER}

# 펌웨어 표의 행 순서. 목록에 없는 component 는 뒤에 붙인다.
FW_ORDER = ["OS", "SCM", "SSD", "SWITCH", "BMC", "NIC"]

# DB 는 대문자로 저장하고, 위키는 템플릿 표기를 쓴다.
FW_LABEL = {"SWITCH": "Switch", "OTHER": "기타"}

FABRIC_LABEL = {
    "SERVICE_ROCE": "RoCE",
    "SERVICE_IB":   "IB",
    "SERVICE_ETH":  "Ethernet",
}

KIND_LABEL = {"C": "CBox", "D": "DBox", "E": "EBox", "S": "스위치"}

NIC_LABEL  = {"EXTERNAL": "External", "INTERNAL": "Internal", "MIXED": "Mixed"}
TYPE_LABEL = {"OUTBAND": "outband", "INBAND": "inband", "B2B": "b2b"}

NA = "-"


# --------------------------------------------------------------- 작은 조립 함수

def _t(v) -> str:
    """빈 값은 위키 표기 그대로 '-'. 0 과 빈 문자열을 구분한다."""
    if v is None:
        return NA
    s = str(v).strip()
    return escape(s) if s else NA


def trim_number(v) -> str:
    """숫자에서 의미 없는 0 을 떼낸다. 1810.000 → 1810, 1.900 → 1.9

    DB 가 DECIMAL 이라 자릿수를 맞춰 1810.000 처럼 나온다. 읽기만 불편하다.
    실제로 입력한 소수(89.178)는 그대로 둔다 — 반올림하면 사용자가 적어 넣은
    값이 사라진다.

    빈 값은 빈 문자열로 돌려준다. 화면 템플릿이 '—' 를, 위키가 '-' 를 쓰므로
    없는 값 표기는 부르는 쪽이 정한다. 포털 화면에서는 Jinja 필터 num 으로 쓴다.
    """
    if v is None:
        return ""
    s = str(v).strip()
    if re.fullmatch(r"-?\d+\.\d+", s):
        s = s.rstrip("0").rstrip(".")
    return s


def _n(v) -> str:
    """위키용. trim_number 와 같은 규칙이고, 빈 값은 '-' 로 낸다."""
    s = trim_number(v)
    return escape(s) if s else NA


def _p(v) -> str:
    return f"<p>{_t(v)}</p>"


def _lines(values: list) -> str:
    """한 칸에 여러 줄. 라이선스 2개, Service IP 2개 같은 경우."""
    vals = [v for v in values if v not in (None, "")]
    if not vals:
        return f"<p>{NA}</p>"
    return "".join(f"<p>{_t(v)}</p>" for v in vals)


_BARE_IP = re.compile(r"^(\d{1,3}\.\d{1,3}\.\d{1,3})\.(\d{1,3})(/\d{1,2})?$")


def compact_addresses(s: str) -> str:
    """'192.168.10.151, 192.168.10.154, ...' → '192.168.10.{151,154,...}'

    띄엄띄엄한 주소 여덟 개를 그대로 쓰면 한 칸이 100 자를 넘어 표가 읽히지
    않는다. 입력할 때 쓰는 중괄호 표기로 되묶어 내보낸다.
    연속한 번호는 범위로 줄인다(160,161,162 → 160-162).

    조각 중 하나라도 '순수한 IP(/프리픽스)' 가 아니면(vlan·MTU 가 붙은 경우 등)
    손대지 않고 원문을 그대로 돌려준다. 잘못 묶는 것보다 긴 편이 낫다.
    """
    parts = [p.strip() for p in (s or "").split(",") if p.strip()]
    if len(parts) < 3:
        return s

    prefix = suffix = None
    nums: list[int] = []
    for p in parts:
        m = _BARE_IP.match(p)
        if not m:
            return s
        if prefix is None:
            prefix, suffix = m.group(1), m.group(3) or ""
        elif m.group(1) != prefix or (m.group(3) or "") != suffix:
            return s
        nums.append(int(m.group(2)))

    nums = sorted(set(nums))
    groups: list[str] = []
    i = 0
    while i < len(nums):
        j = i
        while j + 1 < len(nums) and nums[j + 1] == nums[j] + 1:
            j += 1
        if j - i >= 2:                       # 3개 이상 연속이면 범위로
            groups.append(f"{nums[i]}-{nums[j]}")
        else:
            groups += [str(n) for n in nums[i:j + 1]]
        i = j + 1

    return f"{prefix}.{{{','.join(groups)}}}{suffix}"


def _bullets(values: list) -> str:
    """템플릿의 Service IP 행은 불릿이다. 그 모양을 유지한다."""
    vals = [v for v in values if v not in (None, "")]
    if not vals:
        return f"<p>{NA}</p>"
    items = "".join(f"<li><p>{_t(v)}</p></li>" for v in vals)
    return f"<ul>{items}</ul>"


def _kv_table(pairs: list[tuple[str, str]], colwidth: tuple[int, int] | None = None) -> str:
    """머리글 없는 2열 표. pairs 의 값은 이미 조립된 XHTML 조각이다.

    열 폭은 colgroup 으로 준다. storage 에서 이 방식이 에디터의 data-colwidth 로
    변환된다 — 템플릿의 200/448 을 재현하려면 이게 필요하다.
    """
    head = ""
    if colwidth:
        head = (f'<colgroup><col style="width: {colwidth[0]}px;" />'
                f'<col style="width: {colwidth[1]}px;" /></colgroup>')
    body = "".join(
        f"<tr><td><p>{escape(k)}</p></td><td>{v}</td></tr>" for k, v in pairs
    )
    return (f'<table data-layout="{TABLE_LAYOUT}">'
            f'{head}<tbody>{body}</tbody></table>')


def _fw_table(fw: list[dict]) -> str:
    """[펌웨어] 표. Model 열은 값이 있을 때만 만든다.

    왜 조건부인가
      모델은 선택 입력이다. 열을 항상 넣으면 모델을 적지 않은 기존 페이지마다
      빈 칸이 한 줄씩 늘어난다. 읽는 사람에게 아무것도 알려주지 않는 열이다.
      한 건이라도 적힌 클러스터만 3열로 발행하고, 나머지는 지금 모양을 지킨다.
    """
    rank = {c: i for i, c in enumerate(FW_ORDER)}
    ordered = sorted(fw, key=lambda f: (rank.get(f["component"], 99), f["component"]))

    has_model = any((f.get("model") or "").strip() for f in ordered)
    widths = COLW_FW_M if has_model else COLW_FW

    heads = ["List"] + (["Model"] if has_model else []) + ["FW Version"]

    if not ordered:
        rows = "<tr>" + "".join(f"<td><p>{NA}</p></td>" for _ in heads) + "</tr>"
    else:
        out = []
        for f in ordered:
            cells = [_p(FW_LABEL.get(f["component"], f["component"]))]
            if has_model:
                cells.append(_p((f.get("model") or "").strip() or NA))
            cells.append(_p(f["version"]))
            out.append("<tr>" + "".join(f"<td>{c}</td>" for c in cells) + "</tr>")
        rows = "".join(out)

    return (
        f'<table data-layout="{TABLE_LAYOUT}">'
        "<colgroup>"
        + "".join(f'<col style="width: {w}px;" />' for w in widths)
        + "</colgroup><thead><tr>"
        + "".join(f"<th><p><strong>{h}</strong></p></th>" for h in heads)
        + f"</tr></thead><tbody>{rows}</tbody></table>"
    )


# --------------------------------------------------------------- 각 표

def _nums(vals: list) -> str:
    """'248, 89.178, 2.535, 156.287' 형태.

    자리 수를 유지한다 — 가운데 값이 없으면 '-' 를 넣어 Total/Used/System/Free
    순서가 어긋나지 않게 한다. 전부 없으면 행 전체를 '-' 로 낸다.
    (칸 수를 리스트 길이로 받는 이유: Usable 은 4칸, Logical 은 3칸이다.)
    """
    if all(v is None for v in vals):
        return NA
    return ", ".join(_n(v) for v in vals)


def basic_table(basic: dict, licenses: list[dict]) -> str:
    lic_lines = [
        ", ".join([
            l.get("license_type") or NA,
            f"{l['term_months'] // 12}년" if l.get("term_months") and l["term_months"] % 12 == 0
            else (f"{l['term_months']}개월" if l.get("term_months") else NA),
            str(l.get("start_date") or NA),
            str(l.get("end_date") or NA),
        ])
        for l in licenses
    ]
    return _kv_table([
        ("Last Update",                                   _p(basic.get("last_update"))),
        ("Cluster size",                                  _p(basic.get("cluster_size"))),
        ("VAST SW Release",                               _p(basic.get("vast_sw_release"))),
        ("용량 - Usable (Total/Used/System/Free) (단위TB)",
            _p(_nums([basic.get("usable_total_tb"), basic.get("usable_used_tb"),
                      basic.get("usable_system_tb"), basic.get("usable_free_tb")]))),
        ("용량 - Logical (Total/Used/Free) (단위TB)",
            _p(_nums([basic.get("logical_total_tb"), basic.get("logical_used_tb"),
                      basic.get("logical_free_tb")]))),
        ("DRR",                                           f"<p>{_n(basic.get('drr'))}</p>"),
        ("보장 DRR",                                       f"<p>{_n(basic.get('guaranteed_drr'))}</p>"),
        ("라이선스 (유형/계약기간/시작일/종료일)",             _lines(lic_lines)),
    ], colwidth=COLW_BASIC)


def spec_table(basic: dict, spec: dict, protocols: list[str],
               service_types: list[str], network: list[dict]) -> str:
    pairs = [
        ("Cluster Name",               _p(basic.get("cluster_name"))),
        ("Cluster detail (Model/qty)", _p(basic.get("cluster_detail"))),
        ("PSNT",                       _p(basic.get("psnt"))),
        ("SW Release",                 _p(basic.get("vast_sw_release"))),
        ("Protocol",                   _p(", ".join(protocols) if protocols else None)),
        ("Service type",               _p(", ".join(service_types) if service_types else None)),
        ("Service NiC",                _p(NIC_LABEL.get(spec.get("service_nic") or ""))),
        ("MGMT Type",                  _p(TYPE_LABEL.get(spec.get("mgmt_type") or ""))),
        ("IPMI Type",                  _p(TYPE_LABEL.get(spec.get("ipmi_type") or ""))),
    ]
    pairs += _network_pairs(network)
    pairs.append(("추가 정보", _p(spec.get("remark"))))
    return _kv_table(pairs, colwidth=COLW_SPEC)


def _network_pairs(network: list[dict]) -> list[tuple[str, str]]:
    """네트워크 행. 표에 없던 라벨은 표준 행 뒤에 순서대로 붙인다.

    Service 계열은 망 이름을 앞에 붙이고 불릿으로 낸다(템플릿 모양).
    """
    # 철자 차이(대소문자, 하이픈/공백)는 무시하고 묶는다. 'vast dns' 로 저장된
    # 값이 표준 행 'vast-dns' 에 들어가야 한다 — 안 그러면 표준 행은 '-' 로
    # 비고 같은 항목이 아래에 한 줄 더 생겨 같은 항목이 두 번 보인다.
    by_key: dict[str, list[dict]] = {}
    name_of: dict[str, str] = {}
    for n in network:
        k = netparse.label_key(n["wiki_label"])
        by_key.setdefault(k, []).append(n)
        name_of.setdefault(k, n["wiki_label"])

    # 표준 행은 고정 순서, 같은 계열의 추가 행은 그 뒤에 번호순으로 붙인다.
    # (Service IP 3 은 Service IP 2 다음이지 표 맨 아래가 아니다.)
    out: list[tuple[str, str]] = []
    for label in netparse.order_labels(list(name_of.values())):
        key = netparse.label_key(label)
        label = _STD_NAME.get(key) or name_of.get(key, label)
        group = by_key.get(key, [])
        vals = []
        for n in group:
            prefix = FABRIC_LABEL.get(n.get("fabric") or "")
            addr = compact_addresses(n.get("addresses") or "")
            vals.append(f"{prefix}, {addr}" if prefix and addr else addr)
        is_service = any((n.get("fabric") or "").startswith("SERVICE") for n in group)
        out.append((label, _bullets(vals) if is_service else _lines(vals)))
    return out


# --------------------------------------------------------------- 본문 전체

FOOTER = (
    "<p />"
    '<ac:structured-macro ac:name="info" ac:schema-version="1">'
    "<ac:rich-text-body><p>이 페이지는 "
    "<strong>VAST 고객사 사용현황 포털</strong>에서 자동 생성됩니다. "
    "표 안의 값을 위키에서 직접 고치면 다음 발행 때 덮어써집니다. "
    "값은 포털에서 수정하세요.</p></ac:rich-text-body>"
    "</ac:structured-macro>"
)


def unit_table(units: list[dict]) -> str:
    """[장비 시리얼] 표. 등록된 장비가 하나도 없으면 아무것도 내지 않는다.

    2단 레이아웃 안이 아니라 그 아래에 둔다. 장비가 열몇 대면 행이 그만큼 생겨서
    한쪽 칸에 넣으면 좌우 높이가 심하게 어긋난다.
    """
    if not units:
        return ""

    rank = {k: i for i, k in enumerate(["C", "D", "E", "S"])}
    ordered = sorted(units, key=lambda u: (rank.get(u.get("kind"), 9),
                                           u.get("model_name") or "",
                                           u.get("seq") or 0))
    body = "".join(
        "<tr>"
        f"<td>{_p(KIND_LABEL.get(u.get('kind'), u.get('kind')))}</td>"
        f"<td>{_p(u.get('model_name'))}</td>"
        f"<td>{_p(u.get('seq'))}</td>"
        f"<td>{_p(u.get('serial_no'))}</td>"
        f"<td>{_p(u.get('part_no'))}</td>"
        f"<td>{_p(u.get('remark'))}</td>"
        "</tr>"
        for u in ordered
    )
    return (
        "<p />"
        "<p>[장비 시리얼]</p>"
        f'<table data-layout="{TABLE_LAYOUT}">'
        '<colgroup><col style="width: 90px;" /><col style="width: 170px;" />'
        '<col style="width: 60px;" /><col style="width: 220px;" />'
        '<col style="width: 200px;" /><col style="width: 220px;" /></colgroup>'
        "<thead><tr>"
        "<th><p><strong>구분</strong></p></th>"
        "<th><p><strong>모델</strong></p></th>"
        "<th><p><strong>#</strong></p></th>"
        "<th><p><strong>Serial</strong></p></th>"
        "<th><p><strong>P/N</strong></p></th>"
        "<th><p><strong>비고</strong></p></th>"
        f"</tr></thead><tbody>{body}</tbody></table>"
    )


def build(basic: dict, spec: dict, licenses: list[dict], firmware: list[dict],
          protocols: list[str], service_types: list[str],
          network: list[dict], units: list[dict] | None = None) -> str:
    left = (
        "<p>[기본 정보]</p>"
        + basic_table(basic, licenses)
        + "<p />"
        + "<p>[펌웨어]</p>"
        + _fw_table(firmware)
    )
    right = (
        "<p>[구성 정보]</p>"
        + spec_table(basic, spec, protocols, service_types, network)
    )

    if TWO_COLUMN:
        # breakout-mode 를 Confluence 가 거부하면 LAYOUT_BREAKOUT = "" 로 두면 된다.
        # 그래도 표 자체의 data-layout="full-width" 는 그대로 적용된다.
        bo = f' ac:breakout-mode="{LAYOUT_BREAKOUT}"' if LAYOUT_BREAKOUT else ""
        body = (
            f'<ac:layout><ac:layout-section ac:type="two_equal"{bo}>'
            f"<ac:layout-cell>{left}</ac:layout-cell>"
            f"<ac:layout-cell>{right}</ac:layout-cell>"
            "</ac:layout-section></ac:layout>"
        )
    else:
        body = left + "<p />" + right

    return body + unit_table(units or []) + FOOTER


def page_title(d: dict) -> str:
    """'LGCNS - SLM' 형태. wiki_title 이 있으면 그 값을 그대로 쓴다.

    제목이 곧 Confluence 의 페이지 식별자다(한 스페이스에서 유일해야 한다).
    같은 고객사에 용도가 같은 클러스터가 둘이면 자동 생성 제목이 겹치므로,
    그럴 때는 wiki_title 에 직접 적어 구분한다.

    d 에는 customer_name / purpose / cluster_name / wiki_title 이 있으면 된다.
    """
    manual = (d.get("wiki_title") or "").strip()
    if manual:
        return manual

    cust = (d.get("customer_name") or "").strip()
    tail = (d.get("purpose") or "").strip() or (d.get("cluster_name") or "").strip()
    return f"{cust} - {tail}".strip(" -") if tail else cust


# ===================================================================== 전체 현황
#
# 위키의 '[VAST Data] 고객사 사용현황' 페이지를 포털이 통째로 만든다.
#
# 열은 포털 목록 화면(_table.html)과 같게 맞춘다.
#   고객사 · 클러스터 · PSNT · 구성 · 장비 · Release · Usable · 사용률 · DRR · Last update
#
# 처음에는 위키에 원래 있던 표의 열(Logical/License/만료일/IP)을 따랐다. 그러면
# 같은 데이터를 보는 화면이 둘인데 열이 달라서, 둘을 비교할 때마다 어느 쪽이
# 맞는지 따져야 했다. 한 쪽에만 있는 열은 '빠진 것' 처럼 보인다.
#   → 포털이 원본이므로 위키를 포털에 맞춘다. 비교할 필요가 없어진다.
#
# 색을 쓸 수 없어 글자로 대신하는 것 두 개
#   DRR 보장치 미달      포털은 빨간 글씨 → 위키는 '(미달)'
#   Last update 180일 초과  포털은 빨간 글씨 + 말풍선 → 위키는 '(지연)'
#
# 표에서 빠진 메모는 표 아래 목록으로 낸다
#   cluster.remark   클러스터 비고
#   customer.remark  고객사 메모 — 포털에 클러스터가 없는 고객사도 여기로 나온다
# 이 페이지는 통째로 덮어쓰기라, 위키에 손으로 적은 내용은 발행 때 사라진다.
# 남아야 하는 메모가 들어올 자리가 이 두 곳이다.

# 열 이름과 폭(px). 글자가 줄바꿈되지 않을 만큼 넉넉하게 준다.
# 가장 긴 칸은 '장비' 다 — '6 x SMC-G5, 4 x HPE-G5, 3 x Ceres-v2, 2 x sn3700' 처럼 길어진다.
OVERVIEW_COLS = [
    ("고객사",      220), ("클러스터",  260), ("PSNT",   180), ("구성",      110),
    ("장비",        520), ("Release",   200), ("Usable", 130), ("사용률",    110),
    ("DRR",         140), ("Last update", 180),
]
OVERVIEW_WIDTH = sum(w for _, w in OVERVIEW_COLS)

# 표 전체 폭(px). colgroup 합계와 같게 둔다.
#
# 왜 명시하는가
#   data-layout="full-width" 만 두면 Confluence 가 창 폭에 맞춰 열을 눌러 넣는다.
#   열이 10 개라 장비·Release 같은 칸이 줄바꿈되어 읽기 어렵다. 창보다 넓게
#   잡으면 표가 가로로 스크롤된다 — 좁혀 보는 대신 밀어 보는 쪽을 택했다.
#
#   None 으로 두면 이 속성을 넣지 않고 창 폭에 맞춘다(예전 동작).
#   열 폭만 바꾸려면 OVERVIEW_COLS 를 고치면 합계가 따라온다.
OVERVIEW_TABLE_WIDTH: int | None = OVERVIEW_WIDTH


def _customer_cell(c: dict) -> str:
    """고객사 이름. 그 클러스터 페이지가 발행돼 있으면 링크를 건다.

    포털 목록은 행 전체가 클릭되지만 위키 표에는 그런 게 없다. 그래서 이름에
    링크를 건다 — 현황을 보다가 상세로 넘어가는 길이 필요하다.

    ac:link 대신 보통 링크를 쓴다. pageId 로 바로 가는 주소라 페이지 제목이
    바뀌어도 깨지지 않고, 상대 주소라 사이트 호스트를 코드에 박지 않는다.
    """
    name = _t(c.get("customer_name"))
    pid = str(c.get("confluence_page_id") or "").strip()
    if not pid.isdigit():
        return f"<p>{name}</p>"
    return f'<p><a href="/wiki/pages/viewpage.action?pageId={pid}">{name}</a></p>'


def _overview_row(c: dict) -> str:
    # 클러스터: PROD 가 아니면 상태를 붙인다. 포털 목록도 태그로 표시한다.
    cname = _t(c.get("cluster_name"))
    if (c.get("status") or "PROD") != "PROD":
        cname += f" ({escape(str(c['status']))})"

    # Last update: 날짜만으로는 오래된 값인지 모른다. 180 일이 넘으면 표시한다.
    upd = c.get("last_update")
    if upd:
        txt = upd.strftime("%y-%m-%d")
        days = c.get("days_since_update")
        if days is not None and days > 180:
            txt += " (지연)"
        update = escape(txt)
    else:
        update = NA

    drr = _n(c["drr"]) if c.get("drr") is not None else NA
    if c.get("drr_status") == "BELOW":
        drr += " (미달)"

    pct = c.get("used_pct")

    cells = [
        _customer_cell(c),
        f"<p>{cname}</p>",
        f"<p>{_t(c.get('psnt'))}</p>",
        f"<p>{_t(c.get('cluster_size'))}</p>",
        f"<p>{_t(c.get('cluster_detail'))}</p>",
        f"<p>{_t(c.get('vast_sw_release'))}</p>",
        f"<p>{_n(c['usable_total_tb']) if c.get('usable_total_tb') is not None else NA}</p>",
        f"<p>{(_n(pct) + '%') if pct is not None else NA}</p>",
        f"<p>{escape(drr)}</p>",
        f"<p>{update}</p>",
    ]
    return "<tr>" + "".join(f"<td>{x}</td>" for x in cells) + "</tr>"


def _memo_list(title: str, items: list[str]) -> str:
    if not items:
        return ""
    return ("<p />"
            f"<p><strong>{escape(title)}</strong></p>"
            "<ul>" + "".join(f"<li><p>{x}</p></li>" for x in items) + "</ul>")


OVERVIEW_FOOTER = (
    "<p />"
    '<ac:structured-macro ac:name="info" ac:schema-version="1">'
    "<ac:rich-text-body><p>이 페이지는 "
    "<strong>VAST 고객사 사용현황 포털</strong>이 통째로 생성합니다. "
    "위키에서 직접 고치거나 적은 내용은 다음 발행 때 사라집니다. "
    "표의 값은 포털에서, 메모는 클러스터의 <strong>비고</strong> 또는 "
    "<strong>고객사 메모</strong>에 적으세요.</p>"
    "</ac:rich-text-body></ac:structured-macro>"
)

# 목차 — children 매크로를 쓴다. 매개변수가 없어 틀릴 여지가 없다.
#
# 원래 pagetree 를 쓰려 했고 두 번 실패했다.
#   root="@self" 평문   → API 오류 500. root 는 링크 타입을 기대한다
#   root 생략          → 500 은 안 나지만 스페이스 전체 트리가 그려진다.
#                        Cloud 의 기본 뿌리는 '이 페이지' 가 아니라 스페이스 최상단이다
# 이 페이지의 하위는 클러스터 페이지 한 겹이라, 트리든 목록이든 결과가 같다.
OVERVIEW_TOC = (
    "<p />"
    "<p><strong>목차</strong></p>"
    '<ac:structured-macro ac:name="children" ac:schema-version="1" />'
)


def overview_build(clusters: list[dict], memos: list[dict]) -> str:
    """전체 현황 페이지 본문.

    구성: 표 → 클러스터 비고 → 고객사 메모 → 안내 → 목차
    """
    cols = "".join(f'<col style="width: {w}px;" />' for _, w in OVERVIEW_COLS)
    head = "".join(f"<th><p><strong>{escape(h)}</strong></p></th>"
                   for h, _ in OVERVIEW_COLS)
    body = ("".join(_overview_row(c) for c in clusters) if clusters else
            "<tr>" + "".join(f"<td><p>{NA}</p></td>" for _ in OVERVIEW_COLS) + "</tr>")

    cluster_memos = [
        f"<strong>{_t(c.get('customer_name'))} / {_t(c.get('cluster_name'))}</strong>"
        f" &#8212; {_t(c.get('remark'))}"
        for c in clusters if (c.get("remark") or "").strip()
    ]
    customer_memos = [
        f"<strong>{_t(m.get('name'))}</strong> &#8212; {_t(m.get('remark'))}"
        for m in memos
    ]

    width_attr = (f' data-width="{OVERVIEW_TABLE_WIDTH}"'
                  if OVERVIEW_TABLE_WIDTH else "")

    return "".join([
        f'<table data-layout="{TABLE_LAYOUT}"{width_attr}>'
        f"<colgroup>{cols}</colgroup>"
        f"<thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>",
        _memo_list("클러스터 비고", cluster_memos),
        _memo_list("고객사 메모", customer_memos),
        OVERVIEW_FOOTER,
        OVERVIEW_TOC,
    ])


_TBODY = re.compile(r"<tbody[^>]*>(.*?)</tbody>", re.I | re.S)


def count_table_rows(storage: str) -> int:
    """위키 현재 본문의 첫 표에 있는 데이터 행 수.

    통째로 덮어쓰기 전에 '몇 행이 사라지는가' 를 보여주기 위한 값이다.
    머리글을 세지 않도록 tbody 안의 tr 만 센다. 표가 없으면 0.
    정밀한 파싱이 목적이 아니라 사람이 멈출 근거를 주는 것이 목적이다.
    """
    m = _TBODY.search(storage or "")
    if not m:
        return 0
    return len(re.findall(r"<tr\b", m.group(1), re.I))
