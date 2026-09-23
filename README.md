# 项目管理秘书（AI 问答后端）

输入一条**已经补过时间与地点的问题**，模型自己决定查哪张表、怎么算，取数后作答，全程留痕（可审计）。
本地代码里**没有一条业务规则**：口径与表知识全部来自线上维护的业务字典和知识库。

---

## 一、当前状态（2026-09-23）

| 能力 | 状态 |
|---|---|
| 问题补全（按知识库规则库） | 已生效，补全耗时约 2–3 s |
| 字面纠错 + 名称归一 + 时间地点补全 | 已生效（本服务自己做，不再依赖上游） |
| 六项指标（售电量/线损率/意见工单/故障报修/停电时长/跳闸） | 全部走统一快照表，本期/同比/环比都能答 |
| 预算、工单分档、台区线路数 | 能答，但口径未定，数字会随问法变（见第七节） |
| 超范围问题（酒店/GDP/三公） | 如实拒答，并落缺口清单 |
| 多轮对话 | `session_id` 回传即续聊；长会话靠 **L2 会话摘要**记住更早的轮次 |
| 历史对话接口 | **默认全部返回**（`total` 为真实总数，另有 `returned` / `truncated`）|
| 知识库 | **单库收口**（只暴露 `razubo7dra`）；支持文件列表、**按页 HTML 预览**、原文下载、同步/异步上传、删除 |
| 平均耗时 | 单问 6–20 s（近期实测多在 7–8 s；复杂问法偶有几十秒）|

**部署情况**：服务器（`47.121.183.81`）上跑的是 **4.0**；本仓库 `deploy_server4.0` 分支已是 **4.5**
（多出文件预览、异步上传、会话摘要、规则优先级、错误码规范化），**尚未部署到服务器**。
上线步骤见 `deploy/DEPLOY.md`。

## 二、怎么跑

    cd <项目根目录>\secretary     # <项目根目录> 换成你实际 clone/解压的路径
    py -X utf8 server.py            # 只监听 127.0.0.1

    set SECRETARY_HOST=0.0.0.0      # 局域网可访问（或双击根目录 启动局域网服务.bat）
    py -X utf8 server.py

> 更省事：直接双击根目录的 `启动演示页面.bat`（本机）或 `启动局域网服务.bat`（局域网）。两个脚本都用 `%~dp0` 定位自身所在目录，与项目放在哪个盘无关。

**第一次跑先配凭据**：把根目录的 `.env.example` 复制成同目录的 `.env`，填 6 个必填项 —— `DB_HOST / DB_PORT / DB_USER / DB_PASSWORD / LLM_BASE_URL / LLM_API_KEY`。`.env` 已被 `.gitignore` 忽略，不会进仓库。

| 环境变量 | 默认 | 说明 |
|---|---|---|
| SECRETARY_ENV | 项目根目录的 .env | DB 与模型端点凭据来源；不填就用本项目根目录的 .env |
| SECRETARY_HOST / SECRETARY_PORT | 127.0.0.1 / 8200 | 监听地址与端口 |
| SECRETARY_MODEL | qwen3.8-max | 模型，页面下拉 26 个可选 |
| SECRETARY_MAX_STEPS | 0 | 工具步数上限，0 = 不限（模型自己收手） |
| SECRETARY_KB_AGENT | aid-72fa8cae2b124d819617f157e97d0a1d | 知识检索服务（已绑 `r57xtq9ypm` + `razubo7dra` 两个库）|
| SECRETARY_KB_IDS | `r57xtq9ypm,razubo7dra` | 检索范围（逗号分隔）。**检索面**用这一对 |
| `DEFAULT_INDEX_ID`（在 `.env` 里，不是环境变量）| razubo7dra | **管理面**默认库：上传/删除/预览都对着它 |
| SECRETARY_KB_SINGLE | 1 | 单库收口：`/api/kb/indices` 只返回 `DEFAULT_INDEX_ID`，路径里传别的库**会被忽略** |
| SECRETARY_KB_ASYNC_DEFAULT | 0 | 上传默认同步；改 `1` 则默认异步（需前端先接轮询）|
| SECRETARY_SESSION_SUMMARY / _EVERY / _MIN_TURNS / _KEEP_RECENT | 1 / 5 / 6 / 2 | L2 会话摘要：总开关 / 触发轮数 / 注入的轮数门槛 / 始终原样带的最近轮数 |
| SECRETARY_RULES_PRIORITY | 1 | 是否在系统提示最前面加【规则优先级】声明（解决 system 规则与【统计范围】撞车）|
| SECRETARY_API_MAX_ROWS | 5000 | 历史接口"全部返回"时的防呆上限，超了 `truncated=true` |
| SECRETARY_TABLES | 空 | 限定可查的表；空 = 用字典里登记的全部 |

