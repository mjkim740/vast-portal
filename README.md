# VAST 고객사 사용현황 포털

VAST Data를 쓰는 고객사의 클러스터 구성·용량·라이선스·네트워크를 기록하고,
그 내용으로 Confluence 페이지를 발행한다.

**DB가 원본이고 위키는 출력물이다.** 값은 포털에서 고치고, 위키는 발행으로 갱신한다.
위키에서 표를 직접 고치면 다음 발행 때 덮어써진다.

이전에는 Confluence 위키에 직접 표를 작성했다. 고객사가 늘면서 표기가 제각각이 되고
(`2 CL`, `CL x2`, `CL(2)`), 라이선스 만료나 갱신 지연을 한눈에 볼 수 없어 전환했다.

---

## 구성

```
브라우저
   │
   ├─ vast-portal.ds.local ──→ HAProxy(10.20.15.185:80)
   │                              └─→ ingress-nginx NodePort 31080
   │                                     └─→ Service portal-api
   │
   └─ 10.20.15.121:31090 ─────→ Service portal-api (NodePort, 이름 해석이 안 될 때)
                                     │
                                     ├─→ Deployment portal-api (FastAPI, 2 replica)
                                     │        └─→ Confluence REST v2 (발행)
                                     └─→ StatefulSet mariadb (NFS PVC)
```

- **WAS** FastAPI + Jinja2. 화면이 폼과 표뿐이라 SPA를 쓰지 않았다.
- **DB** MariaDB 11.4. NFS(`vastdata-delete` StorageClass) 위에 올라간다.
- **인증 없음.** 사내망 공개 전제다. 그래서 비밀번호·라이선스 키는 스키마에 넣지 않았다.

---

## 디렉터리

```
db/
  schema.sql                        전체 스키마 (새로 구축할 때)
  migrations/
    2026-10-13-publish.sql          기존 DB에 추가 적용할 변경분
infra/
  mariadb.yaml                      MariaDB StatefulSet + PVC + Service
  haproxy.cfg                       HAProxy → ingress-nginx
portal/
  Dockerfile                        python:3.12-slim
  requirements.txt
  k8s/portal.yaml                   Deployment + Service(NodePort) + Ingress
  app/
    main.py                         클러스터 CRUD, 장비, 모델 관리
    routes_data.py                  네트워크 · 라이선스 · 펌웨어 · 용량
    routes_publish.py               Confluence 발행
    confluence.py                   REST v2 클라이언트
    page_body.py                    DB 값 → 위키 본문(storage XHTML)
    netparse.py                     주소 파싱, 라벨 정규화, 행 순서
    audit.py                        변경 이력 기록
    db.py                           커넥션
    templates/  static/
```

### 스키마 파일 두 개를 함께 돌리지 말 것

같은 뷰를 정의한다. 둘 중 하나만 적용한다.

| 상황 | 적용할 파일 |
|---|---|
| 새로 구축 | `db/schema.sql` 만 |
| 이미 쓰고 있는 DB | `db/migrations/` 의 변경분만 |

---

## 처음부터 구축

### 1. 네임스페이스와 Secret

비밀값은 레포에 없다. 명령으로 만든다.

```bash
kubectl create namespace vast-portal

kubectl create secret generic mariadb-secret -n vast-portal \
  --from-literal=root-password='<루트 비밀번호>' \
  --from-literal=username=portal \
  --from-literal=password='<앱 비밀번호>'
```

### 2. MariaDB

```bash
kubectl apply -f infra/mariadb.yaml
kubectl rollout status -n vast-portal statefulset/mariadb --timeout=180s
```

### 3. 스키마

```bash
kubectl exec -i -n vast-portal mariadb-0 -- sh -c \
  'mariadb -uroot -p"$MARIADB_ROOT_PASSWORD" ' < db/schema.sql
```

