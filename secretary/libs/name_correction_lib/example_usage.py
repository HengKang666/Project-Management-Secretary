# -*- coding: utf-8 -*-
"""接入示例 —— 直接运行：python example_usage.py

演示两个能力，以及**最关键的一条约定**：合成名要按前缀查。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from name_correction_lib import Corrector   # noqa: E402

# 全局只建一次。构造会加载两万条索引（200~300ms），之后单次调用 1~15ms。
CORRECTOR = Corrector()


def step1_correct(text: str) -> str:
    """第一步：保守纠错 —— 只把确定是错的字改对，不确定的一律不动。"""
    r = CORRECTOR.correct(text)
    for w in r.word_fixes:
        print(f"    词 {w['错误']} → {w['正确']}")
    for n in r.name_fixes:
        print(f"    名 {n['错误']} → {n['正确']}")
    for a in r.areas:
        print(f"    台区「{a['文本']}」→ {a['标准名']}")
    for c in r.need_clarify:
        print(f"    ! 需要反问：{c['提示']}")
    return r.corrected


def step2_resolve(text: str) -> str:
    """第二步：名称归一 —— 把口语名称补成库里的标准名。"""
    r = CORRECTOR.resolve(text)
    for n in r.names:
        d = n.to_dict()
        print(f"    {d['原文']} → {d['标准名']}"
              f"  [{d['匹配方式']}]" + ("" if d.get("库内精确名", True)
                                       else "  ← 库内精确名=false，合成名"))
        if d.get("同名候选数"):
            print(f"      同名候选 {d['同名候选数']} 个，例如："
                  f"{'、'.join(d.get('其他候选', [])[:3])}")
        # ★ 最关键的一条：合成名不能拿去等值匹配
        if not n.exact:
            print(f"      → 转 SQL 必须用前缀匹配：name LIKE '{n.prefix}%'")
    for u in r.unresolved:
        print(f"    ! 库里没有「{u['文本']}」，最接近：{u.get('最接近')}")
    return r.resolved


def pipeline(text: str) -> str:
    """推荐接法：先纠错、再归一。两步用同一个 Corrector 实例。"""
    print(f"  输入：{text}")
    print("  【① 纠错】")
    t = step1_correct(text)
    print(f"    → {t}")
    print("  【② 归一】")
    t = step2_resolve(t)
    print(f"    → {t}")
    return t


if __name__ == "__main__":
    for s in [
        "帮我查一下环谈供电所上个月的台去线损",   # 纯错字
        "公家棚线损",                        # 知道是台区、不知道哪一个 → 合成名
        "公家彭",                            # 同音错字 + 多候选
        "白鹤变压器",                         # 用户说了类型词，就尊重它
        "凉水的线损",                         # 裸地名 → 优先单位名
        "厉山供电所的负荷是多少",               # 已经对了，不该再补一遍
        "情况怎么样",                         # 普通问句，不该动
    ]:
        print("=" * 74)
        pipeline(s)
        print()
