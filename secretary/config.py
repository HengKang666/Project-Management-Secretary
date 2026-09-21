# -*- coding: utf-8 -*-
"""配置：读本项目根目录的 .env（默认），模型可用环境变量切换。"""
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.environ.get('SECRETARY_ENV', os.path.join(os.path.dirname(HERE), '.env'))


REQUIRED = ('DB_HOST', 'DB_PORT', 'DB_USER', 'DB_PASSWORD', 'LLM_BASE_URL', 'LLM_API_KEY')

_TEMPLATE_HINT = (
    '\n[配置] 缺少 %s\n'
    '       这个文件不在仓库里（含密码，刻意不入库），需要自己建一份：\n'
    '\n'
    '         cp .env.example .env        # Linux / macOS\n'
    '         copy .env.example .env      # Windows\n'
    '\n'
    '       然后把 6 项填上：%s\n'
    '       各项说明见 .env.example 里的注释。\n'
)


def load_env(path=ENV_PATH):
    if not os.path.exists(path):
        raise SystemExit(_TEMPLATE_HINT % (path, ' / '.join(REQUIRED)))
    d = {}
    for line in open(path, encoding='utf-8'):
        line = line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        k, _, v = line.partition('=')
        # 剥离行尾注释：仅当 '#' 前有空白时才算注释，避免截断含 '#' 的密码
        v = re.split(r'\s+#', v, maxsplit=1)[0]
        d[k.strip()] = v.strip()
    missing = [k for k in REQUIRED if not d.get(k)]
    if missing:
        raise SystemExit('[配置] %s 里这些项还是空的：%s\n'
                         '       填好再启动（说明见 .env.example）。'
                         % (path, ' / '.join(missing)))
    return d


ENV = load_env()

DB = {
    'host': ENV['DB_HOST'],
    'port': int(ENV['DB_PORT']),
    'user': ENV['DB_USER'],
    'password': ENV['DB_PASSWORD'],
    'db': 'dlj_data',
}
SCHEMA = 'dlj_data'

# 对话记录库：问答/会话/消息/执行明细写到这里（表结构见 agent_data_建表.sql）。
# 与业务库同一台 MySQL、同一账号，只是库名不同。要用独立账号就改 DB_USER 那一套。
# 整个记录功能可以用 SECRETARY_QA_LOG=0 关掉（关掉后服务照跑，只是不留记录）。
AGENT_DB = os.environ.get('SECRETARY_AGENT_DB', 'agent_data')

# 纠错词典的数据源：
#   db   = 读数据库（默认）。词典在 agent_data 的 6 张 t_nc_* 表里，
#          **服务启动时一次性全量读进内存**，之后纠错全程不查库。
#   file = 读 libs/name_correction_lib/data/ 下的 CSV（零依赖，供独立部署/回退用）。
# 建表见 tools/lexicon_schema.sql，导入见 tools/import_lexicon.py。
# 环境变量和 .env 都可以配，环境变量优先。
LEXICON_SOURCE = (os.environ.get('SECRETARY_LEXICON')
                  or ENV.get('SECRETARY_LEXICON') or 'db').strip().lower()

# 纠错词典库：与对话记录库同一台 MySQL、同一账号，只是库名不同。
# 键名必须用 database 而不是 db —— pymysql 1.1+ 已把 db 标为废弃，传进去会刷警告。
LEXICON_DB_NAME = (os.environ.get('SECRETARY_LEXICON_DB')
                   or ENV.get('SECRETARY_LEXICON_DB') or AGENT_DB)
LEXICON_DB = {
    'host': DB['host'],
    'port': DB['port'],
    'user': DB['user'],
    'password': DB['password'],
    'database': LEXICON_DB_NAME,
}

# 词典热更新：每隔这么久（秒）查一次版本指纹，发现词典被改过就重建纠错引擎。
# 这样人工 UPDATE 完词典不用重启服务。设 0 = 关闭探测（只在启动时加载一次）。
LEXICON_RECHECK_SEC = int(os.environ.get('SECRETARY_LEXICON_RECHECK')
                          or ENV.get('SECRETARY_LEXICON_RECHECK') or '60')

