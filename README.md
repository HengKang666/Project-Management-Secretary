# 项目管理秘书（AI 问答后端）

输入一条**已经补过时间与地点的问题**，模型自己决定查哪张表、怎么算，取数后作答，全程留痕（可审计）。
本地代码里**没有一条业务规则**：口径与表知识全部来自线上维护的业务字典和知识库。

---

## 一、当前状态（2026-09-16）

| 能力 | 状态 |
|---|---|
| 问题补全（按知识库规则库） | 已生效，补全耗时约 2–3 s |
| 六项指标（售电量/线损率/意见工单/故障报修/停电时长/跳闸） | 全部走统一快照表，本期/同比/环比都能答 |
| 预算、工单分档、台区线路数 | 能答，但口径未定，数字会随问法变（见第七节） |
| 超范围问题（酒店/GDP/三公） | 如实拒答，并落缺口清单 |
| 平均耗时 | 单问 6–20 s（补全 2–3 s + 查数作答 4–17 s） |

当前正在跑：监听 0.0.0.0:8200。本机 http://127.0.0.1:8200/ ，局域网 http://192.168.0.178:8200/ （IP 是 DHCP，可能变，看启动窗口打印）。

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
| SECRETARY_KB_AGENT | aid-72fa8cae2b124d819617f157e97d0a1d | 知识检索服务（只绑 r57xtq9ypm） |
| SECRETARY_KB_IDS | r57xtq9ypm | 只检索这个知识库 |
| SECRETARY_TABLES | 空 | 限定可查的表；空 = 用字典里登记的全部（22 张） |
| SECRETARY_COMPLETION_APP | e207644fd37247af957b7fbc613e6efc | 上游「信息补全」工作流应用 id（机构名纠错 + 统计时间） |

