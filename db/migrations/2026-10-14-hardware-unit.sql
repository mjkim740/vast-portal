-- =====================================================================
-- 장비 개체 (시리얼 · 파트넘버)
--
-- 이미 쓰고 있는 DB 에 이 파일만 추가로 돌린다.
--
--   kubectl exec -i -n vast-portal mariadb-0 -- sh -c \
--     'mariadb -uroot -p"$MARIADB_ROOT_PASSWORD" vast_portal' \
--     < db/migrations/2026-10-14-hardware-unit.sql
-- =====================================================================

USE vast_portal;

-- ---------------------------------------------------------------------
-- 장비 한 대 = 한 행
--
-- 왜 cluster_hardware 에 컬럼을 붙이지 않았나
--   cluster_hardware 는 'SMC 3대' 처럼 모델별 한 행이다. 거기에 시리얼 칸을
--   붙이면 3대분을 한 칸에 쉼표로 넣게 되고, 나중에 시리얼로 장비를 찾을 수
--   없다. 네트워크 주소를 한 개씩 저장한 것과 같은 이유다.
--
-- 왜 cluster_hardware.id 를 참조하지 않나  ← 중요
--   기본 폼을 저장할 때 cluster_hardware 는 전체 교체된다(DELETE 후 INSERT).
--   거기에 FK 로 매달면 기본 정보만 고쳐도 시리얼이 전부 지워진다.
--   그래서 클러스터에 직접 매달고, 장비 행과는 (kind, model_name) 으로 맞춘다.
--
--   대신 모델명을 바꾸거나 장비 행을 지우면 시리얼이 떠 있게 된다.
--   입력 화면에서 '현재 장비 목록에 없는 시리얼' 로 따로 보여주고 지울 수 있다.
--   조용히 사라지는 것보다 보이게 두는 편이 낫다.
--
-- seq 는 같은 모델 안에서의 번호다(1호기, 2호기...).
-- 수량이 3이면 화면에 1~3 칸이 뜨고, 아는 것만 채우면 된다.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS hardware_unit (
  id         BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  cluster_id BIGINT UNSIGNED NOT NULL,
  kind       ENUM('C','D','E','S') NOT NULL,
  model_name VARCHAR(50)  NOT NULL COMMENT 'cluster_hardware.model_name 과 맞춘다',
  seq        SMALLINT UNSIGNED NOT NULL COMMENT '같은 모델 안에서의 번호 (1부터)',

  serial_no  VARCHAR(60)           COMMENT '장비 시리얼',
  part_no    VARCHAR(60)           COMMENT '파트넘버. 구성에 따라 같은 모델도 다를 수 있다',
  remark     VARCHAR(200),

  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  updated_by VARCHAR(50),

  PRIMARY KEY (id),
  UNIQUE KEY uk_unit      (cluster_id, kind, model_name, seq),
  -- 시리얼은 전사에서 하나뿐이어야 한다. 손으로 옮겨 적다 같은 값을 두 번
  -- 넣는 실수를 여기서 잡는다. NULL 은 여러 개 허용되므로 미입력은 자유롭다.
  UNIQUE KEY uk_unit_serial (serial_no),
  KEY idx_unit_part (part_no),
  CONSTRAINT fk_unit_cluster FOREIGN KEY (cluster_id)
    REFERENCES cluster(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ---------------------------------------------------------------------
-- 등록 현황 — '3대 중 2대 등록' 을 화면과 위키에서 쓴다
-- ---------------------------------------------------------------------
CREATE OR REPLACE VIEW v_hardware_unit_fill AS
SELECT
  h.cluster_id,
  h.kind,
  h.model_name,
  h.quantity,
  COALESCE(u.filled, 0) AS filled
FROM cluster_hardware h
LEFT JOIN (
  SELECT cluster_id, kind, model_name, COUNT(*) AS filled
  FROM hardware_unit
  WHERE serial_no IS NOT NULL AND serial_no <> ''
  GROUP BY cluster_id, kind, model_name
) u ON u.cluster_id = h.cluster_id
   AND u.kind       = h.kind
   AND u.model_name = h.model_name;

-- ---------------------------------------------------------------------
-- 확인
-- ---------------------------------------------------------------------
SELECT '장비 개체' AS 항목, COUNT(*) AS 건수 FROM hardware_unit;
SHOW COLUMNS FROM hardware_unit;