# 多轮上下文：同一场会话里，把最近 N 轮问答带上作为模型上下文。
# 作用：理解「那全市的呢」这类省略主语的问法；问题没提时间/地点时沿用上一轮口径。
# 设 0 = 关闭（每次提问完全独立，与 2.0 之前行为一致）。
# 2026-09-21 由 3 调到 5：长会话里「上一轮」常常超出 3 轮就丢失指代，
# 5 轮的上下文增量可接受（每轮最多带 400 字回答，见 qa_log.load_context）。
HISTORY_TURNS = int(os.environ.get('SECRETARY_HISTORY_TURNS', '5'))

LLM_BASE = ENV['LLM_BASE_URL'].rstrip('/')
LLM_KEY = ENV['LLM_API_KEY']
MODEL = os.environ.get('SECRETARY_MODEL', 'qwen3.8-max')

# 可选模型清单（全部来自本端点 GET /models 实测存在，共 250 个；这里挑出适合本场景的）
# group 用于前端分组；note 写清"什么时候该用它"。
MODELS = [
    # 通义千问 · 3.8（最新一代）
    {'id': 'qwen3.8-max', 'label': 'Qwen3.8-Max', 'group': '通义千问', 'note': '旗舰，默认；工具调用与长回答最稳'},
    {'id': 'qwen3.8-max-0902', 'label': 'Qwen3.8-Max-0902', 'group': '通义千问', 'note': '旗舰快照版（版本固定，便于复现）'},
    {'id': 'qwen3.8-flash', 'label': 'Qwen3.8-Flash', 'group': '通义千问', 'note': '同代轻量，最快；实测会查很久不收手'},
    {'id': 'qwen3.8-27b', 'label': 'Qwen3.8-27B', 'group': '通义千问', 'note': '开源尺寸，可内网自部署对照'},
    # 通义千问 · 3.7 / 3.6 / 3.5
    {'id': 'qwen3.7-max', 'label': 'Qwen3.7-Max', 'group': '通义千问（上一代）', 'note': '上一代旗舰'},
    {'id': 'qwen3.7-plus', 'label': 'Qwen3.7-Plus', 'group': '通义千问（上一代）', 'note': '上一代均衡档'},
    {'id': 'qwen3.7-flash', 'label': 'Qwen3.7-Flash', 'group': '通义千问（上一代）', 'note': '上一代轻量'},
    {'id': 'qwen3.6-max-preview', 'label': 'Qwen3.6-Max-Preview', 'group': '通义千问（上一代）', 'note': '更早一代旗舰，可做降级备用'},
    {'id': 'qwen3.6-plus', 'label': 'Qwen3.6-Plus', 'group': '通义千问（上一代）', 'note': ''},
    {'id': 'qwen3.6-flash', 'label': 'Qwen3.6-Flash', 'group': '通义千问（上一代）', 'note': ''},
    {'id': 'qwen3.5-plus', 'label': 'Qwen3.5-Plus', 'group': '通义千问（上一代）', 'note': ''},
    {'id': 'qwen3.5-flash', 'label': 'Qwen3.5-Flash', 'group': '通义千问（上一代）', 'note': ''},
    # 专用
    {'id': 'qwen3-max', 'label': 'Qwen3-Max', 'group': '通义千问（专用）', 'note': '更早旗舰'},
    {'id': 'qwen3-coder-plus', 'label': 'Qwen3-Coder-Plus', 'group': '通义千问（专用）', 'note': '代码向，写 SQL 可能更强'},
    {'id': 'qwen3-vl-plus', 'label': 'Qwen3-VL-Plus', 'group': '通义千问（多模态）', 'note': '能看图，适合带图表的问法'},
    {'id': 'qwen3.5-omni-plus', 'label': 'Qwen3.5-Omni-Plus', 'group': '通义千问（多模态）', 'note': '全模态'},
    # 其它厂商
    {'id': 'deepseek-v4-pro', 'label': 'DeepSeek-V4-Pro', 'group': 'DeepSeek', 'note': '强推理，适合复杂口径推导'},
    {'id': 'deepseek-v4-flash', 'label': 'DeepSeek-V4-Flash', 'group': 'DeepSeek', 'note': 'DeepSeek 轻量档'},
    {'id': 'deepseek-v3.2', 'label': 'DeepSeek-V3.2', 'group': 'DeepSeek', 'note': ''},
    {'id': 'kimi-k3', 'label': 'Kimi-K3', 'group': 'Kimi', 'note': '长上下文强'},
    {'id': 'kimi-k2-thinking', 'label': 'Kimi-K2-Thinking', 'group': 'Kimi', 'note': '思考型；本服务关掉了 thinking，慎用'},
    {'id': 'kimi-k2.7-code', 'label': 'Kimi-K2.7-Code', 'group': 'Kimi', 'note': '代码向'},
    {'id': 'glm-5.2', 'label': 'GLM-5.2', 'group': '智谱 GLM', 'note': ''},
    {'id': 'glm-5', 'label': 'GLM-5', 'group': '智谱 GLM', 'note': ''},
    {'id': 'MiniMax/MiniMax-M3', 'label': 'MiniMax-M3', 'group': 'MiniMax', 'note': ''},
    {'id': 'MiniMax/MiniMax-M2.7', 'label': 'MiniMax-M2.7', 'group': 'MiniMax', 'note': ''},
]
MODEL_IDS = [m['id'] for m in MODELS]

