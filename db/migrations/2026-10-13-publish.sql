-- =====================================================================
-- schema_v4 추가 적용분 — 3단계(Confluence 발행)
--
-- schema_v4.sql 을 이미 적용한 DB 에 이 파일만 추가로 돌리면 된다.
-- 테이블 변경은 없다. 뷰 하나를 교체한다.
--
--   kubectl exec -i -n vast-portal mariadb-0 -- sh -c \
--     'mariadb -uroot -p"$MARIADB_ROOT_PASSWORD" vast_portal' \
--     < schema_v4_publish.sql
-- =====================================================================

USE vast_portal;

-- ---------------------------------------------------------------------
-- 발행 필요 목록 — 자녀 테이블 변경까지 본다
--
-- 왜 고치는가
--   기존 뷰는 cluster.updated_at 만 봤다. 그런데 네트워크·라이선스·펌웨어·
--   용량을 고쳐도 cluster 행은 건드리지 않으므로 updated_at 이 그대로다.
--   즉 "IP 를 바꾸고 발행을 안 한" 클러스터가 목록에 안 잡혔다.
--
--   change_log 는 자녀 테이블 저장 때마다 한 줄씩 남는다(앱에서 기록).
--   그 마지막 시각과 cluster.updated_at 중 늦은 쪽을 '마지막 변경' 으로 본다.
--
--   발행은 change_log 를 쓰지 않으므로 '발행 → 또 발행 필요' 로 도는 일은 없다.
--
-- DECOMM 은 제외한다. 철거한 클러스터가 발행 대기에 계속 남으면
-- 목록이 쓸모없어진다. 위키 페이지를 내리는 건 수기 작업이다.
-- ---------------------------------------------------------------------
CREATE OR REPLACE VIEW v_publish_pending AS
SELECT
  c.id   AS cluster_id,
  cu.name AS customer_name,
  c.name  AS cluster_name,
  GREATEST(c.updated_at, COALESCE(g.last_change, c.updated_at)) AS updated_at,
  p.published_at,
  p.status AS last_status
FROM cluster c
JOIN customer cu ON cu.id = c.customer_id
LEFT JOIN (
  SELECT cluster_id, MAX(changed_at) AS last_change
  FROM change_log GROUP BY cluster_id
) g ON g.cluster_id = c.id
LEFT JOIN publish_log p ON p.cluster_id = c.id
 AND p.id = (SELECT MAX(p2.id) FROM publish_log p2 WHERE p2.cluster_id = c.id)
WHERE c.status <> 'DECOMM'
  AND (
       p.id IS NULL
    OR p.status = 'FAILED'
    OR p.published_at < GREATEST(c.updated_at, COALESCE(g.last_change, c.updated_at))
  );

-- ---------------------------------------------------------------------
-- 위키 페이지 제목
--
-- 왜 컬럼이 필요한가
--   제목은 기본적으로 '고객사 - 용도' 로 만든다. 그런데 같은 고객사에 용도가
--   같은 클러스터가 둘이면 제목이 겹친다(KT클라우드 가산/예천처럼 이름이
--   같은 사례가 이미 있다). Confluence 는 한 스페이스에서 제목이 유일해야
--   하므로, 두 번째 발행이 첫 번째의 페이지를 덮어쓰게 된다.
--
--   이 컬럼에 제목을 직접 적으면 그 값을 쓴다. 비워 두면 자동 생성이다.
--   UNIQUE 를 걸어 DB 단계에서 같은 제목이 두 번 등록되지 않게 막는다.
--   (NULL 은 여러 개 허용되므로 자동 생성을 쓰는 클러스터는 제약을 안 받는다.
--    자동 생성끼리의 충돌은 앱이 발행 전에 검사한다.)
-- ---------------------------------------------------------------------
ALTER TABLE cluster
  ADD COLUMN wiki_title VARCHAR(200) NULL
    COMMENT '위키 페이지 제목. 비우면 "고객사 - 용도" 로 자동 생성'
    AFTER confluence_page_id,
  ADD UNIQUE KEY uk_cluster_wiki_title (wiki_title);

-- ---------------------------------------------------------------------
-- 확인
-- ---------------------------------------------------------------------
SELECT '발행 대기' AS 항목, COUNT(*) AS 건수 FROM v_publish_pending;
