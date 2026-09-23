# 服务端部署说明

> ## ⚠️ 这是 2.0 版，比 1.0 多一件事：**要先建记录库**
>
> 2.0 新增「对话记录 + 多轮上下文 + 历史会话」功能，问答会自动写入一个 **`agent_data`** 库。
>
> **升级步骤：**
> 1. 上传下面 8 个文件（或整个目录覆盖），**别覆盖 `.env`**。
> 2. 重启服务：`sudo systemctl restart secretary`。
> 3. 自检：`curl -s http://127.0.0.1:8200/health` → 应有 `"qa_log": true`。
>
> **记录库已经建好了**（`agent_data`，6 张表 + 2 个测试账号，就在同一台 MySQL 上），
> **不用再执行建表 SQL**。建表脚本 `agent_data_建表.sql` 留作参考：
> 换服务器 / 重建环境时才需要跑一次。
>
> ### 只上传这 8 个文件就够（其余 53 个完全没变）
>
> ```
> 【新增】
>   .env.example                        配置项说明（可选，方便以后查）
>   secretary/qa_log.py                 ★核心：落库 + 历史查询
>   secretary/给同事的接口对接说明.md       ← 注意：在 secretary/ 同级的根目录
>
> 【覆盖】
>   secretary/agent.py                  ★核心：多轮上下文 + 落库调用
>   secretary/server.py                 ★核心：3 个新接口
>   secretary/config.py                 新增 AGENT_DB / HISTORY_TURNS
>   secretary/time_scope.py             ★核心：本轮没说时沿用上一轮口径
>   DEPLOY.md                           本文档
> ```
>
> `__pycache__` / `*.pyc` 是运行时生成的，**不要传**。
> 验证方式：见「八、2.0 新增：对话记录库」。

## 一、上传什么

整个 `deploy_server2.0/` 目录（约 3.8 MB），传到你服务器的任意目录，例如 `/opt/secretary/`。

```
deploy_server2.0/
├── .env                  ← 【必须】数据库 + 模型凭据，含密码，别传到公开位置
├── .env.example          ← 配置项说明（可选项都在里面）
├── secretary/            ← 【必须】服务代码
│   ├── server.py         ← 入口（问答 + 历史会话接口）
│   ├── agent.py / config.py / semantic.py / time_scope.py / name_fix.py
│   ├── qa_log.py         ← 【2.0 新增】对话记录落库 + 历史查询
│   ├── tools_db.py / tools_kb.py / tools_asr.py / gaps.py / report.py
│   ├── libs/name_correction_lib/   ← 名称纠错与归一引擎（含 7 个 CSV 词典）
│   └── static/           ← 问答页面
├── start.sh              ← Linux 启动脚本
├── start.bat             ← Windows 启动脚本
├── install_service.sh    ← 一键装 systemd 常驻服务
└── 给同事的接口对接说明.md  ← 可直接转发给调用方
```

不需要 `docs/`、`skills/`、`verify/`、`output/` —— 那些是本地开发和测试用的。
`output/` 目录服务会自己建（缺口记录落盘）。

## 二、服务器装什么

**只要 Python 3.9+ 和一个包**：

```bash
# 1) Python（一般都自带，没有就装）
python3 --version            # 要 >= 3.9
yum install -y python3       # CentOS / 龙蜥
apt install -y python3       # Ubuntu / Debian

# 2) 唯一的三方依赖
python3 -m pip install pymysql
```

全部依赖就这一个。拼音是自己离线导的表，不装 pypinyin。
**不需要** MySQL 客户端——用 pymysql 纯 Python 驱动连库。

装完先自检一下能不能连上库和模型：

```bash
cd /opt/secretary/secretary
python3 -X utf8 -c "
import config, pymysql, json, urllib.request
c = pymysql.connect(host=config.DB['host'], port=config.DB['port'], user=config.DB['user'],
                    password=config.DB['password'], database='dlj_data', connect_timeout=10)
print('库 OK')
req = urllib.request.Request(config.LLM_BASE + '/models',
                             headers={'Authorization': 'Bearer ' + config.LLM_KEY})
print('模型 OK，共', len(json.loads(urllib.request.urlopen(req, timeout=20).read())['data']), '个')
"
```