KB_SEARCH_URL = 'https://llm-dk4h22odcm9j1oqk.cn-beijing.maas.aliyuncs.com/api/v1/indices/knowledge/search'
# 专用检索服务：绑定「项目管理秘书口径库 uxht00z9ey」+「ai大脑业务知识库 igjhr8giyb」
# 检索服务：aid-72fa8… 只绑了 r57xtq9ypm（ai大脑通用语义知识库）；aid-c345… 是原先那个（只绑到口径库）。
KB_AGENT_ID = os.environ.get('SECRETARY_KB_AGENT', 'aid-72fa8cae2b124d819617f157e97d0a1d')
# 只检索指定知识库；留空 = 用检索服务默认范围
KB_IDS = [x for x in os.environ.get('SECRETARY_KB_IDS', 'r57xtq9ypm').replace(' ', '').split(',') if x]

# AI 只能查这些表（留空 = 用业务字典里登记的全部）。
# 六项指标已统一到这两张预计算表：快照（本期值）+ 对比（同比/环比）。
TABLES = [x for x in os.environ.get('SECRETARY_TABLES', '').replace(' ', '').split(',') if x]

# 系统提示从 ai_data.ai_prompt 注入（取 IS_new=1 的最新版），不在代码里写死。
# 列表里不存在的 key 会自动跳过，所以可以先接好槽位、等内容补上就自动生效。
#   answer_agent   回答规则（含那 10 条必须遵守）
#   business_rules 共享业务规则（deleted_flag / 单位 / 层级 / 时间处理）
#   sql_plan_rules ←【待他们新增】「先拆解任务、再一次性发出互不依赖的查询」这条约束
#                    （原 `question_splitter` 目前是“原样透传、不做补全”，不能直接用）
PROMPTS = [x for x in os.environ.get('SECRETARY_PROMPTS',
    'answer_agent,business_rules,sql_plan_rules').replace(' ', '').split(',') if x]

# 上游「信息补全」工作流应用（含机构名自动纠错，如 凉水供电所→两水供电所）。
# 出参 = 纠正后的问题 + 统计时间（本月/上月）。本服务在模型循环之前先调它，再叠加知识库补全。
COMPLETION_APP_ID = os.environ.get('SECRETARY_COMPLETION_APP', 'e207644fd37247af957b7fbc613e6efc')
COMPLETION_URL = (LLM_BASE.split('/compatible-mode')[0] +
                  '/api/v1/apps/' + COMPLETION_APP_ID + '/completion')

# 工具调用步数上限：0 = 不设上限（由模型自己决定什么时候收手）
MAX_STEPS = int(os.environ.get('SECRETARY_MAX_STEPS', '0'))
SQL_MAX_ROWS = 200
SQL_TIMEOUT_MS = 25000
HTTP_TIMEOUT = 120