> ⚠️ **两套知识库配置不要混**：**检索面**（`SECRETARY_KB_AGENT` + `SECRETARY_KB_IDS`）决定"问答时去哪些库找资料"；
> **管理面**（`.env` 的 `DEFAULT_INDEX_ID`）决定"上传/删除对着哪个库"。两者是不同链路、不同凭据。
>
> ⚠️ `SECRETARY_*` 系列**写在 `.env` 里不生效**，只能走系统环境变量或 systemd 的 `Environment=`。
> 例外：`DEFAULT_INDEX_ID` 和 4 个百炼键是**读 `.env`** 的。

**端点**：

| 类别 | 端点 |
|---|---|
| 问答 | `POST /api/ask`（返回 answer / trace / timings，**多轮靠回传同一个 `session_id`**）|
| 历史对话 | `GET /api/sessions`（会话列表）、`GET /api/history`（某场会话的消息）—— **不传 `limit` 就是全部返回** |
| 其它问数 | `GET /api/stats`（概览）、`POST /api/feedback`（赞/踩）、`GET /api/gaps`（缺口）|
| 知识库 | `GET /api/kb/health`、`GET /api/kb/indices`、`GET /api/kb/indices/{id}/documents`、<br>`POST /api/kb/indices/{id}/documents`（上传，可 `?async=1`）、`DELETE /api/kb/indices/{id}/documents/{file_id}`、<br>`GET /api/kb/files/{file_id}`（详情）、`GET /api/kb/files/{file_id}/text?page=N`（**按页 HTML 预览**）、<br>`GET /api/kb/files/{file_id}/download`（下载原文）、`GET /api/kb/uploads/{task_id}`（查异步上传进度）|
| 页面与健康 | `GET /`（问答页）、`/asr`（语音页）、`/kb`（知识库控制台）、`GET /health`（六个能力字段）|
| 报告 | `POST /api/report`、`POST /api/report/notice`、`GET /api/report/meta` |

> 接口契约见 `deploy/接口对接说明.md`（问数）、`deploy/知识库接口文档.md`（知识库）、
> **`deploy/知识库与历史对话_对接手册.md`（面向前端，含可直接粘贴的 JS）**。

## 三、分工与流程（五段）

    ①信息补全（字面：纠错 + 名称归一，本服务；时间与地点：上游外置）
    ②问题补全（本服务，按知识库规则库）
    ③理解问题（查什么表 / 答哪些方面 —— 模型自己定）
    ④查库（run_sql，只读 + 白名单）
    ⑤总结回答（带口径）

| 阶段 | 谁做 | 本地代码里有什么 |
|---|---|---|
| ① 信息补全（字面） | 本服务 | name_fix.fix()：改错字（环谈→环潭）+ 名称归一（厉山→厉山供电所），零第三方依赖 |
| ① 信息补全（时间/地点） | **本服务** | time_scope.describe()：相对时间（上月/本月/今年）用代码换算成具体年月并写回问题文本；<br>多轮会话里本轮没提时间/地点时**沿用上一轮**（这是"说了沿用上一轮却按全市答"那类 bug 的修法）|
| ② 问题补全 | 本服务 | agent.complete_question()：去知识库取**补全规则切片** → 一次不带工具的模型调用 → 标准问题 |
| ③ 理解问题 | 模型 | 只注入业务字典的**表目录 + 字段说明**与 5 个工具；没有任何「问题→表」的映射 |
| ④ 查库 | 模型 | run_sql：只读闸 + 白名单（来自字典）+ 自动 LIMIT 200 |
| ⑤ 总结回答 | 模型 | 输出纪律（数字必须来自 run_sql、必答项逐项覆盖）+ 零工具守卫 |

## 四、硬约束（不要破坏）

1. **本地不预设业务规则**：口径、状态码、指标公式、补全规则，全部来自知识库与业务字典；本地只写「过程纪律」（如逐项覆盖、并发批量）。
2. **对数据库只读**：tools_db._exec() 有只读闸（仅 SELECT/WITH）。不要再写线上字典表；表结构由维护方改。
3. **不固化查询流程**：代码里没有「问题→表」的映射，也没有指标清单。

## 五、数据来源

