# -*- coding: utf-8 -*-
"""名称纠错 / 名称归一库 —— 中文实体名的确定性处理。

    from name_correction_lib import Corrector

    c = Corrector()                                  # 全局建一次，常驻复用

    c.correct("白鹤2组台区线损").corrected            # 白鹤二组公变线损（保守纠错）
    c.resolve("公家棚线损").resolved                  # 龚家棚台区线损（名称归一）

首次构造会加载约 2 万条词典索引（200~300ms），
建议全局只建一个实例、常驻复用（无状态，可安全并发）。

词典默认从同目录 data/ 下的 CSV 读。也可以搬进数据库：
    from name_correction_lib import use_source, Corrector
    use_source(MysqlSource())        # 需实现 read(key) -> list[dict]
    c = Corrector()                  # ★ 换源后必须重新构造
数据库实现见服务层的 secretary/lex_source.py。
"""
from .name_corrector import (
    Correction,
    Corrector,
    NameResolution,
    Resolution,
    data_source_name,
    describe_data,
    use_source,
)

__all__ = [
    "Corrector",
    "Correction",       # correct() 的返回：保守纠错
    "Resolution",       # resolve() 的返回：名称归一
    "NameResolution",
    "describe_data",
    "use_source",       # 注入数据库数据源
    "data_source_name",
]
