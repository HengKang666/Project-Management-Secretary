-- ============================================================================
-- 知识库文件台账 + 按页富文本（P1，2026-09-22）
--
-- 库：agent_data（与对话记录库同一个）
-- 用法：mysql -h <host> -u <user> -p agent_data < kb_file_schema.sql
-- 或：  python tools/apply_kb_file_schema.py
--
-- 幂等：表用 CREATE TABLE IF NOT EXISTS，加列用 information_schema 判断，
--       可以重复执行，不会报错、不会清数据。
--
-- 为什么要有这两张表（而不是只靠百炼云端）：
--   ① 云端只有「文件元信息」，没有原文、也不支持按页取文本 ⇒ 预览做不了；
--   ② 上传到云端的文件，我们手里只有一次 bytes 的机会，不落盘就永久丢失；
--   ③ 将来换成本地自建知识库时，「台账 + 按页文本」这层可以原地复用。
-- ============================================================================

-- ---------------------------------------------------------------------------
-- 1. 文件台账：一行一个上传过的文件
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS t_kb_file (
  id             BIGINT        NOT NULL AUTO_INCREMENT       COMMENT '主键',
  file_id        VARCHAR(64)   NOT NULL                      COMMENT '云端文件ID（百炼 fileId），对账主键',
  index_id       VARCHAR(32)   NOT NULL                      COMMENT '所属知识库ID',
  file_name      VARCHAR(255)  NOT NULL                      COMMENT '原始文件名（含后缀）',
  file_ext       VARCHAR(16)             DEFAULT NULL        COMMENT '小写后缀，如 pdf / docx',
  size_bytes     BIGINT                  DEFAULT NULL        COMMENT '字节数',
  md5            CHAR(32)                DEFAULT NULL        COMMENT '内容MD5，用于秒传/去重',
  storage_path   VARCHAR(500)            DEFAULT NULL        COMMENT '原文件磁盘路径；控制台直接传的为 NULL',
  origin         VARCHAR(16)   NOT NULL  DEFAULT 'api'       COMMENT '来源：api=本服务上传 / console=百炼控制台传的',
  page_count     INT           NOT NULL  DEFAULT 0           COMMENT '抽取出的页数（0=还没抽）',
  text_chars     INT           NOT NULL  DEFAULT 0           COMMENT '抽出的正文总字符数',
  text_format    VARCHAR(16)   NOT NULL  DEFAULT 'html'      COMMENT '文本格式：html（富文本，前端直接渲染）',
  extract_status VARCHAR(16)   NOT NULL  DEFAULT 'pending'   COMMENT 'pending/extracting/done/failed/unsupported',
  extract_error  VARCHAR(500)            DEFAULT NULL        COMMENT '抽取失败原因（给人看的）',
  extract_ms     INT                     DEFAULT NULL        COMMENT '抽取耗时（毫秒）',
  uploader       VARCHAR(64)             DEFAULT NULL        COMMENT '上传人（业务用户ID）',
  upload_time    DATETIME      NOT NULL  DEFAULT CURRENT_TIMESTAMP COMMENT '上传时间',
  update_time    DATETIME      NOT NULL  DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
  deleted_flag   TINYINT       NOT NULL  DEFAULT 0           COMMENT '删除标记：0正常 1删除',
  PRIMARY KEY (id),
  UNIQUE KEY uk_index_file (index_id, file_id),
  KEY idx_md5 (md5),
  KEY idx_status (extract_status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci COMMENT='知识库文件台账';


-- ---------------------------------------------------------------------------
-- 2. 按页富文本：一行一页（"天然分页"落在这里）
--    PDF 按页 / xlsx 按 sheet / pptx 按 slide / docx·txt 按字数切块
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS t_kb_file_page (
  id         BIGINT     NOT NULL AUTO_INCREMENT COMMENT '主键',
  file_id    VARCHAR(64) NOT NULL               COMMENT '关联 t_kb_file.file_id',
  page_no    INT         NOT NULL               COMMENT '页码，从 1 开始',
  page_label VARCHAR(64)          DEFAULT NULL  COMMENT '展示用标签：第 3 页 / Sheet1 / Slide 2 / 第 2 节',
  content    MEDIUMTEXT                         COMMENT '正文，HTML 富文本（已做 XSS 白名单清洗）',
  char_count INT         NOT NULL DEFAULT 0     COMMENT '本页字符数（按纯文本计）',
  create_time DATETIME   NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uk_file_page (file_id, page_no),
  KEY idx_file (file_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci COMMENT='知识库文件按页富文本';


-- ---------------------------------------------------------------------------
-- 3. 会话摘要三列（L2 会话记忆，P2 才会用到；先建好列，不影响现有逻辑）
-- ---------------------------------------------------------------------------
SET @db := DATABASE();

SET @sql := (SELECT IF(COUNT(*) = 0,
  'ALTER TABLE t_chat_session ADD COLUMN summary MEDIUMTEXT NULL COMMENT ''会话摘要（L2）''',
  'SELECT ''summary 列已存在，跳过'' ')
  FROM information_schema.COLUMNS
  WHERE TABLE_SCHEMA = @db AND TABLE_NAME = 't_chat_session' AND COLUMN_NAME = 'summary');
PREPARE st FROM @sql; EXECUTE st; DEALLOCATE PREPARE st;

SET @sql := (SELECT IF(COUNT(*) = 0,
  'ALTER TABLE t_chat_session ADD COLUMN summary_upto_seq INT NOT NULL DEFAULT 0 COMMENT ''摘要已覆盖到第几条消息''',
  'SELECT ''summary_upto_seq 列已存在，跳过'' ')
  FROM information_schema.COLUMNS
  WHERE TABLE_SCHEMA = @db AND TABLE_NAME = 't_chat_session' AND COLUMN_NAME = 'summary_upto_seq');
PREPARE st FROM @sql; EXECUTE st; DEALLOCATE PREPARE st;

SET @sql := (SELECT IF(COUNT(*) = 0,
  'ALTER TABLE t_chat_session ADD COLUMN summary_time DATETIME NULL COMMENT ''摘要生成时间''',
  'SELECT ''summary_time 列已存在，跳过'' ')
  FROM information_schema.COLUMNS
  WHERE TABLE_SCHEMA = @db AND TABLE_NAME = 't_chat_session' AND COLUMN_NAME = 'summary_time');
PREPARE st FROM @sql; EXECUTE st; DEALLOCATE PREPARE st;


-- ---------------------------------------------------------------------------
-- 4. 异步上传任务表（4.2 新增）
--
-- 为什么单独一张表：台账 t_kb_file 的主键是 (index_id, file_id)，而 file_id 要等
-- **云端上传成功后**才有 —— 异步上传恰恰是"还没传完就先返回"，那会儿还没法写台账。
-- 而且"上传任务"（排队/上传中/失败）与"文件能不能预览"是两个维度，混一张表会别扭。
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS t_kb_upload_task (
  id             BIGINT       NOT NULL AUTO_INCREMENT    COMMENT '主键',
  task_id        VARCHAR(64)  NOT NULL                   COMMENT '任务号（给前端轮询用）',
  index_id       VARCHAR(32)  NOT NULL                   COMMENT '目标知识库',
  file_name      VARCHAR(255) NOT NULL                   COMMENT '原始文件名',
  size_bytes     BIGINT                DEFAULT NULL      COMMENT '字节数',
  md5            CHAR(32)              DEFAULT NULL      COMMENT '内容MD5',
  storage_path   VARCHAR(500)          DEFAULT NULL      COMMENT '已落的本地原文路径（失败时会删掉）',
  uploader       VARCHAR(64)           DEFAULT NULL      COMMENT '上传人（业务用户ID）',
  status         VARCHAR(16)  NOT NULL DEFAULT 'pending' COMMENT 'pending排队 / uploading上传中 / done成功 / failed失败',
  file_id        VARCHAR(64)           DEFAULT NULL      COMMENT '云端文件ID（成功后回填，前端拿它去预览）',
  error          VARCHAR(500)          DEFAULT NULL      COMMENT '失败原因（给人看的）',
  wait_parse     TINYINT      NOT NULL DEFAULT 1         COMMENT '原样记录本次上传参数',
  submit_index   TINYINT      NOT NULL DEFAULT 1         COMMENT '原样记录本次上传参数',
  skip_if_exists TINYINT      NOT NULL DEFAULT 1         COMMENT '原样记录本次上传参数',
  elapsed_ms     INT                   DEFAULT NULL      COMMENT '上传耗时（毫秒）',
  create_time    DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  update_time    DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
  PRIMARY KEY (id),
  UNIQUE KEY uk_task (task_id),
  KEY idx_status (status),
  KEY idx_index_time (index_id, create_time)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci COMMENT='知识库异步上传任务';


-- ---------------------------------------------------------------------------
-- 5. 建完自检
-- ---------------------------------------------------------------------------
SELECT TABLE_NAME AS tbl, TABLE_COLLATION AS coll, TABLE_COMMENT
FROM information_schema.TABLES
WHERE TABLE_SCHEMA = @db
  AND TABLE_NAME IN ('t_kb_file', 't_kb_file_page', 't_kb_upload_task');

SELECT COLUMN_NAME FROM information_schema.COLUMNS
WHERE TABLE_SCHEMA = @db AND TABLE_NAME = 't_chat_session'
  AND COLUMN_NAME IN ('summary', 'summary_upto_seq', 'summary_time')
ORDER BY COLUMN_NAME;
