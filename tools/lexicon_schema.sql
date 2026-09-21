-- ============================================================================
-- 纠错词典表建表脚本（t_nc_*）
--
-- 环境：MySQL 8.0.26 / InnoDB，库 agent_data
--
-- 【命名规范 —— 与 agent_data 现有 6 张表统一】
--   创建/更新时间  create_time / update_time
--   删除标记       deleted_flag TINYINT 0正常1删除
--   主键           id BIGINT AUTO_INCREMENT
--   表名           t_ 前缀 + 小写下划线
--
-- 【字符集】显式 utf8mb4_general_ci
--   实例默认是 utf8mb4_0900_ai_ci，不显式指定会继承 0900，
--   与 dlj_data / agent_data 其它表跨库 JOIN 时直接报 1267
--   （Illegal mix of collations）。
--
-- 【列名】全部用英文 + 反引号包裹。原因有两个：
--   1. 反引号可彻底避开保留字（`right` 就是 MySQL 保留字，所以正确写法列叫 `right_word`）
--   2. 中文列名在 ORM / 导出 / 跨工具流转时容易出编码问题
--   代码侧由 lex_source.py 做一次「英文列名 -> 中文键」的显式映射。
--
-- 【列宽】按实测最大长度取，并留余量。注意 catalog.name 最长 66 字符
--   （业务库里的脏数据，名称被重复拼接了两遍），不能用 VARCHAR(64)。
--
-- 【重要】这 6 张表不要登记进 ai_data.ai_table_metadata，
--   否则模型会把这 2 万条台区名当成业务数据去写 SQL。
--
-- 本脚本可重复执行（CREATE TABLE IF NOT EXISTS），不会删数据。
-- ============================================================================

USE agent_data;


