"""주소 입력 문자열을 구조화한다.

폼에서 '10.62.74.51-53/26' 처럼 위키에 쓰던 표기를 그대로 받아
시작/끝/프리픽스로 쪼갠다. 칸을 다섯 개로 나누면 입력이 너무 번거롭고,
표기 자체가 단순해서 파싱 위험이 낮다.

쉼표로 여러 개를 쓰면 각각 한 행이 된다. (dns 처럼 2개인 경우)
"""

from __future__ import annotations

import re

IP = r"\d{1,3}(?:\.\d{1,3}){3}"
# 10.62.74.61/26   10.62.74.51-53/26   192.168.10.212-215   10.62.74.1
ADDR = re.compile(rf"^({IP})(?:\s*[-~]\s*(\d{{1,3}}|{IP}))?(?:\s*/\s*(\d{{1,2}}))?$")

NONE_MARKS = {"", "-", "–", "—", "na", "n/a", "none", "없음"}


def is_blank(s: str | None) -> bool:
    return (s or "").strip().lower() in NONE_MARKS


# 192.168.10.{151,154,157}  192.168.10.{151,154,160-164}/24
BRACE = re.compile(r"(\d{1,3}(?:\.\d{1,3}){2}\.)\{([^}]*)\}(\s*/\s*\d{1,2})?")


def expand_braces(text: str) -> tuple[str, list[str]]:
    """'192.168.10.{151,154,157}' → '192.168.10.151, 192.168.10.154, 192.168.10.157'

    연속 대역이 아닌 주소를 한 줄로 적기 위한 표기다. 앞 세 자리를 여덟 번
    되풀이해 적는 대신 마지막 자리만 묶어 쓴다. 중괄호 안에는 숫자와
    '160-164' 같은 범위를 쉼표로 섞어 쓸 수 있다.

    저장은 그대로 한 주소씩 한 행이다. 묶어 두면 IP 로 검색할 수 없다.

    반환: (펼친 문자열, 읽지 못한 조각 목록)
    """
    bad: list[str] = []

    def one(m: re.Match) -> str:
        prefix, inner, suffix = m.group(1), m.group(2), (m.group(3) or "").strip()
        parts: list[str] = []
        for piece in inner.split(","):
            piece = piece.strip()
            if not piece:
                continue
            rng = re.fullmatch(r"(\d{1,3})\s*[-~]\s*(\d{1,3})", piece)
            if rng:
                a, b = int(rng.group(1)), int(rng.group(2))
                if a > b or b > 255:
                    bad.append(f"{prefix}{{{piece}}}")
                    continue
                parts += [f"{prefix}{n}" for n in range(a, b + 1)]
            elif piece.isdigit() and int(piece) <= 255:
                parts.append(f"{prefix}{piece}")
            else:
                bad.append(f"{prefix}{{{piece}}}")
        return ", ".join(p + suffix for p in parts)

    return BRACE.sub(one, str(text)), bad


def parse_addresses(text: str) -> tuple[list[dict], list[str]]:
    """'10.62.74.51-53/26, 10.62.74.61' → 주소 목록.

    중괄호 표기는 먼저 펼친다. 쉼표로 쪼개기 전에 해야 한다 —
    중괄호 안에도 쉼표가 있어서, 순서가 바뀌면 조각이 난다.

    반환: (파싱된 주소 목록, 읽지 못한 조각 목록)
    """
    out: list[dict] = []
    bad: list[str] = []
    if is_blank(text):
        return out, bad

    text, bad = expand_braces(text)

    for part in str(text).split(","):
        part = part.strip()
        if not part:
            continue
        m = ADDR.match(part)
        if not m:
            bad.append(part)
            continue

        start, end, prefix = m.group(1), m.group(2), m.group(3)
        if end and "." not in end:          # '51-53' 처럼 마지막 옥텟만 준 경우
            end = start.rsplit(".", 1)[0] + "." + end

        if not _valid(start) or (end and not _valid(end)):
            bad.append(part)
            continue

        out.append({
            "addr_start": start,
            "addr_end": end,
            "prefix_len": int(prefix) if prefix else None,
        })
    return out, bad


def _valid(ip: str) -> bool:
    return all(0 <= int(o) <= 255 for o in ip.split("."))


def format_address(addr_start: str, addr_end: str | None, prefix_len: int | None) -> str:
    """DB 값을 폼 입력 형식으로 되돌린다."""
    s = addr_start or ""
    if addr_end:
        # 같은 /24 안이면 마지막 옥텟만 보여준다 (입력할 때 쓰던 표기)
        if addr_start.rsplit(".", 1)[0] == addr_end.rsplit(".", 1)[0]:
            s += "-" + addr_end.rsplit(".", 1)[1]
        else:
            s += "-" + addr_end
    if prefix_len:
        s += f"/{prefix_len}"
    return s


