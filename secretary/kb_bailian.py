"""阿里云百炼知识库 OpenAPI 封装层（极简版）。

设计原则：
1. **这一层是唯一接触阿里云 SDK 的地方**。上层业务永远不 import alibabacloud_*。
   将来要换成别的云 / 自建 RAG，只改这一个文件。
2. 返回值统一拍平成普通 dict，字段名固定，不让 SDK 对象泄漏到上层。
3. 字段读取走 `_pick()` 做**大小写不敏感**匹配 —— Darabonba 生成的 SDK 在不同版本里
   `to_map()` 的 key 大小写不完全一致，写死 "FileId" 很容易踩坑。
4. SDK 的两种失败形态都要接住（见 `_translate_sdk_exception`）。
5. **不做任何自动重试**。因为本模块用到的提交类接口（ApplyFileUploadLease /
   SubmitIndexAddDocumentsJob）都**不幂等**，重试会产生重复数据。
"""
from __future__ import annotations

import hashlib
import re
import time
from pathlib import Path
from typing import Any, Sequence

import requests
from alibabacloud_bailian20231229 import models as bl
from alibabacloud_bailian20231229.client import Client as BailianSdkClient
from alibabacloud_tea_openapi import exceptions as tea_exceptions
from alibabacloud_tea_openapi import models as open_api_models
from alibabacloud_tea_util import models as util_models
from alibabacloud_tea_util.client import Client as UtilClient

from kb_config import Settings

# 单文件上传体积上限（ApplyFileUploadLease 规定 1B ~ 100MB）
MAX_UPLOAD_BYTES = 100 * 1024 * 1024

# 文件解析成功 / 索引构建成功的状态值
PARSE_OK = ("PARSE_SUCCESS", "SUCCESS", "FINISH")
PARSE_FAIL = ("PARSE_FAILED", "FAILED", "ERROR")


NOT_AUTHORIZED_HINT = """
云端拒绝访问该业务空间。按顺序排查（第 1 条命中率最高）：

【1】RAM 子账号没有加入百炼业务空间  ★最常见
    关键认知：**阿里云 RAM 权限与百炼"业务空间成员"是两套独立的权限体系。**
    只授予 AliyunBailianDataFullAccess 策略是不够的，必须再把它加进业务空间。
    操作（用**主账号**登录百炼控制台）：
      左下角【权限管理】→【用户管理】→【新增用户】
      → 类型选「RAM 用户」→ 选中子账号 → 保存并继续配置权限
      → 勾选目标业务空间，角色选「管理员」→ 确定
    （2025-08-06 起百炼收回了子账号的默认高级权限，必须显式赋权）

【2】WORKSPACE_ID 填错
    正确格式形如 llm-3z7uw7fwz0vexxxx，**必须以 `llm-` 开头**。
    获取：百炼控制台左上角切换业务空间处，或右上角【设置】→【账号管理】。

【3】AK/SK 与业务空间不属于同一个阿里云主账号。

【4】控制台右上角地域不是「华北2（北京）」——知识库仅北京地域提供。

【5】该业务空间已被删除。

最快的验证方式：临时换成**主账号**的 AK/SK（主账号可直接调用，无须加入业务空间）。
通了 → 就是子账号权限问题；不通过 → 一定是 WORKSPACE_ID 填错。
""".strip()