端点：问答 POST /api/ask {question, session_id}（返回 answer/scope/session_id/qa_id/qtype/page_html；分析题才给 page_html）｜ 定时技能标题 POST /api/skill/title {skill_id,account}（只回标题，无异常也回）｜ 知识库管理 GET|POST|DELETE /api/kb/**、页面 GET /kb ｜ 技能触发 POST /api/skill/run {skill_id,questions,inputs} ｜ 页面 GET /，技能触发台 GET /skills ｜ 语音页 /asr ｜ 健康 GET /health（含纠错词典与记录库状态）｜ 缺口 GET /api/gaps ｜ 会话列表 GET /api/sessions ｜ 会话历史 GET /api/history?session_id= ｜ 统计 GET /api/stats ｜ 评价 POST /api/feedback ｜ 技能清单 GET /api/triggers、/api/skills、/api/statechange ｜ 计划报告 POST /api/report {notice} ｜ 模拟通知 POST /api/report/notice {station} ｜ 报告参数 GET /api/report/meta。

## 三、分工与流程（五段）

    ①信息补全（本服务：字面纠错 + 名称归一 + 时间与地点换算）
    ②问题补全（本服务，按知识库规则库）
    ③理解问题（查什么表 / 答哪些方面 —— 模型自己定）
    ④查库（run_sql，只读 + 白名单）
    ⑤总结回答（带口径）

| 阶段 | 谁做 | 本地代码里有什么 |
|---|---|---|
| ① 信息补全（字面） | 本服务 | name_fix.fix()：改错字（环谈→环潭）+ 名称归一（厉山→厉山供电所）；词典从 agent_data 的 t_nc_* 表读，失败自动降级 |
| ① 信息补全（时间/地点） | 本服务 | time_scope.describe()：相对时间词换成具体期间；本轮没说的**优先沿用上一轮**，再没有才补默认值 |
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
| 业务库 dlj_data | 133 张业务表（真数据） | 只读，run_sql 取数 |
| 业务字典 ai_data | 5 张表：ai_table_metadata（表，17 行）、ai_column_metadata（字段，429 行）、ai_metric_metadata（指标口径，25 条）、ai_column_synonym（同义词，126 条）、ai_table_relation | 只读；semantic.py 启动时读进内存 |
| 记录库 agent_data | 对话记忆 4 张表：t_chat_session / t_chat_message / t_chat_query_trace / t_chat_trace_step；纠错词典 6 张 t_nc_* 表 | qa_log.py 读写（**唯一需要写权限的地方**，失败只打日志不影响问答） |
| 知识库 r57xtq9ypm | ai大脑通用语义知识库：**补全规则库**（「用户问 X 时要补成 Y」）+ 业务术语词典 | kb_search 检索切片 |

三个知识库的分工：uxht00z9ey 口径知识库 ｜ igjhr8giyb 业务知识库 ｜ r57xtq9ypm 通用语义知识库（**当前只用它**）。

## 六、代码结构（secretary/）

| 文件 | 职责 |
|---|---|
| server.py | HTTP 服务 + 静态页；路由 /health /api/ask /api/asr /api/gaps /api/skill/* /api/kb/* |
| agent.py | 五段流程主体：问题补全（顺带判题型 查数/分析）+ 模型自主循环 + 3 个工具定义（run_sql/find_column/kb_search）+ 分析题注入《分析规则》与生成分析页面 + trace |
| name_fix.py | **阶段①字面补全**：错字纠正 + 名称归一；零依赖，失败自动降级不影响主流程（详见 docs/接入-名称纠错与归一.md） |
| libs/name_correction_lib/ | 上面那步用的纠错引擎本体（标准库实现，附 8 个 CSV 词典与拼音表） |
| semantic.py | 语义层（只读）：表目录、字段目录、白名单校验、SQL 抽表 |
| tools_db.py | 只读闸、白名单拦截、find_column / run_sql（list_tables、describe_table 已废：表目录本来就整份注入系统提示） |
| tools_kb.py | 百炼知识检索客户端（只要切片，不要它生成的答案；limit 可调） |
| gaps.py | 缺口清单：答不了的问题落 output/gaps/gaps.jsonl |
| tools_asr.py | 语音识别五阶段（/asr 页面用） |
| config.py | 读 .env、模型清单、端口/主机、知识库、表范围 |
| static/index.html、static/asr.html | 问答页（左回答 / 右过程，带会话记忆）、语音页 |
| static/skills.html | 技能触发台：选技能 → 改上游数据 → 触发 → 并排对比「实际提问 vs 走技能触发」 |
| qa_log.py | **对话记忆落库**：会话/消息/口径与质量/执行明细写进 agent_data；多轮上下文从它读 |
| time_scope.py | 阶段①时间与地点：相对时间换算、沿用上一轮、【统计范围】组装 |
| lex_source.py | 纠错词典的来源（agent_data 表 或 本地 CSV），带版本指纹热更新 |
| tools_app.py | 上游「信息补全」工作流客户端（自动纠正机构名 + 补统计时间），agent.ask 的第一步 |
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
    py -X utf8 env_check.py         # 【换机器部署后先跑这个】读的哪个 .env / 连的哪个库 / 提示词与字典有没有内容
    py -X utf8 test_semantic.py     # 语义层自检（26 条断言，不依赖大模型）
    py -X utf8 e2e_test.py          # 27 题端到端
    py -X utf8 regression.py        # 30 题回归（含 3 道复杂题），落 output/regression_v3.json
    py -X utf8 e2e_raw.py           # 16 道原话走完整流程
    py -X utf8 truth_check.py       # 直接查库核对真值

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
| skills/数据表说明书.md | 每张表做什么、怎么设计、有哪些坑 —— 写别的分析 skill 复用这份 |
| skills/年度缺陷治理计划分析.md | 技能文档：分几节、每节找哪类数据、怎么判断、报告里不许出现什么 |
| secretary/口径库.md、电力业务口径文档.md、数据地图.md | 知识库内容（已上传百炼，**不进仓库**） |
| output/ | 测试报告与原始数据（**不进仓库**） |
