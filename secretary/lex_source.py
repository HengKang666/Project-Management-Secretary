# -*- coding: utf-8 -*-
"""纠错词典的数据库数据源 —— 把 agent_data 的 t_nc_* 表喂给 name_correction_lib。

【它做什么】
    注册表全量读进内存，交给 name_corrector 建索引。
    ★ 只在加载时读一次（懒加载），之后每次纠错都是纯内存运算，**一条 SQL 都不发**。
    数据库是「词典存放在哪」，不是「纠错时要去问谁」—— 这是不牺牲性能的前提：
    实测最坏情况要跑 2223 次 difflib 相似度计算，走库的话 2223 × 43ms ≈ 95 秒，
    而内存索引只要 49ms。

【为什么要有列名映射】
    库表用英文列名（避开保留字、跨工具流转安全），而 name_corrector.py 里
    全是 r["错误写法"] 这种中文取值。映射集中在 SPEC 这一张表里，
    **算法代码一行都不用改**。

【必须守住的语义】表不存在或为空时抛 FileNotFoundError。
    Corrector.__init__ 里 area_core / wordlist 的降级分支靠捕获这个异常工作
    （「没有索引文件就现算」「词表可选」）。抛成 OperationalError 之类会让降级失效、
    服务直接起不来 —— 这是最容易踩的坑。
    但「连不上数据库」要原样抛，那样 /health 才能区分「库连不上」和「表是空的」。

【列宽/口径】py_full / py_strict / initials / core_code 是离线编译产物，
    存进库只是为了省掉启动时的重算（两万条要 4 秒）。本模块原样搬运，绝不重算。
"""
from __future__ import annotations

import pymysql

import config

# CSV 文件名 -> (库表名, {CSV列名: 库列名})
#   key 故意沿用 CSV 文件名，这样 name_corrector 里的 _read("typo.csv") 一个字都不用改。
SPEC = {
    'typo.csv': ('t_nc_typo', {
        '适用组织': 'org', '错误写法': 'wrong', '正确写法': 'right_word',
        '频率等级': 'freq_level', '错因类型': 'reason', '纠正示例': 'sample'}),
    'terms.csv': ('t_nc_term', {
        '术语': 'term', '定义': 'definition', '常见表达': 'alias', '适用组织': 'org'}),
    'ambiguous.csv': ('t_nc_ambiguous', {
        '拼音码': 'py_code', '名称A': 'name_a', '类别A': 'kind_a',
        '名称B': 'name_b', '类别B': 'kind_b', '处理方式': 'action'}),
    'wordlist.csv': ('t_nc_wordlist', {
        '标准词': 'word', '领域': 'domain', '来源': 'src',
        '拼音码': 'py_full', '首字母': 'initials'}),
    'catalog.csv': ('t_nc_catalog', {
        '名称': 'name', '类别': 'kind', '所属': 'owner', '拼音码': 'py_full',
        '拼音码_严': 'py_strict', '首字母': 'initials', '来源ID': 'source_id',
        # 多出来的这一列在 CSV 版的 catalog.csv 里没有（它在 area_core.csv 里），
        # 特意查出来是为了让 area_core.csv 能从同一份结果派生，省一次 2 万行查询。
        # name_corrector 不取这个键，多一个键对它没有任何影响。
        '核心码': 'core_code'}),
    'pinyin_table.csv': ('t_nc_pinyin', {
        '字': 'hanzi', '声母': 'initial', '韵母': 'final'}),
}
# 注意：area_core.csv 不在这里 —— 它由 catalog 派生，见 read()。它不是独立表。

# 词典版本指纹：6 张表的 MAX(update_time) 拼起来。
# 一条 SQL 拿全，不用建版本号表、不用写触发器，人工改表也不会忘记打标记。
# ★ 约定用 deleted_flag 软删除：物理 DELETE 不改 update_time，探测不到。
VERSION_SQL = (
    "SELECT CONCAT_WS('|',"
    " (SELECT IFNULL(MAX(update_time),'1970-01-01') FROM t_nc_typo),"
    " (SELECT IFNULL(MAX(update_time),'1970-01-01') FROM t_nc_term),"
    " (SELECT IFNULL(MAX(update_time),'1970-01-01') FROM t_nc_ambiguous),"
    " (SELECT IFNULL(MAX(update_time),'1970-01-01') FROM t_nc_wordlist),"
    " (SELECT IFNULL(MAX(update_time),'1970-01-01') FROM t_nc_catalog),"
    " (SELECT IFNULL(MAX(update_time),'1970-01-01') FROM t_nc_pinyin)"
    ") AS v"
)