## 三、启动

**端口是 8200**，可以用环境变量改：`SECRETARY_PORT=8300`。

### Linux

> **⚠️ 先看这一条：这个服务必须"常驻"，不能靠手动启动。**
>
> 用 `bash start.sh` 前台跑起来，**SSH 窗口一关、或会话超时，进程就会被系统杀掉**，
> 同事立刻调不通 —— 典型表现就是「昨天还能用，今天点开链接打不开，要重新启动一次」。

**对外提供服务，正确做法是装成 systemd 服务（一条命令）：**

```bash
cd /opt/secretary
chmod +x start.sh install_service.sh
sudo bash install_service.sh        # 开机自启 + 崩溃自动拉起 + 与 SSH 会话无关
```

装完就**再也不用管**：服务器重启会自动起来，进程挂了 5 秒内自动重启。

---

临时试跑（**只在调试时用**）：

```bash
bash start.sh          # 前台，报错直接看得见（关掉窗口就停）
bash start.sh -d       # 后台，但服务器重启后不会自启，仍需手动再跑一次
```

启动成功后窗口会打印：

```
model = qwen3.8-max
本机访问：http://127.0.0.1:8200
同事访问：http://<服务器IP>:8200      ← 把这一行发给同事
```

### Windows 服务器

双击 `start.bat`（同样会设好 `0.0.0.0` 并打印同事访问地址）。

### 常用运维命令（装完 install_service.sh 之后）

```bash
systemctl status secretary           # 看服务状态
systemctl restart secretary          # 重启（改完代码或 .env 后执行）
systemctl stop secretary             # 停止
journalctl -u secretary -f           # 看实时日志
tail -f server.log                   # 看程序输出日志
bash install_service.sh uninstall    # 卸载（停服务 + 取消开机自启）
```

`install_service.sh` 会自动生成 `/etc/systemd/system/secretary.service`
（`WorkingDirectory` 指向脚本所在目录，`Restart=always` 崩溃自动拉起），
**不需要你手写 systemd 配置文件**。

## 四、放行 8200 端口（**最容易漏的一步**）

服务本身没问题、同事却连不上，九成是这里。

```bash
# firewalld（CentOS / 龙蜥 / 红旗）
firewall-cmd --add-port=8200/tcp --permanent && firewall-cmd --reload
firewall-cmd --list-ports

# ufw（Ubuntu / Debian）
ufw allow 8200/tcp && ufw status

# 查看端口到底有没有在听
netstat -tlnp | grep 8200        # 要看到 0.0.0.0:8200，不是 127.0.0.1:8200
```

**如果是阿里云 / 腾讯云等云服务器，还要去控制台的「安全组」里放行 8200 入方向**——
系统防火墙放行了、安全组没放，一样连不上。

自检：在服务器上跑 `curl -s http://127.0.0.1:8200/health`

```json
{"ok": true, "model": "qwen3.8-max", "name_fix": true}
```

**三个字段都要看，不要只看第一个：**

| 字段 | 含义 |
|---|---|
| `ok` | 服务活着（能应答） |
| `model` | 当前默认模型 |
| `name_fix` | **纠错/归一是否生效** |

`name_fix` 为 `false` 时会附一个 `name_fix_reason` 说明原因。常见是
`libs/name_correction_lib/data/` 没跟着传上去（那 8 个 csv 是两万条真实台区名录，
**不在仓库里，必须随部署包一起传**）。缺了它服务照跑、问答照答，
**但错字纠正、简称补全、地点识别会静默失效**，很难发现。


## 五、两台机器之间的网络

- **同一个内网**：同事直接用 `http://<服务器内网IP>:8200/`
- **不在同一个内网**：需要服务器有公网 IP（或走公司 VPN）。公网暴露的话，
  见下面「六、安全性」——这个服务**没有登录校验**，任何人都能调。

