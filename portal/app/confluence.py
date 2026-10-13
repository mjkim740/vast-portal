"""Confluence Cloud REST v2 클라이언트.

인증: Basic (계정 이메일 : API 토큰). 토큰은 k8s Secret 에서 환경변수로 온다.
      코드와 이미지에는 토큰이 없다.

주의
  - /rest/api/3/* 은 Jira 엔드포인트다. Confluence 는 /wiki/api/v2/* 이다.
    Jira 권한이 없는 계정으로 Jira 엔드포인트를 때리면 토큰이 정상이어도
    'Client must be authenticated' 가 나온다. 진단할 때 헷갈리는 지점이다.
  - 페이지 수정은 version.number 를 직전 값 + 1 로 보내야 한다.
    동시에 두 사람이 발행하면 409 가 나는데, 그때는 다시 읽어서 재시도한다.
"""

from __future__ import annotations

import os

import httpx

BASE_URL = os.getenv("CONFLUENCE_BASE_URL", "https://cpf-ds.atlassian.net").rstrip("/")
EMAIL    = os.getenv("CONFLUENCE_EMAIL", "")
TOKEN    = os.getenv("CONFLUENCE_TOKEN", "")
PARENT   = os.getenv("CONFLUENCE_PARENT_ID", "")   # 없으면 스페이스 최상단
TIMEOUT  = float(os.getenv("CONFLUENCE_TIMEOUT", "20"))


class ConfluenceError(Exception):
    """발행 실패. message 가 그대로 publish_log.message 에 들어간다."""


def configured() -> bool:
    return bool(EMAIL and TOKEN)


def _client() -> httpx.Client:
    if not configured():
        raise ConfluenceError(
            "Confluence 인증 정보가 없습니다. "
            "CONFLUENCE_EMAIL / CONFLUENCE_TOKEN 환경변수를 확인하세요."
        )
    return httpx.Client(
        base_url=BASE_URL,
        auth=(EMAIL, TOKEN),
        timeout=TIMEOUT,
        headers={"Accept": "application/json", "Content-Type": "application/json"},
    )


def _call(method: str, path: str, **kw) -> dict:
    with _client() as c:
        try:
            r = c.request(method, path, **kw)
        except httpx.HTTPError as e:
            raise ConfluenceError(f"연결 실패: {e}") from e

    if r.status_code == 401:
        raise ConfluenceError(
            "인증 실패(401). 계정 이메일과 API 토큰을 확인하세요. "
            "토큰은 id.atlassian.com 에서 재발급합니다."
        )
    if r.status_code == 403:
        raise ConfluenceError(
            "권한 없음(403). 해당 계정이 이 스페이스에 쓰기 권한이 있는지, "
            "Confluence 라이선스가 할당되어 있는지 확인하세요."
        )
    if r.status_code == 404:
        raise ConfluenceError(f"대상을 찾을 수 없습니다(404): {path}")
    if r.status_code == 409:
        raise ConfluenceError(
            "버전 충돌(409). 다른 사람이 페이지를 먼저 수정했습니다. 다시 발행하세요."
        )
    if r.status_code >= 400:
        raise ConfluenceError(f"API 오류 {r.status_code}: {_reason(r)}")

    if not r.content:
        return {}
    try:
        return r.json()
    except ValueError:
        raise ConfluenceError(f"응답을 해석할 수 없습니다: {r.text[:200]}") from None


def _reason(r: httpx.Response) -> str:
    """Atlassian 오류 응답은 모양이 몇 가지라 한 줄로 줄인다."""
    try:
        d = r.json()
    except ValueError:
        return r.text[:300]
    for key in ("message", "title", "detail"):
        if isinstance(d.get(key), str):
            return d[key]
    errs = d.get("errors")
    if isinstance(errs, list) and errs:
        first = errs[0]
        if isinstance(first, dict):
            return str(first.get("title") or first.get("detail") or first)
        return str(first)
    return str(d)[:300]


# --------------------------------------------------------------- 진단

def whoami() -> dict:
    """설정 화면과 발행 전 점검용. Confluence 엔드포인트를 쓴다."""
    return _call("GET", "/wiki/rest/api/user/current")


