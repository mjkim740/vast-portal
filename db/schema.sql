-- =====================================================================
-- VAST 고객사 사용현황 관리 시스템 - 스키마 v4 (재설계)
--
-- 데이터 흐름이 바뀌었다.
--   v3 : Confluence MD → 파싱 → DB        (위키가 입력, DB가 사본)
--   v4 : 웹 폼 → DB → Confluence 페이지 발행 (DB가 원본, 위키가 출력물)
--
-- 그에 따라
--   - 파싱용 원문 보존 컬럼(raw_text, cluster_size_detail) 제거
--   - size_spec('3*1') 제거 → 장비 수량에서 계산
--   - 드롭다운 목록을 조회 테이블로 분리 (모델 추가 시 코드 수정 불필요)
--   - 발행 이력(publish_log)과 변경 이력(change_log) 추가
--
-- 대상: MySQL 8.0.16+ / MariaDB 10.5+
-- =====================================================================

CREATE DATABASE IF NOT EXISTS vast_portal
  DEFAULT CHARACTER SET utf8mb4
  DEFAULT COLLATE utf8mb4_unicode_ci;

USE vast_portal;

-- =====================================================================
-- 1. 코드 테이블 (폼 드롭다운 소스)
-- =====================================================================

-- ---------------------------------------------------------------------
-- 장비 모델 목록 (폼 드롭다운의 소스)
--   kind = 'C' : CBox   (IL, CC, SMC, Kaytus, HPE, Cisco ...)
--   kind = 'D' : DBox   (Ceres, Maverick ...)
--   kind = 'E' : EBox   (C/D 분리형이 아닌 단일형)
--   kind = 'S' : 스위치
--
--   목록에 없는 모델은 폼에서 '기타'를 고르고 직접 입력한다.
--   그 경우 cluster_hardware.model_id 는 NULL, model_name 에 입력값이 들어간다.
--   자주 쓰는 모델이 되면 여기에 INSERT 만 하면 드롭다운에 나타난다(코드 수정 불필요).
-- ---------------------------------------------------------------------
CREATE TABLE hardware_model (
  id          INT UNSIGNED NOT NULL AUTO_INCREMENT,
  kind        ENUM('C','D','E','S') NOT NULL,
  name        VARCHAR(50)  NOT NULL COMMENT '드롭다운에 보일 모델명',
  vendor      VARCHAR(50)           COMMENT '선택 입력',
  sort_order  SMALLINT UNSIGNED NOT NULL DEFAULT 100,
  is_active   TINYINT(1)   NOT NULL DEFAULT 1 COMMENT '0 이면 목록에서 숨김(기존 데이터는 유지)',
  remark      VARCHAR(200),
  created_at  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uk_model (kind, name),
  KEY idx_model_active (kind, is_active, sort_order)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

INSERT INTO hardware_model (kind, name, sort_order) VALUES
  ('C','IL',       10),
  ('C','CC',       20),
  ('C','SMC',      30),
  ('C','Kaytus',   40),
  ('C','HPE',      50),
  ('C','Cisco',    60),
  ('D','Ceres',    10),
  ('D','Maverick', 20);

-- 스위치 모델
-- 쓰지 않게 된 모델은 DELETE 하지 말고 is_active=0 으로 둔다.
-- 그래야 그 모델을 쓰던 과거 클러스터 기록이 깨지지 않는다.
INSERT INTO hardware_model (kind, name, vendor, sort_order) VALUES
  ('S','sn3700', 'nvidia', 10),
  ('S','sn2100', 'nvidia', 20);

-- =====================================================================
-- 2. 고객사 / 클러스터
-- =====================================================================

CREATE TABLE customer (
  id          BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  name        VARCHAR(100) NOT NULL COMMENT '고객사명 (예: LGCNS)',
  short_code  VARCHAR(30)           COMMENT '내부 약칭',
  industry    VARCHAR(50),
  status      ENUM('ACTIVE','INACTIVE') NOT NULL DEFAULT 'ACTIVE',
  remark      TEXT,
  created_at  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  updated_by  VARCHAR(50),
  PRIMARY KEY (id),
  UNIQUE KEY uk_customer_name (name)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ---------------------------------------------------------------------
-- 클러스터
--   model_type 이 'CD' 면 장비를 C / D 로 나눠 입력하고,
--   'E' 면 벤더명을 직접 입력한 E 항목만 들어간다.
--   폼은 이 값을 보고 어떤 드롭다운을 띄울지 정한다.
--
--   클러스터명은 중복될 수 있어(KT클라우드 가산/예천 동명) UNIQUE 를 걸지 않는다.
--   식별 기준은 PSNT.
-- ---------------------------------------------------------------------
CREATE TABLE cluster (
  id                 BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  customer_id        BIGINT UNSIGNED NOT NULL,

  name               VARCHAR(100) NOT NULL COMMENT 'Cluster Name (예: AILAB-NAS)',
  psnt               VARCHAR(50)           COMMENT '장비 식별 기준 (예: VA24442801)',
  purpose            VARCHAR(200)          COMMENT '용도 (예: SLM, 반도체 EDA 설계)',
  site               VARCHAR(200)          COMMENT '설치 장소',

  model_type         ENUM('CD','E') NOT NULL DEFAULT 'CD'
                     COMMENT 'CD = CBox+DBox 분리형, E = 단일형(벤더명 직접 입력)',

  cur_release        VARCHAR(30)           COMMENT 'SW Release (예: 5.3.5-sp5)',
  service_nic        ENUM('EXTERNAL','INTERNAL','MIXED'),
  mgmt_type          ENUM('OUTBAND','INBAND'),
  ipmi_type          ENUM('OUTBAND','INBAND','B2B'),

  guaranteed_drr     DECIMAL(6,3)          COMMENT '계약상 보장 DRR. 없으면 NULL',
  install_date       DATE,
  status             ENUM('PROD','POC','DEMO','TEMP','PLANNED','DECOMM') NOT NULL DEFAULT 'PROD',
  support_status     ENUM('DIRECT','PARTNER','NONE') NOT NULL DEFAULT 'DIRECT',
  remark             TEXT                  COMMENT '추가 정보',

  -- Confluence 발행 대상
  confluence_space   VARCHAR(20)  NOT NULL DEFAULT 'CPFDS',
  confluence_page_id VARCHAR(30)           COMMENT '발행 대상 페이지 ID. NULL 이면 첫 발행 시 생성',
  -- 제목은 기본적으로 '고객사 - 용도' 로 만든다. 같은 고객사에 용도가 같은
  -- 클러스터가 둘이면 제목이 겹쳐 두 번째 발행이 첫 번째 페이지를 덮어쓴다.
  -- 그런 경우 여기에 제목을 직접 적는다. UNIQUE 는 NULL 을 여러 개 허용하므로
  -- 자동 생성을 쓰는 클러스터는 제약을 받지 않는다(앱이 발행 전에 검사한다).
  wiki_title         VARCHAR(200)          COMMENT '위키 페이지 제목. 비우면 자동 생성',

  created_at         DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at         DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  updated_by         VARCHAR(50),
  PRIMARY KEY (id),
  UNIQUE KEY uk_cluster_psnt (psnt),
  UNIQUE KEY uk_cluster_wiki_title (wiki_title),
  KEY idx_cluster_customer (customer_id),
  KEY idx_cluster_name (name),
  CONSTRAINT fk_cluster_customer FOREIGN KEY (customer_id)
    REFERENCES customer(id) ON DELETE RESTRICT,
  CONSTRAINT ck_cluster_drr CHECK (guaranteed_drr IS NULL OR guaranteed_drr > 0)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- 프로토콜 / 서비스 타입 (다중 선택)
CREATE TABLE cluster_protocol (
  cluster_id BIGINT UNSIGNED NOT NULL,
  protocol   VARCHAR(20) NOT NULL COMMENT 'NFS, SMB, S3',
  PRIMARY KEY (cluster_id, protocol),
  CONSTRAINT fk_proto_cluster FOREIGN KEY (cluster_id)
    REFERENCES cluster(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE cluster_service_type (
  cluster_id   BIGINT UNSIGNED NOT NULL,
  service_type VARCHAR(20) NOT NULL COMMENT 'Ethernet, RoCE, IB',
  PRIMARY KEY (cluster_id, service_type),
  CONSTRAINT fk_svctype_cluster FOREIGN KEY (cluster_id)
    REFERENCES cluster(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- =====================================================================
-- 3. 장비 구성
-- =====================================================================

-- ---------------------------------------------------------------------
-- 한 행이 '모델 + 수량' 하나를 뜻한다. 폼에서 행을 추가/삭제한다.
--   C+D 예: (C, SMC, 3) / (D, Ceres, 1)
--   E   예: (E, 'Dell PowerEdge R760', 6)
--
--   model_id 가 있으면 드롭다운에서 고른 것, NULL 이면 '기타'로 직접 입력한 것.
--   model_name 은 어느 경우든 반드시 채운다(목록이 바뀌어도 기록이 남도록).
-- ---------------------------------------------------------------------
CREATE TABLE cluster_hardware (
  id          BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  cluster_id  BIGINT UNSIGNED NOT NULL,
  kind        ENUM('C','D','E','S') NOT NULL,
  model_id    INT UNSIGNED          COMMENT '드롭다운 선택 시 hardware_model.id. 기타면 NULL',
  model_name  VARCHAR(100) NOT NULL COMMENT '화면/위키 출력에 쓰는 이름',
  quantity    SMALLINT UNSIGNED NOT NULL DEFAULT 1,
  capacity_note VARCHAR(50)         COMMENT '예: 338TB, 5615',
  sort_order  SMALLINT UNSIGNED NOT NULL DEFAULT 100,
  remark      VARCHAR(200),
  created_at  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  updated_by  VARCHAR(50),
  PRIMARY KEY (id),
  KEY idx_hw_cluster (cluster_id, kind, sort_order),
  KEY idx_hw_model (model_name),
  CONSTRAINT fk_hw_cluster FOREIGN KEY (cluster_id)
    REFERENCES cluster(id) ON DELETE CASCADE,
  CONSTRAINT fk_hw_model FOREIGN KEY (model_id)
    REFERENCES hardware_model(id) ON DELETE SET NULL,
  CONSTRAINT ck_hw_qty CHECK (quantity > 0)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- =====================================================================
-- 4. 펌웨어
-- =====================================================================

CREATE TABLE cluster_firmware (
  id         BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  cluster_id BIGINT UNSIGNED NOT NULL,
  component  ENUM('OS','SCM','SSD','SWITCH','BMC','NIC','OTHER') NOT NULL,
  version    VARCHAR(60) NOT NULL COMMENT '예: 12.14.41-2299875',
  checked_at DATE        NOT NULL COMMENT '확인 시점. 이력으로 쌓인다',
  remark     VARCHAR(200),
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  updated_by VARCHAR(50),
  PRIMARY KEY (id),
  UNIQUE KEY uk_fw (cluster_id, component, checked_at),
  KEY idx_fw_cluster (cluster_id, component),
  CONSTRAINT fk_fw_cluster FOREIGN KEY (cluster_id)
    REFERENCES cluster(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- =====================================================================
-- 5. 네트워크
-- =====================================================================

-- ---------------------------------------------------------------------
--   role       : 검색용. '이 대역 누가 쓰나' 같은 역조회에 쓴다.
--   wiki_label : 출력용. 위키 표의 행 이름을 그대로 재현한다.
--                (예: 'Service IP 1', 'Service IP 2')
--   sort_order : 위키 표의 행 순서
--   주소는 VARBINARY(16)+INET6_ATON 으로 저장해 범위 검색이 되게 한다.
-- ---------------------------------------------------------------------
CREATE TABLE cluster_network (
  id          BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  cluster_id  BIGINT UNSIGNED NOT NULL,
  role        ENUM('VMS','CNODE','DNODE','SWITCH','MGMT_GW','SERVICE',
                   'SERVICE_GW','DNS','VAST_DNS','NTP','TEST','BACKUP','OTHER') NOT NULL,
  fabric      ENUM('MGMT','IPMI','SERVICE_ETH','SERVICE_IB','SERVICE_ROCE','EXTERNAL') NOT NULL,

  addr_start  VARBINARY(16) NOT NULL,
  addr_end    VARBINARY(16)         COMMENT '범위가 아니면 NULL',
  prefix_len  TINYINT UNSIGNED      COMMENT '예: 26, 27, 24',
  vlan        SMALLINT UNSIGNED,
  mtu         SMALLINT UNSIGNED     COMMENT 'ETH 9214 / IB 2044',

  wiki_label  VARCHAR(50)  NOT NULL COMMENT "위키 표 행 이름 (예: 'Cnode mgmt ip', 'Service IP 1')",
  sort_order  SMALLINT UNSIGNED NOT NULL DEFAULT 100,
  remark      VARCHAR(200),

  addr_start_txt VARCHAR(45) AS (INET6_NTOA(addr_start)) VIRTUAL,
  addr_end_txt   VARCHAR(45) AS (INET6_NTOA(addr_end))   VIRTUAL,

  created_at  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  updated_by  VARCHAR(50),
  PRIMARY KEY (id),
  KEY idx_net_cluster (cluster_id, sort_order),
  KEY idx_net_addr (addr_start, addr_end),
  KEY idx_net_role (role, fabric),
  CONSTRAINT fk_net_cluster FOREIGN KEY (cluster_id)
    REFERENCES cluster(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- =====================================================================
-- 6. 라이선스
-- =====================================================================

-- status 컬럼을 두지 않는다. 날짜에서 파생되는 값이라
-- 저장하면 배치가 한 번 밀릴 때 실제와 어긋난다. v_license_status 뷰에서 계산.
CREATE TABLE license (
  id           BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  cluster_id   BIGINT UNSIGNED NOT NULL,
  license_type VARCHAR(40) NOT NULL COMMENT '예: U100, U675(Box)',
  term_months  SMALLINT UNSIGNED    COMMENT '개월 단위로 통일 (5년=60)',
  start_date   DATE,
  end_date     DATE,
  capacity_tb  DECIMAL(12,2),
  contract_no  VARCHAR(50)          COMMENT '예: LS-001645',
  purpose      VARCHAR(200)         COMMENT '증설 사유 등',
  sort_order   SMALLINT UNSIGNED NOT NULL DEFAULT 100,
  remark       VARCHAR(200),
  created_at   DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at   DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  updated_by   VARCHAR(50),
  PRIMARY KEY (id),
  KEY idx_license_cluster (cluster_id, sort_order),
  KEY idx_license_end (end_date),
  CONSTRAINT fk_license_cluster FOREIGN KEY (cluster_id)
    REFERENCES cluster(id) ON DELETE CASCADE,
  CONSTRAINT ck_license_period CHECK (
    end_date IS NULL OR start_date IS NULL OR end_date >= start_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- =====================================================================
-- 7. 용량 스냅샷
-- =====================================================================

-- VMS 는 Total/Used/Free 만 보고한다. System 은 잔차로 도출한다.
--   System = Total - Used - Free
--   예) 248 - 89.178 - 156.287 = 2.535
CREATE TABLE capacity_snapshot (
  id                BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  cluster_id        BIGINT UNSIGNED NOT NULL,
  measured_at       DATE NOT NULL COMMENT 'Last Update',
  release_at_time   VARCHAR(30)   COMMENT '측정 시점의 SW Release',

  usable_total_tb   DECIMAL(12,3),
  usable_used_tb    DECIMAL(12,3),
  usable_free_tb    DECIMAL(12,3),
  usable_system_tb  DECIMAL(12,3) AS
                    (usable_total_tb - usable_used_tb - usable_free_tb) STORED
                    COMMENT 'Total - Used - Free',

  logical_total_tb  DECIMAL(12,3),
  logical_used_tb   DECIMAL(12,3),
  logical_free_tb   DECIMAL(12,3),

  drr               DECIMAL(6,3) COMMENT '1.9:1 → 1.900',
  source            ENUM('MANUAL','VMS_API') NOT NULL DEFAULT 'MANUAL',
  remark            VARCHAR(200),
  created_at        DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_by        VARCHAR(50),
  PRIMARY KEY (id),
  UNIQUE KEY uk_snapshot (cluster_id, measured_at),
  KEY idx_snapshot_date (measured_at),
  CONSTRAINT fk_snapshot_cluster FOREIGN KEY (cluster_id)
    REFERENCES cluster(id) ON DELETE CASCADE,
  CONSTRAINT ck_snapshot_drr CHECK (drr IS NULL OR drr > 0)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- =====================================================================
-- 8. 발행 이력
-- =====================================================================

-- Confluence API 는 페이지 버전 충돌 시 실패한다.
-- 조용히 실패하면 위키가 옛 값을 계속 보여주므로 결과를 남긴다.
CREATE TABLE publish_log (
  id           BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  cluster_id   BIGINT UNSIGNED NOT NULL,
  published_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  page_id      VARCHAR(30),
  page_version INT UNSIGNED,
  status       ENUM('OK','FAILED') NOT NULL,
  message      TEXT COMMENT '실패 사유',
  published_by VARCHAR(50),
  PRIMARY KEY (id),
  KEY idx_pub_cluster (cluster_id, published_at DESC),
  CONSTRAINT fk_pub_cluster FOREIGN KEY (cluster_id)
    REFERENCES cluster(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- =====================================================================
-- 9. 변경 이력
-- =====================================================================

-- 위키의 버전 기록을 대신한다. 앱에서 기록한다(트리거 아님).
--   이유: 트리거는 changed_by 를 알 수 없다. 세션 변수로 넘길 수는 있으나
--         커넥션 풀 환경에서 누락/오염 위험이 있어 앱에서 명시적으로 남긴다.
--   인증이 없으므로 changed_by 는 폼에서 입력받은 이름이다. 감사 추적은 아니다.
CREATE TABLE change_log (
  id         BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  cluster_id BIGINT UNSIGNED NOT NULL,
  changed_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  changed_by VARCHAR(50),
  action     ENUM('CREATE','UPDATE','DELETE') NOT NULL,
  table_name VARCHAR(40) NOT NULL COMMENT '예: cluster, cluster_network',
  row_id     BIGINT UNSIGNED      COMMENT '해당 행 id',
  field_name VARCHAR(40)          COMMENT 'UPDATE 일 때 바뀐 컬럼',
  old_value  TEXT,
  new_value  TEXT,
  PRIMARY KEY (id),
  KEY idx_chg_cluster (cluster_id, changed_at DESC),
  KEY idx_chg_table (table_name, row_id),
  CONSTRAINT fk_chg_cluster FOREIGN KEY (cluster_id)
    REFERENCES cluster(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- =====================================================================
-- 뷰
-- =====================================================================

-- 장비 수량 합계. 위키의 'Cluster size' 문자열을 여기서 만든다.
--   model_type = 'CD' → '3*1'      (C 합계 * D 합계)
--   model_type = 'E'  → 'Ebox 12'  (E 합계)
--   스위치(kind='S')는 수량에 포함하지 않는다. 별도 집계만 한다.
CREATE OR REPLACE VIEW v_cluster_size AS
SELECT
  c.id AS cluster_id,
  c.model_type,
  COALESCE(SUM(CASE WHEN h.kind='C' THEN h.quantity END),0) AS c_count,
  COALESCE(SUM(CASE WHEN h.kind='D' THEN h.quantity END),0) AS d_count,
  COALESCE(SUM(CASE WHEN h.kind='E' THEN h.quantity END),0) AS e_count,
  COALESCE(SUM(CASE WHEN h.kind='S' THEN h.quantity END),0) AS s_count,
  -- CONCAT 으로 숫자를 문자열로 만들면 결과 문자셋이 접속 설정을 따라간다.
  -- (latin1 로 붙으면 COLLATE utf8mb4_* 와 충돌해 뷰 생성 자체가 실패한다)
  -- 접속 설정에 기대지 않도록 CONVERT 로 못박는다.
  CONVERT(
    CASE c.model_type
      WHEN 'CD' THEN CONCAT(
        COALESCE(SUM(CASE WHEN h.kind='C' THEN h.quantity END),0), '*',
        COALESCE(SUM(CASE WHEN h.kind='D' THEN h.quantity END),0))
      ELSE CONCAT('Ebox ', COALESCE(SUM(CASE WHEN h.kind='E' THEN h.quantity END),0))
    END USING utf8mb4) COLLATE utf8mb4_unicode_ci AS size_label
FROM cluster c
LEFT JOIN cluster_hardware h ON h.cluster_id = c.id
GROUP BY c.id, c.model_type;

-- 장비 상세. 위키의 'Cluster detail (Model/qty)' 를 만든다. 예: SMC(3), Ceres(1)
--   용량(capacity_note)은 컬럼으로만 보관하고 이 표기에는 넣지 않는다.
--   위키 한 줄에 모델과 용량이 섞이면 읽기도 파악도 어려워진다.
--   용량이 필요한 화면에서는 cluster_hardware 를 직접 조회한다.
CREATE OR REPLACE VIEW v_cluster_hardware_label AS
SELECT
  h.cluster_id,
  CONVERT(GROUP_CONCAT(CONCAT(h.model_name,'(',h.quantity,')')
               ORDER BY FIELD(h.kind,'C','D','E','S'), h.sort_order, h.id
               SEPARATOR ', ') USING utf8mb4) AS detail_label
FROM cluster_hardware h
GROUP BY h.cluster_id;

-- 목록/상세 화면의 기본 정보
CREATE OR REPLACE VIEW v_cluster_basic AS
SELECT
  c.id              AS cluster_id,
  cu.name           AS customer_name,
  c.name            AS cluster_name,
  c.psnt,
  c.purpose,
  c.status,
  c.model_type,
  z.size_label      AS cluster_size,
  hl.detail_label   AS cluster_detail,
  c.cur_release     AS vast_sw_release,
  s.measured_at     AS last_update,
  s.usable_total_tb, s.usable_used_tb, s.usable_free_tb, s.usable_system_tb,
  s.logical_total_tb, s.logical_used_tb, s.logical_free_tb,
  s.drr, c.guaranteed_drr,
  CONVERT(
    CASE WHEN s.drr IS NULL OR c.guaranteed_drr IS NULL THEN NULL
         WHEN s.drr < c.guaranteed_drr THEN 'BELOW' ELSE 'OK'
    END USING utf8mb4) COLLATE utf8mb4_unicode_ci AS drr_status,
  CASE WHEN s.usable_total_tb IS NULL OR s.usable_used_tb IS NULL THEN NULL
       ELSE ROUND(s.usable_used_tb / s.usable_total_tb * 100, 1)
  END AS used_pct,
  DATEDIFF(CURDATE(), s.measured_at) AS days_since_update,
  c.confluence_page_id,
  p.published_at    AS last_published_at,
  p.status          AS last_publish_status
FROM cluster c
JOIN customer cu              ON cu.id = c.customer_id
LEFT JOIN v_cluster_size z    ON z.cluster_id = c.id
LEFT JOIN v_cluster_hardware_label hl ON hl.cluster_id = c.id
LEFT JOIN capacity_snapshot s ON s.cluster_id = c.id
 AND s.measured_at = (SELECT MAX(s2.measured_at) FROM capacity_snapshot s2
                       WHERE s2.cluster_id = c.id)
LEFT JOIN publish_log p       ON p.cluster_id = c.id
 AND p.id = (SELECT MAX(p2.id) FROM publish_log p2 WHERE p2.cluster_id = c.id);

-- 라이선스 상태 (날짜에서 파생)
CREATE OR REPLACE VIEW v_license_status AS
SELECT
  l.id, l.cluster_id, cu.name AS customer_name, c.name AS cluster_name, c.psnt,
  l.license_type, l.term_months, l.start_date, l.end_date,
  DATEDIFF(l.end_date, CURDATE()) AS days_left,
  CONVERT(
    CASE
      WHEN l.end_date   IS NULL      THEN 'UNKNOWN'
      WHEN l.end_date    < CURDATE() THEN 'EXPIRED'
      WHEN l.start_date  > CURDATE() THEN 'PENDING'
      WHEN DATEDIFF(l.end_date, CURDATE()) <= 90 THEN 'EXPIRING'
      ELSE 'ACTIVE'
    END USING utf8mb4) COLLATE utf8mb4_unicode_ci AS license_status
FROM license l
JOIN cluster  c  ON c.id  = l.cluster_id
JOIN customer cu ON cu.id = c.customer_id;

-- 펌웨어 최신값
CREATE OR REPLACE VIEW v_firmware_current AS
SELECT f.cluster_id, f.component, f.version, f.checked_at
FROM cluster_firmware f
WHERE f.checked_at = (SELECT MAX(f2.checked_at) FROM cluster_firmware f2
                       WHERE f2.cluster_id = f.cluster_id AND f2.component = f.component);

-- 네트워크. 위키 표 행 단위로 묶는다.
CREATE OR REPLACE VIEW v_network_label AS
SELECT
  n.cluster_id,
  n.wiki_label,
  n.role,
  n.fabric,
  MIN(n.sort_order) AS sort_order,
  CONVERT(GROUP_CONCAT(
    CONCAT(n.addr_start_txt,
           IF(n.addr_end IS NULL,'',CONCAT('-',SUBSTRING_INDEX(n.addr_end_txt,'.',-1))),
           IF(n.prefix_len IS NULL,'',CONCAT('/',n.prefix_len)),
           IF(n.vlan IS NULL,'',CONCAT(', vlan ',n.vlan)),
           IF(n.mtu  IS NULL,'',CONCAT(', MTU ',n.mtu)))
    ORDER BY n.addr_start SEPARATOR ', ') USING utf8mb4) AS addresses
FROM cluster_network n
GROUP BY n.cluster_id, n.wiki_label, n.role, n.fabric;

-- 갱신 지연
CREATE OR REPLACE VIEW v_stale_clusters AS
SELECT customer_name, cluster_name, psnt, last_update, days_since_update
FROM v_cluster_basic
WHERE last_update IS NULL OR days_since_update > 180
ORDER BY days_since_update DESC;

-- 발행 필요 (데이터가 바뀐 뒤 발행하지 않은 클러스터)
--
--   cluster.updated_at 만 보면 안 된다. 네트워크/라이선스/펌웨어/용량을 고쳐도
--   cluster 행은 건드리지 않아 updated_at 이 그대로다. change_log 는 자녀
--   테이블 저장 때마다 남으므로, 그 마지막 시각과 updated_at 중 늦은 쪽을 쓴다.
--   발행은 change_log 를 쓰지 않아 '발행 -> 또 발행 필요' 로 돌지 않는다.
--
--   DECOMM 은 제외한다. 철거한 클러스터가 계속 남으면 목록이 쓸모없어진다.
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