## 六、安全性（部署前必须知道）

1. **服务没有任何鉴权**。只要端口可达，谁都能调用问答接口（虽然数据库是只读的）。
   建议：**只在内网开放**，或防火墙里限定只允许同事的网段访问：
   ```bash
   firewall-cmd --permanent --add-rich-rule='rule family="ipv4" source address="192.168.1.0/24" port port="8200" protocol="tcp" accept'
   ```
2. **`.env` 里有数据库密码和模型 Key**，文件权限收紧：
   ```bash
   chmod 600 /opt/secretary/.env
   ```
3. **数据库连接是只读的**（`tools_db.py` 里做了表白名单 + 只允许 SELECT），
   但代码本身不防注入式的越权——它靠的是白名单机制。
4. 服务是**单进程多线程**（Python 标准库 http.server）。单次问答 5~30 秒，
   **并发能力有限**，适合几个人用，不适合当生产网关。要扛并发得上 gunicorn/uwsgi + Flask/FastAPI 重写外层。

## 七、排查表

| 现象 | 原因 | 怎么办 |
|---|---|---|
| 同事连不上、一直转圈 | 防火墙 / 安全组没放行 | 第四节 |
| `Connection refused` | 服务没起来，或绑的是 127.0.0.1 | 看有没有 `0.0.0.0:8200`；确认 `SECRETARY_HOST=0.0.0.0` |
| 启动报 `ModuleNotFoundError: pymysql` | 没装依赖 | `python3 -m pip install pymysql` |
| 启动报 `.env` 找不到 | `.env` 没传或位置不对 | `.env` 要和 `secretary/` 平级 |
| 能访问但答「服务异常」 | 库或模型连不上 | 跑第二节那个自检脚本 |
| 换了网络后同事打不开 | 服务器 IP 变了 | 重新看启动窗口打印的地址 |
| 关掉窗口就断 | 前台进程 | 用 `bash start.sh -d` 或 systemd |
| **`/health` 里 `qa_log: false`** | **记录库没建 / 连不上** | **见下面第八节**；此时问答正常，只是不留记录 |
| **`/api/sessions` 返回空** | 记录库刚建、还没人问过 ／ `qa_log` 是 false | 先问一句再看 |

---

## 八、2.0 新增：对话记录库

### 8.1 这是干什么的

每次问答会自动往 `agent_data` 库写：

| 表 | 一行代表什么 |
|---|---|
| `t_chat_session` | 一场会话 |
| `t_chat_message` | 一条消息（一问一答算 2 条） |
| `t_chat_query_trace` | 一次问答的口径与质量（纠错命中 / 时间地点 / 查库次数 / 是否可信 / 用户评价） |
| `t_chat_trace_step` | 问答内部的每一步（纠错 / 补全 / 工具调用 / SQL / 作答） |

有了它，问答页面之外还能对外提供**历史对话列表**和**历史对话记录**接口（见《给同事的接口对接说明.md》）。

### 8.2 建库（一次性）

```bash
mysql -h 47.121.183.81 -u <账号> -p < agent_data_建表.sql
```

脚本会：
- 建 `agent_data` 库（**排序规则固定 `utf8mb4_general_ci`**，与 `dlj_data` 一致，否则跨库 JOIN 会报 `1267`）
- 建 6 张表
- 预置 2 个测试账号（`PROV-FIN-001` / `STATION-HEAD-001`）

> 脚本最后有一条**故意报错**的语句（验证唯一约束真的拦得住重复消息），
> 看到 `Duplicate entry '1-1' for key 'uk_session_seq'` **是正常的**。

**幂等**：脚本可以重复执行（表会 DROP 重建，测试账号靠唯一键不会插成 4 条）。
但**注意会清掉已有记录** —— 生产上重跑前先备份。

### 8.3 配置