| 来源 | 内容 | 用法 |
|---|---|---|
| 业务库 dlj_data | 业务表（真数据） | **严格只读**，run_sql 取数 |
| 业务字典 ai_data | `ai_table_metadata` / `ai_column_metadata` / `ai_metric_metadata` / `ai_column_synonym` / `ai_table_relation` | 只读；semantic.py 启动时读进内存 |
| 系统提示词 ai_data.ai_prompt | 三道：`answer_agent`（回答纪律）/ `business_rules`（业务规则）/ `sql_plan_rules`（查询规划）| 只读，**版本化**（取 `IS_new=1` 的最新一行，60 秒 TTL）。<br>★ **改提示词改库、不用改代码** |
| 知识库（检索面） | `r57xtq9ypm`（ai大脑通用语义知识库）+ `razubo7dra`（个人知识库）| kb_search 检索切片，用于**补全规则**与问答 |
| 记录库 agent_data | **本项目自建**：对话记录、知识库文件台账、纠错词典 | **读写**（唯一有写权限的库）|

**当前用到的知识库**：`r57xtq9ypm`（补全规则库 + 业务术语）与 `razubo7dra`（上传与预览的目标库）。
业务空间下还有别的库（`uxht00z9ey` 口径库、`igjhr8giyb` 业务库等），
但**服务端开了单库收口**，管理面只会暴露 `razubo7dra`。

> 📄 库与表的完整说明（含本次新增的 `t_kb_file` / `t_kb_file_page` / `t_kb_upload_task` 建表语句）
> 见 **`docs/数据库与新增表说明.md`**。

## 六、代码结构（secretary/）

| 文件 | 职责 |
|---|---|
| server.py | HTTP 服务 + 静态页 + 路由；**有全局异常兜底**（未预料的异常回 500 JSON 并记日志，不掐连接）；<br>传错参数一律 400，非法 JSON 也 400 |
| agent.py | 五段流程主体：问题补全 + 模型自主循环 + 工具定义 + trace；<br>**L2 会话摘要**的注入与两道闸；系统提示词拼装（`_system()`，最前面加【规则优先级】）|
| name_fix.py | **阶段①字面补全**：错字纠正 + 名称归一；零依赖，失败自动降级不影响主流程（详见 docs/接入-名称纠错与归一.md） |
| libs/name_correction_lib/ | 上面那步用的纠错引擎本体（标准库实现，附 8 个 CSV 词典与拼音表） |
| semantic.py | 语义层（只读）：表目录、字段目录、白名单校验、SQL 抽表 |
| tools_db.py | 只读闸、白名单拦截、list_tables / find_column / describe_table / run_sql |
| tools_kb.py | 百炼知识检索客户端（只要切片，不要它生成的答案；limit 可调） |
| gaps.py | 缺口清单：答不了的问题落 output/gaps/gaps.jsonl |
| tools_asr.py | 语音识别五阶段（/asr 页面用） |
| config.py | 读 .env、模型清单、端口/主机、检索面知识库、表范围，以及本批新增的开关 |
| time_scope.py | 阶段①的时间与地点补全（相对时间 → 具体年月；多轮沿用上一轮口径）|
| kb_api.py | 知识库 HTTP 接口层：路由、错误码、multipart 解析；**预览/下载/异步上传**都在这里 |
| kb_bailian.py | ★ 唯一接触阿里云 SDK 的文件（将来换云只改它）|
| kb_store.py | 原文件落盘 + 台账增删查 + 按页正文读写 + **异步抽取 worker** |
| text_extract.py | 文件 → 按页 HTML（PDF 走 pymupdf4llm，DOCX 走 mammoth…），**含 XSS 白名单清洗** |
| kb_upload.py | 异步上传任务表与后台 worker |
| session_summary.py | L2 会话摘要：异步生成（乐观锁写回）+ 每 N 轮触发 |
| qa_log.py | 对话记录读写；历史接口（含"全部返回"语义）；摘要读写 |
| static/index.html、static/asr.html | 问答页（左回答 / 右过程）、语音页 |
| tools_app.py | 上游补全应用客户端，**已不在链路里**（保留备查） |
| report.py | 年度缺陷治理计划分析：注入技能文档 + 通知数据，模型用现有工具自己查数、自己写报告（不写死 SQL）|
| skills/数据表说明书.md | 每张表做什么、怎么设计、有哪些坑 —— 写别的分析 skill 也复用这份 |
| skills/年度缺陷治理计划分析.md | 技能文档：分几节、每节查什么、怎么判断、不许做什么 |

## 七、待维护方定的口径（我们不改表，只列）

1. **「某月」是当月还是累计**：没定 → 同一问法两次答 156 件 / 0 件；当月未采集被答成 0 件（违反知识库 1.5「缺失不能当 0」）。
2. **公司预算金额两张表两个数**：budget_allocation（is_new=1）合计 1880 万；budget_result 按公司汇总约 1.11 亿。
3. **台区/线路数用全量还是有效**：t_power_area 全量 24876 / status=1 是 20388；t_power_line 172 / 157。
4. **工单类问题的时间作用字段**：现在按 create_time 过滤，而工单最新创建时间是 2026-08-24 → 有的问法答「查不到」。
5. **补全规则库里电力与政务口径重叠**：「预算都分给谁了」可能被补成财政口径，需要写明适用条件。