class BailianError(RuntimeError):
    """把云端错误包装成异常，并判定**是否值得重试**。

    百炼的授权错误是永久性的，重试一万次也不会好，只会白刷日志、白耗限流额度。
    """

    RETRYABLE_KEYWORDS = (
        "throttl", "timeout", "timed out", "internalerror", "serviceunavailable",
        "systemerror", "servererror", "connection", "reset by peer", "try again",
    )
    PERMANENT_KEYWORDS = (
        "not authorized", "unauthorized", "forbidden", "access denied",
        "invalidapi", "invalid parameter", "invalidparameter", "missing or invalid",
        "signature", "does not exist", "not found", "no permission",
    )

    def __init__(
        self,
        code: str,
        message: str,
        request_id: str = "",
        retryable: bool | None = None,
        http_status: int | None = None,
    ):
        self.code = code or "Unknown"
        self.message = message or ""
        self.request_id = request_id or ""
        self.http_status = http_status
        self.retryable = self._classify(f"{self.code} {self.message}") if retryable is None else retryable
        self.hint = self._build_hint()
        super().__init__(f"[{self.code}] {self.message}")

    @classmethod
    def _classify(cls, text: str) -> bool:
        low = text.lower()
        if any(k in low for k in cls.PERMANENT_KEYWORDS):
            return False
        if any(k in low for k in cls.RETRYABLE_KEYWORDS):
            return True
        return True

    def _build_hint(self) -> str:
        low = f"{self.code} {self.message}".lower()
        if "not authorized" in low or "access denied" in low or "unauthorized" in low:
            return NOT_AUTHORIZED_HINT
        if "invalidaccesskey" in low or "signature" in low:
            return (
                "AK/SK 没有通过阿里云鉴权。确认：① 是否复制完整（Secret 只在创建时显示一次）；"
                "② .env 里有没有多余空格换行；③ 这对 AK 是否属于当前账号且未被禁用。"
            )
        if "throttl" in low:
            return "触发云端限流。稍后重试；如果频繁出现，把调用间隔调大。"
        if "invalidparameter" in low or "invalid parameter" in low or "missing or invalid" in low:
            return (
                "参数不合法。常见原因：① SourceType 必传；"
                "② ChunkSize < 100 时必须同时给 OverlapSize；③ OverlapSize 必须小于 ChunkSize。"
            )
        return ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "request_id": self.request_id,
            "retryable": self.retryable,
            "hint": self.hint,
        }


def _translate_sdk_exception(exc: Exception) -> BailianError:
    """把 SDK 抛出的异常翻译成 BailianError。

    **阿里云 SDK 有两种失败形态，必须都接住：**
      A. HTTP 200，但响应体里 `Success=false`  → 业务错误，走 _unwrap()
      B. HTTP 4xx/5xx                          → SDK **直接抛异常，不返回响应对象**
         例：AccessKey 不存在 → `InvalidAccessKeyId.NotFound`（status 404）

    只处理 A 不处理 B，后果就是"配置填错了却抛出 500 内部堆栈"，使用者完全看不出原因。
    """
    if isinstance(exc, BailianError):
        return exc

    if isinstance(exc, (tea_exceptions.ThrottlingException, tea_exceptions.ServerException)):
        return BailianError(
            getattr(exc, "code", None) or type(exc).__name__,
            getattr(exc, "message", None) or str(exc),
            getattr(exc, "request_id", "") or "",
            retryable=True,
        )

    if isinstance(exc, tea_exceptions.ClientException):
        code = getattr(exc, "code", None) or "ClientException"
        message = getattr(exc, "message", None) or str(exc)
        request_id = getattr(exc, "request_id", "") or ""
        status = getattr(exc, "status_code", None)

        # SDK 的 message 里塞了 "code: 404, xxx request id: yyy Response: {...}" 一大坨，
        # 截掉尾部，只留人类可读的那一句。
        for marker in (" request id:", " Response:", "\n"):
            cut = message.find(marker)
            if cut > 0:
                message = message[:cut]
        message = re.sub(r"^code:\s*\d+,\s*", "", message.strip())
        if len(message) > 300:
            message = message[:300] + "…"

        retryable: bool | None = None
        if isinstance(status, int):
            retryable = True if (status == 429 or status >= 500) else (False if status >= 400 else None)

        detail = f"HTTP {status} · {message}" if status is not None else message
        return BailianError(code, detail, request_id, retryable=retryable, http_status=status)

    # 网络层异常（requests / urllib3 / socket）→ 瞬时
    return BailianError(type(exc).__name__, str(exc), "", retryable=True)


class _SafeSdkClient:
    """SDK 客户端的安全代理：保证**任何**调用抛出的异常都被翻译。

    为什么用代理而不是在每个调用点写 try/except？
    调用点有十几个，**只要漏掉一个就会出现"未捕获异常 → 500 堆栈"**。
    用代理能从结构上杜绝这种遗漏。
    """

    def __init__(self, inner: BailianSdkClient):
        self.inner = inner

    def __getattr__(self, name: str):
        attr = getattr(self.inner, name)
        if not callable(attr):
            return attr

        def guarded(*args: Any, **kwargs: Any):
            try:
                return attr(*args, **kwargs)
            except Exception as exc:  # noqa: BLE001 —— 必须全接住，翻译后再抛
                raise _translate_sdk_exception(exc) from exc

        return guarded