| 变量 | 默认 | 说明 |
|---|---|---|
| `SECRETARY_AGENT_DB` | `agent_data` | 记录写到哪个库 |
| `SECRETARY_QA_LOG` | 开 | 设 `0` 则整个记录功能关闭（服务照跑，不留记录） |
| `SECRETARY_HISTORY_TURNS` | `5` | **多轮上下文**：同一会话带最近 N 轮问答。设 `0` = 关闭，回到「每次提问完全独立」 |

用的是**同一台 MySQL、同一个账号**，只是库名不同，所以 `.env` 不用改也能用。

### 8.4 关掉记录功能

```bash
SECRETARY_QA_LOG=0 bash start.sh -d
```

用途：记录库还没建好、或临时不想写库时。关掉后 `/health` 里 `qa_log` 为 `false` 并附原因。

### 8.5 自检

```bash
# 1) 服务侧
curl -s http://127.0.0.1:8200/health        # 要有 "qa_log": true

# 2) 问一句，再看历史
curl -s -X POST http://127.0.0.1:8200/api/ask \
  -H "Content-Type: application/json" \
  -d '{"question":"环潭供电所的线损率","uid":"PROV-FIN-001"}' --max-time 90
curl -s "http://127.0.0.1:8200/api/sessions?uid=PROV-FIN-001"

# 3) 记录库侧
mysql -h 47.121.183.81 -u <账号> -p -e "
SELECT COUNT(*) FROM agent_data.t_chat_session;
SELECT id,uid,username,role FROM agent_data.t_user;"
```

**关键：落库失败不会影响问答。** 记录库连不上时，问答照常返回，
只是响应里 `qa_id` 为 `null`、`/health` 里 `qa_log` 为 `false`，日志里会有一行
`[qa_log] 落库失败（不影响回答）：…`。

---

## 九、4.1 新增：知识库文件预览（本地副本 + 按页 HTML）

> 接口契约与前端用法见《知识库接口文档.md》**第十一节**，这里只说部署要做什么。

### 9.1 建表（一次性）

```bash
cd /root/agentkownlg/deploy_server
python3 -X utf8 tools/apply_kb_file_schema.py          # 建表 + 自检
python3 -X utf8 tools/apply_kb_file_schema.py --check  # 只看现状、不建
```

会建 `t_kb_file`（文件台账）与 `t_kb_file_page`（按页 HTML），
并给 `t_chat_session` 加三个摘要列（`summary` / `summary_upto_seq` / `summary_time`，P2 会话记忆用）。
**幂等**，可重复执行，不会清数据。

> ★ **不建表也不会让服务起不来**：上传照旧成功，只是没有本地副本、没有预览
> （`/api/kb/files/**` 报 503，`/health` 里 `kb_files:false`）。

### 9.2 装抽取依赖

```bash
python3 -m pip install -r requirements.txt -i https://mirrors.aliyun.com/pypi/simple/
```

新增这几类（**缺哪个只影响那种格式**，其余照常）：

| 依赖 | 干什么 | 不装的后果 |
|---|---|---|
| `mammoth` | DOCX → HTML | docx 无预览 |
| `openpyxl` | XLSX，每 sheet 一页 | xlsx 无预览 |
| `pymupdf4llm` (+`markdown`) | PDF → 按页 Markdown → HTML | 退回纯 `pymupdf`（正文能看但丢表格结构）；**体积较大，会顺带装 onnxruntime** |
| `python-pptx` | PPTX，每 slide 一页 | pptx 无预览 |
| **`nh3`（或 `bleach`）** | **HTML 白名单清洗** | **两个都没有 → 抽取直接报错**（宁可不预览，也不把未清洗的 HTML 入库）|

装完自检一下哪些格式真的能用：

```bash
cd /root/agentkownlg/deploy_server/secretary
python3 -X utf8 -c "import text_extract; print(text_extract.status())"
```

### 9.3 原文件存哪

默认落在 **`<部署目录>/kb_files/`**（与 `secretary/` 平级），可用环境变量改：

```ini
# systemd 里加： Environment=SECRETARY_KB_FILES=/data/kb_files
```