清单原件：output/字典效果实测与待补口径.md、output/AI字典改动记录.md（含改动前的字典备份 output/ai_dict_backup_20260916_0949.json）。

## 八、常用命令

    cd <项目根目录>\secretary
    py -X utf8 test_semantic.py     # 语义层自检（26 条断言，不依赖大模型）
    py -X utf8 e2e_test.py          # 27 题端到端
    py -X utf8 regression.py        # 30 题回归（含 3 道复杂题），落 output/regression_v3.json
    py -X utf8 e2e_raw.py           # 16 道原话走完整流程
    py -X utf8 truth_check.py       # 直接查库核对真值

    cd <项目根目录>
    py -X utf8 tools/apply_kb_file_schema.py         # 建知识库相关的三张表（幂等）
    py -X utf8 tools/apply_kb_file_schema.py --check # 只看现状、不建

## 九、文档地图

| 文档 | 写什么 |
|---|---|
| 位置 | 写什么 |
|---|---|
| README.md（本文件） | 现状、怎么跑、分工、约束、待办 |
| docs/ | 工程文档，索引见 docs/README.md |
| docs/架构图.md、docs/架构说明.md | 五张图 + 关键文件 |
| docs/实现文档.md、docs/工具说明.md | 关键决策与目录 / 5 个工具的契约 |
| docs/报告技能设计.md | 计划报告技能的设计与「实施后的修正」 |
| docs/语义层与提速方案.md、docs/局域网访问说明.md | 早期方案 / 给同事的访问说明 |
| docs/历史/ | 早期草案：构建说明、微调方案讨论、微调数据方案 |
| **docs/数据库与新增表说明.md** | **★ 用了哪几个库、`t_kb_file`/`t_kb_file_page`/`t_kb_upload_task` 三张新表干什么与完整表结构** |
| **`deploy/` 下的三份接口文档** | `接口对接说明.md`（问数主接口，含历史对话"全部返回"）｜`知识库接口文档.md`（知识库全量 + 部署运维）｜**`知识库与历史对话_对接手册.md`（面向前端，含可直接粘贴的 JS）** |
| skills/数据表说明书.md | 每张表做什么、怎么设计、有哪些坑 —— 写别的分析 skill 复用这份 |
| skills/年度缺陷治理计划分析.md | 技能文档：分几节、每节找哪类数据、怎么判断、报告里不许出现什么 |
| secretary/口径库.md、电力业务口径文档.md、数据地图.md | 知识库内容（已上传百炼，**不进仓库**） |
| output/ | 测试报告与原始数据（**不进仓库**） |

---

## 定时技能与问答链路（本项目特有约定，2026-09-22）

**定时触发的技能只回一句话标题**，走 `POST /api/skill/title`：

- 计数 **不由模型产生** —— 每个定时技能在 `skills/技能触发配置.json` 里声明用哪个指标（`title_rule.count_metric`），指标口径放在字典 `ai_data.ai_metric_metadata.metric_formula`，服务直接执行后返回标题。同一天同一技能连打 5 次必然同数（实测 13/118/0/0，约 120ms）。
- **无异常也返回**（`status:"ok"` + 一句话标题），不空返回。
- 定时技能：`approval-role`、`flow-monitor`、`progress-alert`、`risk-control`；各自的「标题判据（取哪个维度、优先级链）」写在对应技能文档里。

**明细追问**走 `POST /api/ask`，调用方需传：

| 入参 | 说明 |
|---|---|
| `today` | **业务日期，必须由上游传**（停留天数、同比、"截至今天"全按它算） |
| `scope` | 数据范围（强制，压过系统默认的"全市"） |
| `skill_id` | 传了就直接加载该技能文档当判断标准（不靠检索）；不传则由模型用 `kb_search` 按需检索知识库 |
| `want_page` / `page_mode` | 分析页面：`sync` 随答案返回；`async` 后台生成、用 `GET /api/page?token=` 取。页面按「问题+业务日期+范围」缓存 |

**补全只做地名/名称/时间这类信息**（`name_fix` + `time_scope`），**不改写用户的问题**；`qtype` 按实际发生的事判定（回答过程中查过判据才算分析）。

**规则文档一处维护、两处生效**：`skills/*.md` 是唯一维护源 → `tools/deploy_update.py` 上服务器（`skill_id` 路径直接读）→ `tools/kb_sync.py` 同步进知识库（自由问句路径检索）。改完规则记得两步都跑。