# --------------------------------------------------------------- 스페이스 / 페이지

def space_id(key: str) -> str:
    d = _call("GET", "/wiki/api/v2/spaces", params={"keys": key, "limit": 1})
    results = d.get("results") or []
    if not results:
        raise ConfluenceError(f"스페이스를 찾을 수 없습니다: {key}")
    return str(results[0]["id"])


def find_page_by_title(sid: str, title: str) -> dict | None:
    """제목으로 기존 페이지를 찾는다. page_id 가 비어 있는 첫 발행 때 쓴다.

    이미 같은 제목의 페이지가 있으면 새로 만들지 않고 그 페이지를 갱신한다.
    Confluence 는 한 스페이스에서 제목이 유일해야 하므로, 그냥 만들면 400 이 난다.
    """
    d = _call("GET", f"/wiki/api/v2/spaces/{sid}/pages",
              params={"title": title, "status": "current", "limit": 5})
    for p in d.get("results") or []:
        if p.get("title") == title:
            return {"id": str(p["id"]), "title": p["title"],
                    "version": int((p.get("version") or {}).get("number") or 1)}
    return None


def get_page(page_id: str) -> dict:
    d = _call("GET", f"/wiki/api/v2/pages/{page_id}")
    return {
        "id":      str(d["id"]),
        "title":   d.get("title") or "",
        "status":  d.get("status") or "current",
        "version": int((d.get("version") or {}).get("number") or 1),
        "url":     page_url(d),
    }


def page_url(d: dict) -> str:
    webui = ((d.get("_links") or {}).get("webui")) or ""
    return f"{BASE_URL}/wiki{webui}" if webui else f"{BASE_URL}/wiki/spaces"


def create_page(sid: str, title: str, body: str) -> dict:
    payload = {
        "spaceId": sid,
        "status":  "current",
        "title":   title,
        "body":    {"representation": "storage", "value": body},
    }
    if PARENT:
        payload["parentId"] = PARENT
    d = _call("POST", "/wiki/api/v2/pages", json=payload)
    return {"id": str(d["id"]),
            "version": int((d.get("version") or {}).get("number") or 1),
            "url": page_url(d)}


def update_page(page_id: str, title: str, body: str, version: int,
                note: str = "포털에서 발행") -> dict:
    payload = {
        "id":      page_id,
        "status":  "current",
        "title":   title,
        "body":    {"representation": "storage", "value": body},
        "version": {"number": version + 1, "message": note},
    }
    d = _call("PUT", f"/wiki/api/v2/pages/{page_id}", json=payload)
    return {"id": str(d["id"]),
            "version": int((d.get("version") or {}).get("number") or version + 1),
            "url": page_url(d)}


def publish(space_key: str, page_id: str | None, title: str, body: str,
            note: str = "포털에서 발행") -> dict:
    """페이지가 있으면 갱신, 없으면 생성. 결과의 id 를 DB 에 저장해 다음부터 갱신한다."""
    sid = space_id(space_key)

    if page_id:
        try:
            cur = get_page(page_id)
        except ConfluenceError:
            page_id = None          # 완전 삭제된 경우 — 아래에서 다시 만든다
        else:
            # 휴지통에 있는 페이지는 404 가 아니라 status='trashed' 로 읽힌다.
            # 그대로 갱신하면 되살아나므로(의도와 다름) 없는 것으로 보고 새로 만든다.
            if cur["status"] != "current":
                page_id = None
            else:
                return update_page(page_id, title, body,
                                   cur["version"], note) | {"created": False}

    found = find_page_by_title(sid, title)
    if found:
        return update_page(found["id"], title, body, found["version"], note) | {"created": False}

    try:
        return create_page(sid, title, body) | {"created": True}
    except ConfluenceError as e:
        # 휴지통의 페이지가 제목을 붙들고 있으면 생성이 거부된다.
        # 사유를 짐작하게 두지 않고 할 일을 알려준다.
        if "title" in str(e).lower() or "already exists" in str(e).lower():
            raise ConfluenceError(
                f"같은 제목의 페이지가 이미 있습니다: '{title}'. "
                "위키 휴지통에 남아 있으면 비우고 다시 발행하세요."
            ) from e
        raise