目录结构：`{root}/{index_id}/{md5}/{原文件名}`。用 md5 当目录名 —— 上传前就能算出来、
天然去重、失败时好清理，不依赖云端返回的 file_id。

> **留意磁盘容量**：原文件会一直留着（删文档时**默认保留**，只有传 `drop_raw=1` 才删）。
> 长期运行要盯一下这个目录的增长，必要时挂独立数据盘。

### 9.4 验证

```bash
# ① 能力字段都要 true（完整清单与含义见 10.6）
curl -s http://127.0.0.1:8200/health
# {"ok":true,...,"kb":true,"kb_files":true,"kb_files_source":"mysql:agent_data"}

# ② 传一个真实文件（同时落盘 + 后台抽 HTML）
curl -s -X POST -F "file=@公司简介.pdf" "http://127.0.0.1:8200/api/kb/indices/razubo7dra/documents"
#   记下响应里的 file_id；local.extract_status 一开始是 pending

# ③ 轮询到 done（大文件几十秒到几分钟）
curl -s "http://127.0.0.1:8200/api/kb/files/<file_id>"

# ④ 取正文（HTML 富文本）
curl -s "http://127.0.0.1:8200/api/kb/files/<file_id>/text?page=1"

# ⑤ 下载原文件
curl -OJ "http://127.0.0.1:8200/api/kb/files/<file_id>/download"
```

**重启不会丢抽取任务**：启动时会扫台账里 `pending/extracting` 的行自动重新排队，
启动日志里有一行 `[启动] 文件台账就绪（… 本次恢复 N 个待抽取）`。

---

## 十、4.2 新增：单库收口 + 异步上传 + 会话摘要

> 接口契约与前端用法见《知识库接口文档.md》**第十二节**；
> L2 摘要新增的返回字段见《接口对接说明.md》。这里只说**部署要做什么**。

### 10.1 多建一张表（`t_kb_upload_task`）

异步上传的任务表。`tools/kb_file_schema.sql` 里已经加进去了，
**重跑一次建表脚本就行**（幂等，不会清数据）：

```bash
cd /root/agentkownlg/deploy_server
python3 -X utf8 tools/apply_kb_file_schema.py          # 建表 + 自检
python3 -X utf8 tools/apply_kb_file_schema.py --check  # 只看现状、不建
```

> ★ **不建这张表也不会让服务起不来**：只有带 `?async=1` 的上传会失败，
> **同步上传照旧**；`/health` 里 `kb_upload:false`。

### 10.2 单库收口（前端只能看到一个知识库）

**默认就是开的**，不用配。它做两件事，都是**服务端强制**（不是前端不显示）：

| 行为 | 效果 |
|---|---|
| `GET /api/kb/indices` | 只返回 `DEFAULT_INDEX_ID`（`razubo7dra`）那一个，并带 `locked:true` |
| 路径里的 `index_id` | **一律忽略**，统一用 `DEFAULT_INDEX_ID`；传了别的值会打日志留痕 |

```bash
# 想恢复"能看能操作业务空间下全部知识库"的老行为：
# systemd 里加 Environment=SECRETARY_KB_SINGLE=0
```

> ⚠️ **为什么必须在服务端做**：只要 `/api/kb/indices` 还在返回全部，
> 调用方换个 `index_id` 照样能操作别的库 —— **前端隐藏 ≠ 隔离**。

### 10.3 异步上传

**默认仍是同步**（对已上线的老前端零影响）。加一个查询参数就走异步：

```bash
# 同步（默认，最长等云端 300 秒）
curl -s -X POST -F "file=@公司简介.pdf" \
  "http://127.0.0.1:8200/api/kb/indices/razubo7dra/documents"

# 异步（立刻返回 task_id）
curl -s -X POST -F "file=@公司简介.pdf" \
  "http://127.0.0.1:8200/api/kb/indices/razubo7dra/documents?async=1"
# → {"task_id":"up_9124575e…","status":"pending","status_url":"/api/kb/uploads/up_…"}

# 轮询（pending → uploading → done / failed）
curl -s "http://127.0.0.1:8200/api/kb/uploads/up_9124575e…"
# → done 时带 file_id，拿它去 /api/kb/files/{file_id} 看预览
```

