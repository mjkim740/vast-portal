-- 전체 현황 페이지 발행 기록
--
-- 왜 필요한가
--   publish_log 는 '발행은 성공해도 실패해도 반드시 한 줄 남는다' 를 전제로
--   만들었다. 전체 현황 페이지 발행은 이 앱에서 가장 되돌리기 어려운 작업이라
--   (페이지 하나를 통째로 교체한다) 로그가 가장 필요한 쪽인데, cluster_id 가
--   NOT NULL 이라 기록할 자리가 없었다.
--
-- 영향
--   NULL 을 허용해도 FK(ON DELETE CASCADE)는 그대로 동작한다. NULL 행은
--   cluster 와 조인되지 않으므로, 클러스터별 발행 이력과 실패 목록을 뽑는
--   기존 쿼리(JOIN cluster)는 결과가 바뀌지 않는다.

ALTER TABLE publish_log
  MODIFY cluster_id BIGINT UNSIGNED NULL
    COMMENT '클러스터별 발행. 전체 현황 페이지 발행이면 NULL';
