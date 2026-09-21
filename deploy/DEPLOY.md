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