**前端接完异步轮询后**，可以把它设成默认（不带 `?async=1` 也走异步）：

```ini
# systemd 里加： Environment=SECRETARY_KB_ASYNC_DEFAULT=1
```

> ⚠️ **服务重启会把"上传中"的任务标成 `failed`，不自动重试。**
> 原因：上传链路（申请租约 → PUT → 登记 → 等解析）**不幂等**，
> 而且中断时无法判断上次传到哪一步 —— 重试只会造重复文档。
> 中断的任务仍在表里，能查到，但需要人工重传。

### 10.4 L2 会话摘要（长会话也能记住）

**默认就是开的**，不用配。后台 daemon 线程异步生成，**不阻塞问答**。

原来「继续对话」只带最近 5 轮、每轮回答只留前 400 字 —— 翻出一场很长的旧会话继续聊，
更早的内容模型是空白。L2 把更早的轮次压成一段 ≤150 字的「上下文交接条」补上：

```
user_content = 本轮问题
             +【本场会话摘要】   ← 更早轮次的浓缩（L2 新增，**要过两道闸**，见下）
             +【对话历史】       ← 最近 5 轮原话（其中最近 2 轮始终保留原话）
             +【统计范围】+【名称归一说明】+【补全参考】+ 用户画像
```

**两道注入闸**（2026-09-22 加）：

| 闸 | 条件 | 为什么 |
|---|---|---|
| ① | `history_turns=0` | 调用方写 0 就是"本轮**完全**不要记忆"，摘要不能绕过它溜进来 |
| ② | 会话总轮数 < `SECRETARY_SUMMARY_MIN_TURNS`（默认 6）| 短会话的 history 已覆盖全场，注入是纯重复 |

> 闸门只影响**注入**，不影响**生成**。响应里的 `summary_skip` 会写明为什么没注入，排障先看它。

★ **摘要只写"还没过期的关注对象与固定叫法"，三样东西一律不写**（2026-09-22 收紧，理由见下）：
① 时间范围与统计主体 —— 那是【统计范围】的活，摘要再写一份只会和它打架；
② 任何结论、判断或状态（"未查到""未生成工单""处于草稿状态""数据缺失已改用累计"…）——
   这类话**没有数字**，躲得过"不许写数字"的纪律，却会被模型当成现成答案而不再查数；
③ 过程叙述（"后续轮次中又增加了…"）。

> **为什么要收紧**：旧版要求写"关注对象 + 已确认口径 + **结论性事实**"，实测生成出来的摘要
> 变成"对象清单 + 口径枚举 + 时效断言"的堆叠 —— 例如"统计主体**先后使用过**全市和两水供电所"、
> "包括**两个**时间范围"、"工单**尚在**实施中"。续聊时这些和本轮的【统计范围】打架，
> 把回答带偏。收紧后重新生成 9 场：**口径枚举 0 场、时效断言 0 场，字数从平均 136 降到 43**。

**相关开关**（都在 systemd 单元里加 `Environment=`）：

| 变量 | 默认 | 说明 |
|---|---|---|
| `SECRETARY_SESSION_SUMMARY` | `1` | 总开关；`0` = 既不生成也不注入 |
| `SECRETARY_SUMMARY_EVERY` | `5` | 未摘要的轮数攒够多少才触发一次（**不是每轮都抽**，省 token）|
| `SECRETARY_SUMMARY_KEEP_RECENT` | `2` | 最近几轮**始终原样带**（保证追问的精细度）。★ 这几轮**不会再被写进摘要**，避免同一件事在提示里出现两遍 |
| `SECRETARY_SUMMARY_MIN_TURNS` | `6` | 会话**总轮数**达到这个值才注入摘要。短会话（≤5 轮）的 history 本来就覆盖全场，注入纯属重复。设 `0` = 不设门槛 |
| `SECRETARY_SUMMARY_MODEL` | 空 | 摘要用哪个模型；留空 = 同主模型（想省钱可填 flash 级）|
| `SECRETARY_SUMMARY_MAX_CHARS` | `150` | 摘要正文长度上限（字）★ 与提示词里写的 150 对齐 |