> 파이프로 넣을 때 `-p` 뒤에 비밀번호를 붙여야 한다. 그냥 `-p` 로 두면
> 비밀번호 프롬프트가 stdin 을 먹어서 `ERROR 1045` 가 난다.

적용 결과를 반드시 확인한다. 테이블은 생겼는데 뷰만 전부 실패하는 경우가 있었다
(연결 charset 과 `COLLATE` 충돌). `show tables` 로는 안 보인다.

```bash
kubectl exec -n vast-portal mariadb-0 -- sh -c \
  'mariadb -uroot -p"$MARIADB_ROOT_PASSWORD" vast_portal -e "SHOW FULL TABLES"' \
  | awk '{print $2}' | sort | uniq -c
```

`12 BASE TABLE` / `8 VIEW` 가 나와야 한다.

### 4. Confluence 인증 (발행 기능)

[id.atlassian.com](https://id.atlassian.com/manage-profile/security/api-tokens) 에서 API 토큰을 발급한다.

```bash
 read -rsp "토큰: " TOKEN; echo          # 맨 앞 공백 — 셸 history 에 안 남는다

# Confluence 엔드포인트로 확인한다. /rest/api/3/* 은 Jira 라서,
# Jira 권한이 없으면 토큰이 멀쩡해도 'Client must be authenticated' 가 나온다.
curl -s -o /dev/null -w '%{http_code}\n' -u "<계정 이메일>:$TOKEN" \
  'https://cpf-ds.atlassian.net/wiki/rest/api/user/current'      # 200 이어야 함

kubectl create secret generic confluence-secret -n vast-portal \
  --from-literal=email='<계정 이메일>' \
  --from-literal=token="$TOKEN"
unset TOKEN
```

Secret 이 없어도 앱은 뜬다. 발행 버튼만 비활성되고, 미리보기와 소스 복사는 동작한다.

### 5. 앱 빌드·배포

```bash
cd portal
docker build -t 10.20.15.121:5000/vast-portal:v11 .
docker push 10.20.15.121:5000/vast-portal:v11
kubectl apply -f k8s/portal.yaml
kubectl rollout status -n vast-portal deploy/portal-api --timeout=120s
```

`k8s/portal.yaml` 의 이미지 태그와 빌드 태그를 맞춘다.

### 6. HAProxy

물리 서버(10.20.15.185)에 설치한다.

```bash
cp infra/haproxy.cfg /etc/haproxy/haproxy.cfg
haproxy -c -f /etc/haproxy/haproxy.cfg     # 문법 검사
systemctl reload haproxy
```

ingress-nginx 의 `externalTrafficPolicy` 가 `Local` 이라, ingress 파드가 없는 노드는
연결을 거부한다. 백엔드에 전 노드를 적고 health check 로 걸러내는 구조다.

### 7. 확인

```bash
curl -s http://vast-portal.ds.local/healthz; echo
curl -s http://vast-portal.ds.local/api/confluence/check; echo
```

```json
{"status":"ok","version":"2026-10-13.8"}
{"ok":true,"base_url":"https://cpf-ds.atlassian.net","account":"...","display_name":"..."}
```

---

## 운영

### 배포한 코드가 어느 판인지 확인

```bash
curl -s http://vast-portal.ds.local/healthz; echo
```

`version` 이 `app/main.py` 의 `APP_VERSION` 이다. 화면 머리글 오른쪽에도 작게 보인다.

**이미지 태그는 믿을 게 못 된다.** 같은 태그로 덮어 빌드하거나 예전 소스로 새 태그를
만들면 태그와 내용이 어긋난다. 실제로 그 때문에 한 번 헤맸다. 코드를 고치면
`APP_VERSION` 과 이미지 태그를 같이 올린다.

### 접속 경로

| 주소 | 경로 |
|---|---|
| `http://vast-portal.ds.local/` | HAProxy → ingress |
| `http://10.20.15.121:31090/` | NodePort 직결. 이름 해석이 안 되는 PC/서버에서 |

NodePort 쪽은 노드 IP 아무거나(121~125) 써도 된다.

### 자주 쓰는 명령

```bash
kubectl logs -n vast-portal deploy/portal-api --tail=50
kubectl get pods -n vast-portal -o wide

# DB 접속
kubectl exec -it -n vast-portal mariadb-0 -- sh -c \
  'mariadb -uroot -p"$MARIADB_ROOT_PASSWORD" vast_portal'
```

MariaDB 11.x 에는 `mysql` / `mysqldump` 명령이 없다. `mariadb` / `mariadb-dump` 를 쓴다.

---

## 데이터 모델에서 알아둘 것

**클러스터는 PSNT 로 식별한다.** 클러스터명은 중복될 수 있다(KT클라우드 가산/예천 동명).
PSNT 는 미입력도 허용한다.

**라이선스 상태는 저장하지 않는다.** 날짜에서 파생한다(`v_license_status`).
저장해 두면 만료일이 지나도 ACTIVE 로 남는다.

**System 사용량은 계산값이다.** `Total − Used − Free` 로 나오는 생성 컬럼이다.

**위키 페이지 제목은 `고객사 - 용도`.** 같은 고객사에 용도가 같은 클러스터가 둘이면
제목이 겹쳐 서로의 페이지를 덮어쓴다. 발행 전에 검사해서 막고, 발행 화면에서
제목을 직접 지정할 수 있다(`cluster.wiki_title`).

**네트워크 행 순서.** 위키 표의 고정 11행은 순서가 정해져 있다. 추가 행 중
`Service IP 3` 처럼 끝 숫자만 다른 것은 그 계열 바로 뒤에 번호순으로 들어간다.
나머지 추가 행은 맨 뒤.

**자격증명과 라이선스 키는 스키마에 없다.** 포털에 인증이 없어서 일부러 뺐다.
기존 위키에는 스위치·VMS 비밀번호가 평문으로 있었는데, 옮기지 않았다.

---

## 입력 요령

**주소** 위키에 쓰던 표기 그대로.

```
10.62.74.61/26                      한 개
10.62.74.51-53/26                   연속 대역
165.243.17.15, 165.244.235.38       여러 개
192.168.10.{151,154,157,160}        띄엄띄엄 — 끝자리만 묶어서
192.168.10.{151,154,160-164}/24     범위 섞어서
```

중괄호 표기는 한 주소씩 따로 저장된다(IP 검색을 위해). 위키에는 다시 묶여서 나간다.

**Enter 로는 저장되지 않는다.** 다음 칸으로 넘어간다. 저장은 저장 버튼만.
저장이 "전체 교체" 방식이라, 입력 중간에 제출되면 아직 못 채운 행이 빠진 채 확정된다.

**저장하지 않고 탭을 옮기면 묻는다.** 입력한 값이 사라지기 때문이다.

---

## 알려진 제약

- **인증이 없다.** 사내망 전제다. 외부에 열면 안 된다.
- **Confluence 날짜 위젯은 마크다운으로 내보낼 때 사라진다.** 위키에서 날짜를
  위젯이 아니라 평문으로 적어야 한다.
- **2단 레이아웃은 storage → 에디터 변환에 의존한다.** 깨지면
  `page_body.py` 의 `TWO_COLUMN = False` 로 바꾸면 표가 위아래로 나열된다
  (변환 위험 없음).
- **위키 페이지는 포털에서 지워도 남는다.** 클러스터를 삭제해도 위키 페이지는
  그대로다. 운영 문서가 실수로 날아가는 편이 더 위험하다고 봤다.

---

## 남은 일

- [ ] 기존 고객사 페이지 데이터를 포털로 옮기기 (32개 중 일부 진행)
- [ ] 인증 (사내 SSO 또는 ingress basic auth)
- [ ] 라이선스 만료 알림
