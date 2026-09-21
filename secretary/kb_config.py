# -*- coding: utf-8 -*-
"""知识库（阿里云百炼）配置。

**复用项目根目录的同一个 `.env`**（和数据库、模型配置在一起），键名用阿里云那套：

    ALIBABA_CLOUD_ACCESS_KEY_ID      RAM 用户的 AccessKey
    ALIBABA_CLOUD_ACCESS_KEY_SECRET
    WORKSPACE_ID                     业务空间 ID，**必须以 llm- 开头**
    DEFAULT_INDEX_ID                 默认知识库 ID（请求里不传 index_id 时用它，可选）

为什么单独一个文件、而不是塞进 config.py：
`kb_bailian.py` 是「云端封装层」，它只认这个 `Settings` 对象、不认识主服务的 config。
这样将来换云 / 换自建 RAG，只动这两个文件，不牵动主服务。
"""
import os
from dataclasses import dataclass, field

try:
    import config as _app_config                 # 复用主服务的 .env 加载（同一份 .env）
    _ENV = getattr(_app_config, 'ENV', {}) or {}
except Exception:                                # noqa: BLE001  单独跑本模块（自测）时兜底
    _ENV = {}

# 知识库功能在中国站**仅支持华北2（北京）**，其他地域不提供服务
DEFAULT_ENDPOINT = 'bailian.cn-beijing.aliyuncs.com'


def _env(key, default=''):
    """取值优先级：真环境变量 > 项目 .env > 默认值。"""
    return (os.environ.get(key) or _ENV.get(key) or default).strip()


def _int(key, default):
    try:
        return int(_env(key, str(default)) or default)
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class Settings:
    # ---- 阿里云凭证（**绝不下发到前端**）----
    access_key_id: str = field(default_factory=lambda: _env('ALIBABA_CLOUD_ACCESS_KEY_ID'))
    access_key_secret: str = field(default_factory=lambda: _env('ALIBABA_CLOUD_ACCESS_KEY_SECRET'))

    # ---- 业务空间 ID，必须以 llm- 开头 ----
    workspace_id: str = field(default_factory=lambda: _env('WORKSPACE_ID'))

    # ---- 可选：默认知识库 ID ----
    default_index_id: str = field(default_factory=lambda: _env('DEFAULT_INDEX_ID'))

    # ---- 接入点 ----
    endpoint: str = field(default_factory=lambda: _env('BAILIAN_ENDPOINT', DEFAULT_ENDPOINT))

    # ---- 上传调参 ----
    # 文件解析是异步的；上传接口会等解析完再提交索引任务，这里控制最长等多久（秒）。
    # 超时**不算失败**，只是先不提交索引任务。
    parse_timeout_seconds: int = field(
        default_factory=lambda: _int('PARSE_TIMEOUT_SECONDS', 300))
    # 单次 HTTP 超时（秒）
    http_timeout_seconds: int = field(
        default_factory=lambda: _int('HTTP_TIMEOUT_SECONDS', 120))

    def missing(self):
        """缺哪些必填项（健康检查用，不抛异常）。"""
        return [name for name, value in (
            ('ALIBABA_CLOUD_ACCESS_KEY_ID', self.access_key_id),
            ('ALIBABA_CLOUD_ACCESS_KEY_SECRET', self.access_key_secret),
            ('WORKSPACE_ID', self.workspace_id)) if not value]

    def validate(self):
        need = self.missing()
        if need:
            raise RuntimeError(
                '缺少知识库配置：' + ', '.join(need)
                + '。请在项目根目录的 .env 里补齐（键名见 .env.example）。')


settings = Settings()