### 10.5 本轮新增开关一览

| 变量 | 默认 | 加在哪 |
|---|---|---|
| `SECRETARY_KB_SINGLE` | `1` | 单库收口，`0` = 恢复能看全部库 |
| `SECRETARY_KB_ASYNC_DEFAULT` | `0` | `1` = 上传默认走异步 |
| `SECRETARY_SESSION_SUMMARY` | `1` | 会话摘要总开关 |
| `SECRETARY_SUMMARY_EVERY` | `5` | 摘要触发阈值（轮）|
| `SECRETARY_SUMMARY_KEEP_RECENT` | `2` | 最近几轮始终带原话（且不再写进摘要）|
| `SECRETARY_SUMMARY_MIN_TURNS` | `6` | 会话总轮数达到才注入摘要（`0`=不设门槛）|
| `SECRETARY_SUMMARY_MODEL` | 空 | 摘要模型（空=同主模型）|
| `SECRETARY_SUMMARY_MAX_CHARS` | `150` | 摘要长度上限（与提示词对齐）|
| `SECRETARY_KB_FILES` | `<部署目录>/kb_files` | 原文件落盘目录（见 9.3）|

> ⚠️ **这些都是 `SECRETARY_*` 系列，写在 `.env` 里不生效** ——
> 只能走 **systemd 单元里的 `Environment=`**（`config.py` 读的是 `os.environ`）。
> 改完必须 `systemctl daemon-reload` 再 `restart`。

### 10.6 验证

```bash
# ① 六个能力字段都要 true（新增 kb_upload / session_summary）
curl -s http://127.0.0.1:8200/health
# {"ok":true,…,"kb":true,"kb_files":true,"kb_upload":true,
#  "session_summary":true,"session_summary_source":"mysql:agent_data"}

# ② 单库收口生效（期望 total_count=1、locked=true）
curl -s "http://127.0.0.1:8200/api/kb/indices?page_size=50"

# ③ 异步上传走一遍（见 10.3 三条命令）
# ④ 会话摘要：同一 session_id 连问 6 轮以上，第 6 轮起响应里 summary_used=true
```

启动日志里应能看到这三行：

```
[启动] 文件台账就绪（mysql:agent_data，已存 N 个文件，本次恢复 N 个待抽取）
[启动] 异步上传就绪（mysql:agent_data，历史任务 {...}，本次标记中断 N 个）
[启动] 会话摘要就绪（mysql:agent_data，每 5 轮触发一次，本次补跑 N 场）
```

### 10.7 四个坑

1. ★ **必须 `daemon-reload` + `restart`** —— 本轮新增了 systemd `Environment=`，
   只 `restart` 不会让新环境变量生效。
   复核：`systemctl show secretary -p Environment | tr ' ' '\n' | grep -i 'KB\|SUMMARY'`
2. **摘要是异步的** —— 回答响应当轮 `summary_queued=true` 只表示"已投递"，
   摘要文本要等下一次提问才会被注入（不要以为当轮就该有）。
3. **收口后传错 `index_id` 不会报错**，会被静默改成 `razubo7dra` ——
   排障时如果发现操作的不是预期库，先翻服务日志里的忽略告警。
4. **摘要生成的模型调用是额外成本** —— 每 `SUMMARY_EVERY` 轮一次；
   会话量大了想省钱，把 `SECRETARY_SUMMARY_MODEL` 指到便宜模型上。

---

## 十一、4.4 新增：系统提示词的「规则优先级」声明

> 接口没变、也不需要建表。**只有一处新配置**，其余是提示词内容层面的调整。

### 11.1 为什么要加