# 위키 템플릿의 표준 행. 신규 등록 시 이 순서로 미리 깔아준다.
#   (wiki_label, role, fabric)
DEFAULT_ROWS = [
    ("VMS mgmt ip",    "VMS",        "MGMT"),
    ("Cnode mgmt ip",  "CNODE",      "MGMT"),
    ("Dnode mgmt ip",  "DNODE",      "MGMT"),
    ("Switch mgmt ip", "SWITCH",     "MGMT"),
    ("MGMT GW",        "MGMT_GW",    "MGMT"),
    ("Service IP 1",   "SERVICE",    "SERVICE_ETH"),
    ("Service IP 2",   "SERVICE",    "SERVICE_ETH"),
    ("Service GW",     "SERVICE_GW", "SERVICE_ETH"),
    ("vast-dns",       "VAST_DNS",   "EXTERNAL"),
    ("dns",            "DNS",        "EXTERNAL"),
    ("ntp",            "NTP",        "MGMT"),
]

ROLES = ["VMS", "CNODE", "DNODE", "SWITCH", "MGMT_GW", "SERVICE",
         "SERVICE_GW", "DNS", "VAST_DNS", "NTP", "TEST", "BACKUP", "OTHER"]

FABRICS = ["MGMT", "IPMI", "SERVICE_ETH", "SERVICE_IB", "SERVICE_ROCE", "EXTERNAL"]

# --------------------------------------------------------------- 라벨 정규화

STD_LABELS = [lbl for lbl, _, _ in DEFAULT_ROWS]


def label_key(s: str) -> str:
    """비교용 키. 대소문자, 공백, 하이픈, 밑줄 차이를 무시한다.

    'vast dns' 와 'vast-dns' 를 같게 보려고 구분자를 전부 떼낸다.
    """
    return re.sub(r"[^a-z0-9가-힣]", "", (s or "").lower())


_STD_BY_KEY = {label_key(lbl): lbl for lbl in STD_LABELS}


def canonical_label(s: str) -> str:
    """표준 행과 철자만 다른 라벨을 표준 표기로 맞춘다.

    왜 필요한가
      위키 출력은 라벨로 행을 묶는다. 'vast dns' 로 저장하면 표준 행
      'vast-dns' 는 값이 없어 '-' 로 나가고, 'vast dns' 가 별도 행으로 또 나와
      같은 항목이 두 줄로 보인다.

    판단이 애매한 건 건드리지 않는다. 'Service ip' 는 'Service IP 1' 인지 2 인지
    알 수 없으므로 사용자가 적은 대로 둔다. 그런 라벨은 화면에서 표시해 준다.
    """
    s = re.sub(r"\s+", " ", (s or "").strip())
    return _STD_BY_KEY.get(label_key(s), s)


def is_standard(s: str) -> bool:
    return label_key(s) in _STD_BY_KEY


# --------------------------------------------------------------- 행 순서

def stem(s: str) -> str:
    """끝의 숫자를 뗀 비교용 줄기. 'Service IP 3' → 'serviceip'

    같은 계열의 행을 알아보기 위한 것이다. 'Service IP 3' 을 추가하면
    'Service IP 1', 'Service IP 2' 바로 뒤에 붙어야지, 표 맨 아래로 가면
    위키에서 읽기 어렵다.
    """
    return re.sub(r"\d+$", "", label_key(s))


def tail_number(s: str) -> int:
    """끝의 숫자. 'Service IP 3' → 3. 없으면 0."""
    m = re.search(r"(\d+)\s*$", (s or "").strip())
    return int(m.group(1)) if m else 0


def order_labels(labels: list[str]) -> list[str]:
    """위키 표에 나갈 순서로 라벨을 늘어놓는다.

    규칙
      1. 표준 11개 행은 위키 표의 순서를 그대로 지킨다.
      2. 표준 행과 같은 계열(끝 숫자만 다른) 추가 행은 그 계열 바로 뒤에
         번호순으로 붙인다. Service IP 3, 4 는 Service IP 2 다음이다.
      3. 어느 계열에도 안 붙는 추가 행(Cnode ipmi ip 등)은 맨 뒤에 둔다.

    반환: 정렬된 라벨 목록. 주어진 labels 에 없는 표준 라벨도 포함한다
          (위키 표는 값이 없어도 '-' 로 행을 유지한다).
    """
    given = {label_key(l): l for l in labels}

    std_keys = [label_key(l) for l in STD_LABELS]
    extras = [k for k in given if k not in set(std_keys)]

    # 계열별로 묶는다
    by_stem: dict[str, list[str]] = {}
    for k in extras:
        by_stem.setdefault(stem(k), []).append(k)

    out: list[str] = []
    for i, lb in enumerate(STD_LABELS):
        out.append(lb)
        # 이 계열의 마지막 표준 행 뒤에 같은 계열 추가 행을 붙인다
        st = stem(lb)
        last_of_stem = not any(stem(x) == st for x in STD_LABELS[i + 1:])
        if last_of_stem and st in by_stem:
            same = sorted(by_stem.pop(st), key=lambda k: (tail_number(given[k]), k))
            out += [given[k] for k in same]

    # 어느 계열에도 안 붙은 것들
    leftover = [k for ks in by_stem.values() for k in ks]
    out += [given[k] for k in sorted(leftover)]
    return out