# --------------------------------------------------------------------------
# 工具函数
# --------------------------------------------------------------------------
def md5_of_file(file_path: str | Path, chunk_size: int = 4096) -> str:
    h = hashlib.md5()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(chunk_size), b""):
            h.update(chunk)
    return h.hexdigest()


def md5_of_bytes(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()


def _pick(data: Any, *names: str, default: Any = None) -> Any:
    """大小写不敏感地取字段。`_pick(d, "FileId", "file_id")`。"""
    if data is None:
        return default
    if isinstance(data, dict):
        lowered = {str(k).lower(): v for k, v in data.items()}
    else:
        try:
            lowered = {str(k).lower(): v for k, v in UtilClient.to_map(data).items()}
        except Exception:
            return default
    for name in names:
        hit = lowered.get(name.lower())
        if hit is not None:
            return hit
    return default


def _rows(data: Any, *names: str) -> list:
    value = _pick(data, *names, default=[])
    return list(value) if isinstance(value, (list, tuple)) else []


class BailianKnowledgeBase:
    """百炼知识库客户端（极简版）。

    典型用法::

        kb = BailianKnowledgeBase(settings)
        kb.list_indices()                                  # 1. 知识库列表
        kb.get_index_detail("o95n15b77j")                  # 2. 知识库详情
        kb.list_index_documents("o95n15b77j")              # 3. 知识库里的文档
        kb.upload_document("o95n15b77j", "手册.pdf", file_path="./手册.pdf")   # 4. 上传
        kb.delete_index_document("o95n15b77j", ["file_xxx"])                   # 5. 删除
    """

    def __init__(self, settings: Settings):
        config = open_api_models.Config(
            access_key_id=settings.access_key_id,
            access_key_secret=settings.access_key_secret,
        )
        config.endpoint = settings.endpoint
        # 注意：不开启 SDK 的 autoretry —— 本模块的提交类接口不幂等，自动重试会产生重复数据
        self.client = _SafeSdkClient(BailianSdkClient(config))
        self.settings = settings
        self.workspace_id = settings.workspace_id
        self._runtime = util_models.RuntimeOptions()
        self._runtime.read_timeout = settings.http_timeout_seconds * 1000
        self._runtime.connect_timeout = settings.http_timeout_seconds * 1000

    # ==================================================================
    # 内部
    # ==================================================================
    def _unwrap(self, resp: Any) -> dict:
        """接住 SDK 的失败形态 A：HTTP 200 但 Success=false。"""
        body = resp.body
        if _pick(body, "Success", "success", default=True) is False:
            raise BailianError(
                str(_pick(body, "Code", "code", default="")),
                str(_pick(body, "Message", "message", default="")),
                str(_pick(body, "RequestId", "request_id", default="")),
            )
        data = _pick(body, "Data", "data", default={})
        if isinstance(data, dict):
            return data
        try:
            return UtilClient.to_map(data) or {}
        except Exception:
            return {}

    # ==================================================================
    # 连通性自检
    # ==================================================================
    def health_check(self) -> dict:
        """分层自检：① AK/SK 是否有效 ② 是否有该业务空间的访问权。

        为什么要分两层：`ListIndices` 是百炼里少数**不校验业务空间成员身份**的接口，
        所以会出现"它返回 200 但 index_total=0，紧接着所有写操作全 NOT AUTHORIZED"
        这种极具迷惑性的现象。用 `ListCategory` 做第二层探针才能区分开。
        """
        report: dict[str, Any] = {
            "ok": False, "credential_ok": False, "workspace_access_ok": False,
            "workspace_id": self.workspace_id, "endpoint": self.settings.endpoint,
            "index_total": 0, "indices": [], "diagnosis": "", "hint": "",
        }

        try:
            page = self.list_indices(page_size=50)
        except BailianError as exc:
            report["diagnosis"] = "credentials_invalid"
            report["error"] = exc.to_dict()
            report["hint"] = (
                "AK/SK 本身无法通过鉴权。检查是否复制完整、是否与当前阿里云账号一致。"
            )
            return report

        report["credential_ok"] = True
        report["index_total"] = page["total_count"]
        report["indices"] = page["indices"]

        try:
            self.list_categories(max_results=5)
            report["workspace_access_ok"] = True
            report["ok"] = True
            report["diagnosis"] = "healthy" if report["index_total"] else "no_indices"
            if not report["index_total"]:
                report["hint"] = "凭证和空间权限都正常，但该业务空间下还没有知识库。"
        except BailianError as exc:
            report["error"] = exc.to_dict()
            report["diagnosis"] = "workspace_forbidden" if not exc.retryable else "transient_error"
            report["hint"] = exc.hint or NOT_AUTHORIZED_HINT
        return report

    def list_categories(self, max_results: int = 50,
                        category_type: str = "UNSTRUCTURED") -> list[dict]:
        """列出数据连接的类目。**同时可作为"业务空间访问权"的探针。**

        拿它当探针的原因：只读、开销极小、且**必须通过业务空间校验**。

        ★ `CategoryType` 是**必填**：不传会被云端拒绝（400 `MissingCategoryType`）。
        参考实现原先漏了这个字段 —— 症状极具迷惑性：**凭证正常、知识库列表也拉得出来
        （index_total>0），但 health_check 的 workspace_access_ok 判成 false**，
        即探针自己坏掉造成的**假阴性**。实测踩到，已在此修掉。
        """
        request = bl.ListCategoryRequest(max_results=max_results,
                                         category_type=category_type)
        resp = self.client.list_category_with_options(self.workspace_id, request, {}, self._runtime)
        data = self._unwrap(resp)
        return [
            {
                "category_id": _pick(row, "CategoryId", "category_id"),
                "name": _pick(row, "CategoryName", "category_name"),
            }
            for row in _rows(data, "CategoryList", "Categories", "category_list")
        ]

    # ==================================================================
    # 1. 查看知识库（列表）
    # ==================================================================
    def list_indices(
        self,
        name: str | None = None,
        page_number: int = 1,
        page_size: int = 20,
    ) -> dict:
        """知识库列表。`name` 可传知识库名称做精确查找。"""
        request = bl.ListIndicesRequest(
            index_name=name,
            page_number=str(page_number),
            page_size=str(page_size),
        )
        resp = self.client.list_indices_with_options(self.workspace_id, request, {}, self._runtime)
        data = self._unwrap(resp)
        indices = [self._shape_index(row) for row in _rows(data, "Indices", "indices")]
        return {
            "total_count": int(_pick(data, "TotalCount", "total_count", default=len(indices)) or 0),
            "page_number": int(_pick(data, "PageNumber", "page_number", default=page_number) or 1),
            "page_size": int(_pick(data, "PageSize", "page_size", default=page_size) or page_size),
            "indices": indices,
        }

    @staticmethod
    def _shape_index(row: Any) -> dict:
        return {
            "index_id": _pick(row, "Id", "id", "index_id"),
            "name": _pick(row, "Name", "name"),
            "description": _pick(row, "Description", "description"),
            "structure_type": _pick(row, "StructureType", "structure_type"),
            "source_type": _pick(row, "SourceType", "source_type"),
            "embedding_model": _pick(row, "EmbeddingModelName", "embedding_model_name"),
            "rerank_model": _pick(row, "RerankModelName", "rerank_model_name"),
            "rerank_min_score": _pick(row, "RerankMinScore", "rerank_min_score"),
            "chunk_size": _pick(row, "ChunkSize", "chunk_size"),
            "overlap_size": _pick(row, "OverlapSize", "overlap_size"),
            "separator": _pick(row, "Separator", "separator"),
            "enable_rewrite": _pick(row, "EnableRewrite", "enable_rewrite"),
        }

    def find_index_by_id(self, index_id: str, max_pages: int = 10, page_size: int = 100) -> dict | None:
        """按 ID 在列表里翻页找某个知识库。

        为什么不用一个接口直接查：百炼**没有 GetIndex 接口**，
        `ListIndices` 只支持按名称过滤。所以只能翻页。
        """
        for page in range(1, max_pages + 1):
            result = self.list_indices(page_number=page, page_size=page_size)
            for item in result["indices"]:
                if item["index_id"] == index_id:
                    return item
            if len(result["indices"]) < page_size:
                break
        return None

    # ==================================================================
    # 2. 知识库详情
    # ==================================================================
    def get_index_detail(self, index_id: str, with_monitor: bool = True) -> dict:
        """知识库详情。

        百炼没有 GetIndex 接口，所以详情是**组合**出来的：
          ① `ListIndices` 翻页找到该知识库的配置
          ② `ListIndexDocuments` 按状态分别统计文档数
          ③ `GetIndexMonitor` 取存储用量（可选，失败不影响整体）
        """
        index = self.find_index_by_id(index_id)
        if index is None:
            raise BailianError(
                "Index.NotFound",
                f"在业务空间 {self.workspace_id} 下找不到知识库 {index_id}。"
                "可能该 ID 填错，或它属于另一个业务空间。",
                retryable=False,
            )

        stats = self.count_documents_by_status(index_id)
        detail = {
            **index,
            # 统计失败的项会返回 -1，不能计进总数
            "document_total": sum(v for v in stats.values() if v >= 0),
            "document_stats": stats,
        }
        if with_monitor:
            # 监控数据拿不到不该让整个详情接口失败，所以单独 try
            try:
                detail["monitor"] = self.get_index_monitor(index_id)
            except BailianError as exc:
                detail["monitor"] = {"error": exc.to_dict()}
        return detail

    def count_documents_by_status(self, index_id: str) -> dict[str, int]:
        """按状态统计文档数。

        小技巧：`ListIndexDocuments` 支持 `DocumentStatus` 过滤，
        每次只要 `page_size=1` 读它的 `TotalCount` 即可 ——
        比把所有文档拉回来再在本地数要省得多。
        """
        stats: dict[str, int] = {}
        for status in ("FINISH", "RUNNING", "INSERT_ERROR", "DELETED"):
            try:
                page = self.list_index_documents(index_id, status=status, page_size=1)
                stats[status.lower()] = page["total_count"]
            except BailianError:
                stats[status.lower()] = -1  # -1 表示本次统计失败
        return stats

    def get_index_monitor(self, index_id: str, days: int = 7) -> dict:
        """知识库监控：存储用量 + 检索 QPS。"""
        now_ms = int(time.time() * 1000)
        request = bl.GetIndexMonitorRequest(
            index_id=index_id,
            start_timestamp=now_ms - days * 24 * 3600 * 1000,
            end_timestamp=now_ms,
        )
        resp = self.client.get_index_monitor_with_options(self.workspace_id, request, {}, self._runtime)
        data = self._unwrap(resp)
        return {
            "storage": _pick(data, "StorageMonitor", "storage_monitor", default=None),
            "retrieve": _pick(data, "RetrieveMonitor", "retrieve_monitor", default=None),
            "raw": data if not isinstance(data, dict) else None,
        }

    # ==================================================================
    # 3. 知识库里的文档
    # ==================================================================
    def list_index_documents(
        self,
        index_id: str,
        status: str | None = None,
        name: str | None = None,
        name_like: bool = False,
        page_number: int = 1,
        page_size: int = 20,
    ) -> dict:
        """知识库下的文档列表。

        `status` 可选：`FINISH`（导入成功）/ `RUNNING`（索引构建中）/
        `INSERT_ERROR`（导入失败）/ `DELETED` / `PARSE_FAILED` / `DOC_PARSING`
        """
        request = bl.ListIndexDocumentsRequest(
            index_id=index_id,
            document_status=status,
            document_name=name,
            enable_name_like=name_like,
            page_number=page_number,
            page_size=page_size,
        )
        resp = self.client.list_index_documents_with_options(
            self.workspace_id, request, {}, self._runtime
        )
        data = self._unwrap(resp)
        documents = []
        for row in _rows(data, "Documents", "documents"):
            documents.append({
                "file_id": _pick(row, "Id", "id"),
                "name": _pick(row, "Name", "name"),
                "size": _pick(row, "Size", "size"),
                "document_type": _pick(row, "DocumentType", "document_type"),
                "status": _pick(row, "Status", "status"),
                "code": _pick(row, "Code", "code"),
                "message": _pick(row, "Message", "message"),
                "gmt_modified": _pick(row, "GmtModified", "gmt_modified"),
                "category_id": _pick(row, "SourceId", "source_id"),
            })
        return {
            "total_count": int(_pick(data, "TotalCount", "total_count", default=len(documents)) or 0),
            "page_number": int(_pick(data, "PageNumber", "page_number", default=page_number) or 1),
            "page_size": int(_pick(data, "PageSize", "page_size", default=page_size) or page_size),
            "documents": documents,
        }

    # ==================================================================
    # 4. 上传文档到知识库
    # ==================================================================
    def upload_document(
        self,
        index_id: str,
        file_name: str,
        file_bytes: bytes | None = None,
        file_path: str | Path | None = None,
        category_id: str = "default",
        parser: str = "DASHSCOPE_DOCMIND",
        wait_parse: bool = True,
        submit_index: bool = True,
        skip_if_exists: bool = True,
    ) -> dict:
        """上传文档到指定知识库。**一个方法走完整条链路。**

        完整链路（官方标准流程）：
            ① ApplyFileUploadLease  申请上传租约（预签名 URL）
            ② HTTP PUT              上传文件二进制
            ③ AddFile               登记到"数据连接"，拿到 fileId
            ④ DescribeFile 轮询     等云端解析完成（PARSE_SUCCESS）
            ⑤ SubmitIndexAddDocumentsJob  提交索引任务（异步构建，可能耗时数小时）

        关于 ④⑤：`SubmitIndexAddDocumentsJob` 的语义是"追加导入**已解析**的文件"，
        所以必须先等解析完成。解析小文件通常几十秒。
        `wait_parse=False` 可跳过等待（只做 ①②③），之后自行处理。

        参数
        ----
        file_bytes / file_path : 二选一。二选一时另一个传 None。
        wait_parse   : 是否等解析完成（默认 True）
        submit_index : 是否提交索引任务（默认 True，需要 wait_parse=True）
        skip_if_exists : 按**文件名**做一次去重检查（尽力而为，不是内容级去重）

        返回
        ----
        {file_id, job_id, name, size, md5, parse_status, index_submitted, skipped, message}
        """
        # 取出字节内容 + 基本信息
        if file_bytes is None:
            if file_path is None:
                raise ValueError("file_bytes 和 file_path 至少要提供一个")
            path = Path(file_path)
            if not path.is_file():
                raise FileNotFoundError(f"文件不存在：{path}")
            file_bytes = path.read_bytes()
            file_name = file_name or path.name

        if not file_name:
            raise ValueError("必须提供 file_name（百炼要求带后缀，如 手册.pdf）")

        size = len(file_bytes)
        if not (1 <= size <= MAX_UPLOAD_BYTES):
            raise ValueError(f"文件大小 {size} 字节超出允许范围 1B ~ 100MB")

        result: dict[str, Any] = {
            "file_id": None, "job_id": None, "name": file_name, "size": size,
            "md5": md5_of_bytes(file_bytes), "parse_status": None,
            "index_submitted": False, "skipped": False, "message": "",
        }

        # ---- 尽力而为的去重：同名且已在索引中，就不重复上传 ----
        # 注意：百炼的提交类接口都**不幂等**，重复上传会产生重复切片，
        # 所以这里挡一道。但它是按文件名判断的，不是内容指纹，只能算"尽力而为"。
        if skip_if_exists:
            existing = self._find_existing_document(index_id, file_name)
            if existing:
                result.update({
                    "file_id": existing["file_id"],
                    "skipped": True,
                    "parse_status": existing["status"],
                    "index_submitted": existing["status"] == "FINISH",
                    "message": f"知识库中已存在同名文档（状态 {existing['status']}），本次跳过上传。",
                })
                return result

        # ---- ① 申请上传租约 ----
        lease = self._apply_file_upload_lease(category_id, file_name, result["md5"], size)

        # ---- ② 上传文件二进制 ----
        self._put_to_lease(lease, file_bytes)

        # ---- ③ 登记文件 → fileId ----
        file_id = self._add_file(lease["lease_id"], category_id, parser)
        result["file_id"] = file_id

        # ---- ④ 等解析完成 ----
        if not wait_parse:
            result.update({"parse_status": "PARSING",
                           "message": "文件已上传并登记（解析中）。未提交索引任务。"})
            return result

        try:
            result["parse_status"] = self._wait_file_parsed(
                file_id, timeout=self.settings.parse_timeout_seconds
            )
        except TimeoutError as exc:
            result.update({"parse_status": "PARSING", "message": str(exc)})
            return result

        # ---- ⑤ 提交索引任务 ----
        if submit_index:
            result["job_id"] = self._submit_add_documents_job(index_id, [file_id])
            result["index_submitted"] = True
            result["message"] = (
                "已提交索引任务。索引在云端异步构建，高峰期可能耗时数小时；"
                "可调用 list_index_documents 查看状态（RUNNING → FINISH）。"
            )
        return result

    def upload_local_file(self, index_id: str, file_path: str | Path, **kwargs: Any) -> dict:
        """按本地路径上传（CLI / 批处理场景的便捷写法）。"""
        return self.upload_document(index_id, "", file_path=file_path, **kwargs)

    def _find_existing_document(self, index_id: str, file_name: str) -> dict | None:
        """按文件名（不含后缀）精确查一下知识库里有没有同名文档。"""
        stem = Path(file_name).stem
        if not stem:
            return None
        try:
            page = self.list_index_documents(
                index_id, name=stem, name_like=False, page_size=10
            )
        except BailianError:
            return None   # 查询失败不该阻断上传
        for doc in page["documents"]:
            if doc["status"] in ("FINISH", "RUNNING"):
                return doc
        return None

    def _apply_file_upload_lease(
        self, category_id: str, file_name: str, file_md5: str, size: int
    ) -> dict:
        request = bl.ApplyFileUploadLeaseRequest(
            file_name=file_name,
            md_5=file_md5,
            size_in_bytes=str(size),
            category_type="UNSTRUCTURED",
        )
        # ⚠️ 注意参数顺序：category_id 在 workspace_id **前面**
        resp = self.client.apply_file_upload_lease_with_options(
            category_id, self.workspace_id, request, {}, self._runtime
        )
        data = self._unwrap(resp)
        param = _pick(data, "Param", "param", default={}) or {}
        lease_id = _pick(data, "FileUploadLeaseId", "file_upload_lease_id")
        url = _pick(param, "Url", "url")
        if not (lease_id and url):
            raise BailianError("ApplyFileUploadLease.NoLease", "未获取到上传租约")
        return {
            "lease_id": lease_id,
            "url": url,
            "headers": _pick(param, "Headers", "headers", default={}),
            "method": str(_pick(param, "Method", "method", default="PUT")).upper(),
        }

    @staticmethod
    def _normalize_headers(raw: Any) -> dict:
        """租约返回的 Header 可能是 dict，也可能是 'K':"V",\\n"K2":"V2" 形式的字符串，两种都兼容。

        另外：Content-Type 可能返回空值，**空值必须剔除** ——
        否则 requests 会带上一个空的 Content-Type，导致 OSS 预签名校验失败。
        """
        headers: dict[str, str] = {}
        if isinstance(raw, dict):
            headers = {str(k): ("" if v is None else str(v)) for k, v in raw.items()}
        elif isinstance(raw, str):
            for k, v in re.findall(r'"([^"]+)"\s*:\s*"([^"]*)"', raw):
                headers[k] = v
        else:
            try:
                headers = {str(k): str(v) for k, v in dict(raw).items()}
            except Exception:
                headers = {}
        if not headers.get("Content-Type"):
            headers.pop("Content-Type", None)
        return headers

    def _put_to_lease(self, lease: dict, file_bytes: bytes) -> None:
        """按租约上传。**必须二进制上传，不能用 FormData。**"""
        response = requests.request(
            lease.get("method", "PUT"),
            lease["url"],
            data=file_bytes,
            headers=self._normalize_headers(lease.get("headers")),
            timeout=max(self.settings.http_timeout_seconds, 300),
        )
        if response.status_code >= 400:
            raise BailianError(
                f"UploadHTTP{response.status_code}",
                f"文件上传到预签名 URL 失败：{response.text[:300]}",
                retryable=response.status_code >= 500,
                http_status=response.status_code,
            )

    def _add_file(self, lease_id: str, category_id: str, parser: str) -> str:
        request = bl.AddFileRequest(lease_id=lease_id, parser=parser, category_id=category_id)
        resp = self.client.add_file_with_options(self.workspace_id, request, {}, self._runtime)
        data = self._unwrap(resp)
        file_id = _pick(data, "FileId", "file_id")
        if not file_id:
            raise BailianError("AddFile.NoFileId", "AddFile 未返回 fileId")
        return str(file_id)

    def describe_file(self, file_id: str) -> dict:
        """查询文件解析状态。状态：INIT → PARSING → PARSE_SUCCESS。"""
        resp = self.client.describe_file_with_options(
            self.workspace_id, file_id, bl.DescribeFileRequest(), {}, self._runtime
        )
        data = self._unwrap(resp)
        return {
            "file_id": file_id,
            "status": str(_pick(data, "Status", "status", default="")).upper(),
            "message": _pick(data, "Message", "message"),
        }

    def _wait_file_parsed(self, file_id: str, timeout: int = 300, interval: int = 5) -> str:
        deadline = time.time() + timeout
        last = ""
        while time.time() < deadline:
            info = self.describe_file(file_id)
            last = info["status"]
            if last in PARSE_OK:
                return last
            if last in PARSE_FAIL:
                raise BailianError(
                    "FileParseFailed",
                    f"文件解析失败（状态 {last}）：{info.get('message') or ''}",
                    retryable=False,
                )
            time.sleep(interval)
        raise TimeoutError(
            f"文件已上传，但解析在 {timeout} 秒内未完成（最后状态 {last}）。"
            "这不代表失败 —— 稍后用 list_index_documents 查看即可；"
            "若想自动等待更久，调大 .env 里的 PARSE_TIMEOUT_SECONDS。"
        )

    def _submit_add_documents_job(
        self, index_id: str, document_ids: Sequence[str], chunk_size: int | None = None,
        overlap_size: int | None = None,
    ) -> str:
        """提交索引任务。**不幂等**，调用前请确认没有重复提交。"""
        request = bl.SubmitIndexAddDocumentsJobRequest(
            index_id=index_id,
            document_ids=list(document_ids),
            source_type="DATA_CENTER_FILE",
            chunk_size=chunk_size,
            overlap_size=overlap_size,
        )
        resp = self.client.submit_index_add_documents_job_with_options(
            self.workspace_id, request, {}, self._runtime
        )
        data = self._unwrap(resp)
        return str(_pick(data, "Id", "id", default=""))

    def get_index_job_status(self, index_id: str, job_id: str) -> dict:
        """查询索引任务状态。⚠️ 限流 20 次/分钟，别高频轮询。

        普通场景不需要调它 —— 直接看 `list_index_documents` 的 Status 更省事。
        """
        request = bl.GetIndexJobStatusRequest(index_id=index_id, job_id=job_id)
        resp = self.client.get_index_job_status_with_options(
            self.workspace_id, request, {}, self._runtime
        )
        data = self._unwrap(resp)
        return {
            "job_id": job_id,
            "status": str(_pick(data, "Status", "status", default="")).upper(),
            "documents": [
                {
                    "file_id": _pick(row, "Id", "id"),
                    "name": _pick(row, "Name", "name"),
                    "status": _pick(row, "Status", "status"),
                }
                for row in _rows(data, "Documents", "documents")
            ],
        }

    # ==================================================================
    # 5. 删除知识库文档
    # ==================================================================
    def delete_index_document(self, index_id: str, file_ids: Sequence[str] | str) -> list[str]:
        """从知识库删除文档。返回已成功删除的 file_id 列表。

        三条规则务必知道：
        1. **仅能删除状态为 `FINISH` 或 `INSERT_ERROR` 的文档**（RUNNING 中的删不掉）
        2. **删除不可逆**，Retrieve 立刻召回不到
        3. **不会删除"数据连接"里的源文件** —— 存储占用还在。
           要彻底清理需另外调用数据连接的删除接口（本极简版未包含）。
        """
        ids = [file_ids] if isinstance(file_ids, str) else list(file_ids)
        if not ids:
            return []
        request = bl.DeleteIndexDocumentRequest(index_id=index_id, document_ids=ids)
        resp = self.client.delete_index_document_with_options(
            self.workspace_id, request, {}, self._runtime
        )
        data = self._unwrap(resp)
        deleted = _pick(data, "DeletedDocument", "deleted_document", default=[])
        return list(deleted) if isinstance(deleted, (list, tuple)) else []