-- ----------------------------------------------------------------------------
-- 1. 错字表 t_nc_typo  <- data/typo.csv（105 行）
--    代码用法：按「错误写法」做整句字符串替换，长词优先
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS t_nc_typo (
  `id`           BIGINT       NOT NULL AUTO_INCREMENT            COMMENT '主键',
  `org`          VARCHAR(8)   NOT NULL DEFAULT ''                COMMENT '适用组织：电力/政务',
  `wrong`        VARCHAR(32)  NOT NULL                           COMMENT '错误写法',
  `right_word`   VARCHAR(32)  NOT NULL                           COMMENT '正确写法（right 是保留字，故加 _word）',
  `freq_level`   VARCHAR(8)   NOT NULL DEFAULT ''                COMMENT '频率等级：高频/中频',
  `reason`       VARCHAR(8)   NOT NULL DEFAULT ''                COMMENT '错因类型：同音/形近',
  `sample`       VARCHAR(64)  NOT NULL DEFAULT ''                COMMENT '纠正示例',
  `deleted_flag` TINYINT      NOT NULL DEFAULT 0                 COMMENT '0正常 1删除',
  `create_time`  DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  `update_time`  DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_wrong_org` (`wrong`, `org`),
  KEY `idx_update` (`update_time`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci COMMENT='纠错词典-错字表';


-- ----------------------------------------------------------------------------
-- 2. 业务术语表 t_nc_term  <- data/terms.csv（75 行）
--    代码用法：术语保护 —— 这些词出现在句子里时不做任何替换
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS t_nc_term (
  `id`           BIGINT       NOT NULL AUTO_INCREMENT            COMMENT '主键',
  `term`         VARCHAR(32)  NOT NULL                           COMMENT '术语',
  `definition`   VARCHAR(128) NOT NULL DEFAULT ''                COMMENT '定义',
  `alias`        VARCHAR(64)  NOT NULL DEFAULT ''                COMMENT '常见表达',
  `org`          VARCHAR(8)   NOT NULL DEFAULT ''                COMMENT '适用组织：电力/政务',
  `deleted_flag` TINYINT      NOT NULL DEFAULT 0                 COMMENT '0正常 1删除',
  `create_time`  DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  `update_time`  DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_term` (`term`),
  KEY `idx_update` (`update_time`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci COMMENT='纠错词典-业务术语表';


-- ----------------------------------------------------------------------------
-- 3. 同音歧义表 t_nc_ambiguous  <- data/ambiguous.csv（81 行）
--    代码用法：同拼音码的两个名字无法区分时，给「反问确认」提示
--    注意：一个拼音码在逻辑上可以对应多组，代码用 list 收集，故不加唯一键
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS t_nc_ambiguous (
  `id`           BIGINT       NOT NULL AUTO_INCREMENT            COMMENT '主键',
  `py_code`      VARCHAR(64)  NOT NULL                           COMMENT '拼音码',
  `name_a`       VARCHAR(32)  NOT NULL                           COMMENT '名称A',
  `kind_a`       VARCHAR(8)   NOT NULL DEFAULT ''                COMMENT '类别A',
  `name_b`       VARCHAR(32)  NOT NULL                           COMMENT '名称B',
  `kind_b`       VARCHAR(8)   NOT NULL DEFAULT ''                COMMENT '类别B',
  `action`       VARCHAR(16)  NOT NULL DEFAULT ''                COMMENT '处理方式：反问确认',
  `deleted_flag` TINYINT      NOT NULL DEFAULT 0                 COMMENT '0正常 1删除',
  `create_time`  DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  `update_time`  DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
  PRIMARY KEY (`id`),
  KEY `idx_py_code` (`py_code`),
  KEY `idx_update` (`update_time`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci COMMENT='纠错词典-同音歧义表';


-- ----------------------------------------------------------------------------
-- 4. 业务标准词表 t_nc_wordlist  <- data/wordlist.csv（197 行）
--    代码用法：词表层的拼音索引（拼音码用 encode_light 现算，不读本表 py_full）
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS t_nc_wordlist (
  `id`           BIGINT       NOT NULL AUTO_INCREMENT            COMMENT '主键',
  `word`         VARCHAR(32)  NOT NULL                           COMMENT '标准词',
  `domain`       VARCHAR(8)   NOT NULL DEFAULT ''                COMMENT '领域：电力/政务',
  `src`          VARCHAR(32)  NOT NULL DEFAULT ''                COMMENT '来源',
  `py_full`      VARCHAR(64)  NOT NULL DEFAULT ''                COMMENT '拼音码（存档用，运行时不用）',
  `initials`     VARCHAR(32)  NOT NULL DEFAULT ''                COMMENT '首字母',
  `deleted_flag` TINYINT      NOT NULL DEFAULT 0                 COMMENT '0正常 1删除',
  `create_time`  DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  `update_time`  DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_word` (`word`),
  KEY `idx_update` (`update_time`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci COMMENT='纠错词典-业务标准词表';


-- ----------------------------------------------------------------------------
-- 5. 标准名称目录 t_nc_catalog
--    <- data/catalog.csv（20183 行）+ data/area_core.csv（20131 行，按「名称」合并进来）
--
--    ★ 为什么合并：实测 area_core 的 20131 个名称 100% 都在 catalog 里，差集为 0，
--      它只是「台区的核心码」这一列。合并后少一次 1 MB 的查询往返。
--      catalog 里名为「9」的那条台区在 area_core 中没有核心码 -> core_code 留空，
--      代码里 `if not code: continue` 会跳过它，行为与原来一致。
--
--    ★ py_full / py_strict / initials / core_code 是【离线编译产物】：
--      两万条现算拼音码要 4 秒，冷启动扛不住，所以必须存进库、不能启动时现算。
--      这四列【禁止手工修改】，改了会和 pinyin_table 的口径分裂，
--      导致同音匹配静默失效。要改只能重跑导入脚本。
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS t_nc_catalog (
  `id`           BIGINT       NOT NULL AUTO_INCREMENT            COMMENT '主键',
  `name`         VARCHAR(128) NOT NULL                           COMMENT '标准名（已清洗）；实测最长 66 字符',
  `kind`         VARCHAR(16)  NOT NULL DEFAULT ''                COMMENT '类别：台区/供电所/供电公司/供电服务站',
  `owner`        VARCHAR(32)  NOT NULL DEFAULT ''                COMMENT '所属',
  `py_full`      VARCHAR(255) NOT NULL DEFAULT ''                COMMENT '拼音码【编译产物-勿手改】',
  `py_strict`    VARCHAR(255) NOT NULL DEFAULT ''                COMMENT '拼音码_严【编译产物-勿手改】',
  `initials`     VARCHAR(128) NOT NULL DEFAULT ''                COMMENT '首字母【编译产物-勿手改】',
  `core_code`    VARCHAR(255) NOT NULL DEFAULT ''                COMMENT '台区核心码【编译产物-勿手改】；非台区或无名核心为空',
  `source_id`    VARCHAR(32)  NOT NULL DEFAULT ''                COMMENT '来源ID（t_power_area.id，仅溯源用，不参与查询）',
  `deleted_flag` TINYINT      NOT NULL DEFAULT 0                 COMMENT '0正常 1删除',
  `create_time`  DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  `update_time`  DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_name` (`name`),
  KEY `idx_kind` (`kind`),
  KEY `idx_core_code` (`core_code`(64)),
  KEY `idx_update` (`update_time`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci COMMENT='纠错词典-标准名称目录（含台区核心码）';


-- ----------------------------------------------------------------------------
-- 6. 拼音表 t_nc_pinyin  <- data/pinyin_table.csv（20922 行）
--    汉字 -> (声母, 韵母)。这张表按「库内业务名称的语境」投票定音，
--    所以和 t_nc_catalog.py_full 的口径一致 —— 多音字不会因为「单字默认音」而对不上。
--    ★ 它是编码算法的参数，改了必须重跑导入脚本重算 py_full / core_code。
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS t_nc_pinyin (
  `id`           BIGINT      NOT NULL AUTO_INCREMENT             COMMENT '主键',
  `hanzi`        VARCHAR(4)  NOT NULL                            COMMENT '汉字（单字）',
  `initial`      VARCHAR(8)  NOT NULL DEFAULT ''                 COMMENT '声母',
  `final`        VARCHAR(8)  NOT NULL DEFAULT ''                 COMMENT '韵母',
  `deleted_flag` TINYINT     NOT NULL DEFAULT 0                  COMMENT '0正常 1删除',
  `create_time`  DATETIME    NOT NULL DEFAULT CURRENT_TIMESTAMP  COMMENT '创建时间',
  `update_time`  DATETIME    NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_hanzi` (`hanzi`),
  KEY `idx_update` (`update_time`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci COMMENT='纠错词典-拼音表（汉字->声母韵母）';


-- ============================================================================
-- 说明：data/rules.csv（34 行）【不导入】
--   它在 name_corrector.py 第 39 行只定义了常量 RULES = "rules.csv"，
--   全文再没有任何地方读取它（已 grep 确认）。读音规则实际硬编码在
--   INITIAL_FUZZY / FINAL_FUZZY 两个 dict 里。
--   建议直接删掉该文件 —— 读音规则改错一条会让全库同音匹配静默失效，
--   做成「可配置」的收益远小于风险。
-- ============================================================================
