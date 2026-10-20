-- 펌웨어 항목별 장비 모델 기록
--
-- 왜 필요한가
--   같은 'SSD' 라도 클러스터마다 모델이 다르고(Micron 7450 / Samsung PM9A3),
--   스위치는 모델에 따라 펌웨어 계열이 갈린다. 버전만 적어두면 그 버전이
--   어느 장비의 것인지 나중에 알 수 없다.
--
-- 왜 uk_fw 에 넣지 않는가
--   uk_fw (cluster_id, component, checked_at) 가 '같은 날 같은 항목은 덮어쓴다'
--   를 보장한다. model 을 키에 넣으면 모델명을 고칠 때마다 같은 날짜에 행이
--   하나씩 새로 쌓인다. model 은 그 행의 속성이지 식별자가 아니다.
--
-- NULL 허용
--   OS 처럼 모델이 없는 항목이 있고, 이미 들어 있는 56건은 모델을 모른다.
--   필수로 만들면 기존 행을 채울 방법이 없다.

ALTER TABLE cluster_firmware
  ADD COLUMN model VARCHAR(60) DEFAULT NULL
    COMMENT '해당 항목의 장비 모델. 예: SN3700, Micron 7450'
  AFTER component;

-- 뷰에 model 을 내보낸다. SELECT 목록만 늘고 조건은 그대로다.
CREATE OR REPLACE VIEW v_firmware_current AS
SELECT f.cluster_id, f.component, f.model, f.version, f.checked_at
FROM cluster_firmware f
WHERE f.checked_at = (SELECT MAX(f2.checked_at) FROM cluster_firmware f2
                       WHERE f2.cluster_id = f.cluster_id
                         AND f2.component  = f.component);