`business_rules【时间处理】` 写着「**未指定年份时默认使用当前年份**」，而代码在多轮会话里
会按「沿用上一轮」给出**别的**年份，并把结果放进 user 段的【统计范围】。
**两处规则撞车，而原文任何地方都没声明谁优先** —— 模型只能自己猜，表现就是"回答被带偏"。

### 11.2 做了什么

| 改动 | 位置 | 说明 |
|---|---|---|
| 在 system **最前面**加一段【规则优先级】 | **代码** `agent.RULES_PRIORITY` | 纯新增，不删任何原有规则 |
| 新开关 `SECRETARY_RULES_PRIORITY` | `secretary/config.py` | 默认 `1`；设 `0` 即恢复原状 |
| 修 `business_rules【时间处理】` 的自相矛盾表述 | **`ai_data.ai_prompt`** 新增一版 | 见 11.3 |

★ **注意措辞**：优先级 ① 写的是「**本轮问题 +【统计范围】+【名称归一说明】**」，
**不是「用户原话」** —— `user_content` 第一行是**代码纠错（名称/字面纠错）+ 时间地点补全之后**的问题，
提示词里**没有**原话。别按"以用户原话为准"去理解。

```text
========== 规则优先级（冲突时一律按此顺序，前面覆盖后面） ==========
① 本轮问题 +【统计范围】+【名称归一说明】—— 这三样都由代码确定，**是唯一权威**：
   问题里的时间、地点、名称都不要再改动，不要自己推算日期，不要另立一套口径。
   （问题已按词典纠正过名称，请直接用它的写法，不要再改回去）
② 本轮检索到的业务规则切片（【补全参考】里那些）—— 它**只是参考**。
③ 本 system 里的各节通用规则（下面所有【】开头的小节）—— 与前两条冲突时让位。
④ 【本场会话摘要】与【对话历史】—— 只用来理解"本轮在问什么"，
   **既不是数据来源、也不是结论来源**，里面的数字和结论一律不得直接引用。

冲突时**直接按前者执行**：不要"综合两者"，也不要在回答里出现两个版本。
```

### 11.3 提示词表那一版（**不是代码改动**）

```sql
-- 已在 ai_prompt 里执行过，这里只是记录，便于回滚与在新环境复现
-- 新行 id=39 / version=2 / IS_new=1：business_rules 的【时间处理】里
--   原文：- 年份筛选使用 create_time 范围：create_time >= '年份-01-01' AND create_time < '(年份+1)-01-01'
--   改后：- 年份筛选**按表取不同字段**（见下面【年份字段对照】），不要一律套用 create_time
-- 旧行 id=21 保留、IS_new 置 0
```

**回滚**：

```sql
UPDATE ai_prompt SET IS_new=0 WHERE id=39;
UPDATE ai_prompt SET IS_new=1 WHERE id=21;
```

> ⚠️ `business_rules` 的表头写着「业务规则（**所有Agent通用**）」——
> 若有别的 Agent 也读这个 key，会同时看到这处改动。
> 若只想影响本项目，应给本项目单开一个 key 并加进 `SECRETARY_PROMPTS`。

### 11.4 验证

```bash
# ① 服务起得来、能力字段照旧
curl -s http://127.0.0.1:8200/health

# ② 优先级声明确实在最前面（★ 用探针看"真实 system 段"，别只读代码）
#    期望：第 1 行是 "========== 规则优先级..."，且它在 "===== answer_agent =====" 之前

# ③ 回归：纠错与时间口径
printf '%s' '{"question":"环谈供电所的线损率","history_turns":0}' > /tmp/q.json
curl -s -X POST http://127.0.0.1:8200/api/ask -H 'Content-Type: application/json' \
     --data-binary @/tmp/q.json --max-time 180
#    期望 fixed_question 含「环潭」，回答给出具体线损率
```

> ⚠️ `SECRETARY_RULES_PRIORITY` 也是 `SECRETARY_*` 系列 ⇒ **写在 `.env` 里不生效**，
> 只能走 systemd `Environment=`，改完 `daemon-reload` + `restart`。