_ERR_NO_TABLE = 1146


class MysqlSource:
    """从 agent_data 的 t_nc_* 表读词典。

    用法：
        src = MysqlSource()
        use_source(src)
        c = Corrector()
        src.close()            # ★ 加载完立刻关连接，不要长期持有

    一个实例只用于「一次加载」：内部缓存不感知外部改动，
    词典更新时请**新建实例 + 重建 Corrector**。
    """

    name = 'mysql'

    def __init__(self, db=None):
        # config.LEXICON_DB 用的是 `database` 键（不是 `db`）：
        # pymysql 1.1+ 已把 db 标为废弃，传进去会刷 DeprecationWarning。
        self._db = dict(db or config.LEXICON_DB)
        self._conn = None
        self._rows = {}

    # -------------------------------------------------- 连接
    def _c(self):
        if self._conn is None:
            self._conn = pymysql.connect(
                cursorclass=pymysql.cursors.DictCursor, charset='utf8mb4',
                connect_timeout=8, read_timeout=60, autocommit=True, **self._db)
        return self._conn

    def close(self):
        conn, self._conn = self._conn, None
        if conn is not None:
            try:
                conn.close()
            except Exception:              # noqa: BLE001
                pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    # -------------------------------------------------- 读表
    def read(self, key: str) -> list:
        """返回一整张词典表，dict 的键名与 CSV 表头完全一致。"""
        hit = self._rows.get(key)
        if hit is not None:
            return hit

        if key == 'area_core.csv':
            # 它没有独立表：实测 20131 个名称 100% 被 catalog 覆盖（差集为 0），
            # 实质只是「台区的核心码」这一列。从 catalog 派生，省一次 2 万行查询。
            # 派生出来会多出五十来条非台区/无核心码的行，name_corrector 里
            # `if not code: continue` 会滤掉，行为与读 CSV 完全一致。
            rows = [{'名称': r['名称'], '核心码': r['核心码']}
                    for r in self.read('catalog.csv')]
        else:
            spec = SPEC.get(key)
            if spec is None:
                raise FileNotFoundError('未知词典：%s' % key)
            table, colmap = spec
            inv = {db: csvk for csvk, db in colmap.items()}
            cols = ', '.join('`%s`' % c for c in colmap.values())
            cur = self._c().cursor()
            try:
                cur.execute('SELECT %s FROM `%s` WHERE `deleted_flag` = 0'
                            % (cols, table))
                raw = cur.fetchall()
            except pymysql.err.ProgrammingError as e:
                # 表不存在 -> 归成 FileNotFoundError，和「CSV 文件缺失」同语义，
                # 这样调用方的降级分支照常生效（否则整个纠错步直接挂掉）。
                if e.args and e.args[0] == _ERR_NO_TABLE:
                    raise FileNotFoundError(
                        '词典表不存在：%s（请先执行 tools/lexicon_schema.sql 建表）'
                        % table) from e
                raise
            finally:
                cur.close()
            if not raw:
                raise FileNotFoundError('词典表为空：%s' % table)
            rows = [{inv[k]: (v if v is not None else '') for k, v in r.items()}
                    for r in raw]

        self._rows[key] = rows
        return rows

    # -------------------------------------------------- 版本探测（热更新用）
    @staticmethod
    def current_version(db=None) -> str:
        """查一次词典版本指纹。查不动就抛异常，由调用方决定是否沿用旧词典。"""
        conn = pymysql.connect(
            cursorclass=pymysql.cursors.DictCursor, charset='utf8mb4',
            connect_timeout=8, read_timeout=20, autocommit=True,
            **(db or config.LEXICON_DB))
        try:
            cur = conn.cursor()
            cur.execute(VERSION_SQL)
            row = cur.fetchone()
            cur.close()
            return row['v'] if row else ''
        finally:
            conn.close()
