# -*- coding: utf-8 -*-
"""
名称纠错核心（自包含模块）。

**它本身不依赖云** —— 只是一个普通的 Python 类：
    from name_corrector import Corrector
    result = Corrector().correct("白鹤2组台区线损")   # -> "白鹤二组公变线损"

阿里云函数计算只是它的一个「壳」：main.py 里的 handler(event, context)
把事件转成文本、调用 correct()、再把结果序列化。同一份代码可以直接：
  · 当库调用（拷 name_corrector.py + data/ 进项目即可，**不需要装任何第三方包**）
  · 当脚本跑（python main.py 有内置自测）
  · 上传 ZIP 当 FC 事件函数

依赖：**零第三方依赖，只用标准库**。
         拼音靠 data/pinyin_table.csv 这张离线生成的表查（见 tools/18_build_pinyin_table.py），
         相似度用 difflib.SequenceMatcher。
       —— 早先依赖 pypinyin，为了能进「标准库实现、零额外依赖」的项目而去掉了。

性能关键：索引**直接读 CSV 里预计算好的「拼音码 / 核心码」列**，不重算两万条词典
  → 冷启动从 5.4 秒降到 0.2 秒，这是能上 FC 的关键，也是能当库用的关键。

数据放在同目录 data/ 下。
"""
from __future__ import annotations

import csv
import re
import unicodedata
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path

# ---- 知识库文件名一律使用 ASCII，避免中文文件名在 ZIP 解压环节被弄坏 ----
PINYIN_TABLE = "pinyin_table.csv"   # 汉字 → (声母, 韵母)，离线生成
CATALOG = "catalog.csv"        # 标准名词典
AREA_INDEX = "area_core.csv"   # 台区核心码（离线预计算，见 tools/15_add_area_core.py）
TYPO = "typo.csv"              # 同音易错字对照表 v2
RULES = "rules.csv"            # 规则表
AMBIGUOUS = "ambiguous.csv"    # 同音歧义名对
TERMS = "terms.csv"            # 业务术语词典
WORDLIST = "wordlist.csv"      # 业务标准词表（电力 + 政务）


def _resolve_data_dir() -> Path:
    """在多个候选位置找 data 目录，提高不同部署环境下的兼容性。"""
    here = Path(__file__).resolve().parent
    for cand in (here / "data", here.parent / "data", Path("/code/data"),
                 Path.cwd() / "data", Path("/tmp/data")):
        if cand.is_dir():
            return cand
    return here / "data"


DATA = _resolve_data_dir()


def describe_data() -> str:
    """给出数据目录的实际情况，便于排查部署问题。"""
    if not DATA.is_dir():
        return f"[诊断] 数据目录不存在：{DATA}"
    files = sorted(p.name for p in DATA.iterdir())
    return f"[诊断] 数据目录 {DATA} 下共 {len(files)} 个文件：{', '.join(files[:12])}"


# ---------------- 发音模糊规则（与离线建索引时完全一致） ----------------
INITIAL_FUZZY = {
    "zh": "z", "ch": "c", "sh": "s",
    "n": "l", "l": "l",
    "f": "h", "h": "h",
    "r": "l",
    "j": "z", "q": "c", "x": "s",
}
FINAL_FUZZY = {
    "ang": "an", "eng": "en", "ing": "in",
    "iang": "ian", "uang": "uan", "ong": "on",
    "iong": "ion", "ueng": "uen",
    "uo": "o", "ie": "e", "ve": "ue",
}

PUNCT = re.compile(r"[\s\-_·、，,。.（）()\[\]【】《》]")
NAME_CHAR = re.compile(r"[\u4e00-\u9fa5A-Za-z0-9#\-]")
HAN = re.compile(r"[\u4e00-\u9fa5]")          # 单个汉字
HAN_RUN = re.compile(r"[\u4e00-\u9fa5]+")     # 连续汉字（用于判断整个片段是否纯汉字）
MAX_NAME_LEN = 20
# 片段里出现这些词，才按「完整名称」去匹配（避免把普通词误当名称）
SUFFIX_HINT = ("供电", "变电站", "电站", "台区", "变压器", "公变", "专变", "室变", "箱变")

# 单位名（公司 / 供电所 / 服务站）—— 只有这类名称才允许「简称匹配」
UNIT_KINDS = {"供电公司", "供电所", "供电服务站"}

# 「未知对象」检测用的两个形态 —— 见 Corrector._detect_unknown
# 形态一：单位后缀正则。不做「贪婪吞掉前缀」的匹配 —— 那会把前文的字一起吃进来，
#        既误报又拖慢（要做全表子串扫描）。改为只取后缀前两个字。
UNIT_SUFFIX_RE = re.compile(r"供电服务站|供电公司|供电中心|供电所|服务站|营业站")
# 形态二：疑问词前面的话题对象，如「陈晓怎么样」里的「陈晓」
QUESTION_HEAD = re.compile(
    r"([\u4e00-\u9fa5A-Za-z0-9]{2,6})(?=怎么样|咋样|怎样|如何|是什么情况|的情况)")

# 组织结构通用词：这些词的拼音不允许触发简称匹配，否则「公司」会命中所有公司
GENERIC_WORDS = ("国网", "供电", "公司", "供电所", "服务站", "变电站", "中心",
                 "台区", "线路", "用电", "电费")

# 简称匹配的「禁入常用词」——避免把常用词当成地名去纠错。
# 典型事故：「负荷」(fuhe) 与供电所「府河」(fuhe) 拼音完全相同，
# 不加护栏就会把「用电高峰负荷」改成「用电高峰府河」。误改比漏改危害大得多。
COMMON_WORDS = (
    "情况", "工作", "数据", "时候", "地方", "结果", "分析", "问题", "办法", "原因",
    "今天", "昨天", "明天", "现在", "本月", "上月", "本年", "去年", "今年",
    "这个月", "上个月", "下个月", "本周", "上周", "本季度", "上季度", "年初", "年底",
    "多少", "哪里", "什么", "怎么", "可以", "帮助", "查询", "统计", "平均", "最大", "最小",
    "排名", "占比", "增长", "下降", "报表", "异常", "汇总", "明细", "审批", "大概", "全部",
    "负荷", "电价", "用电", "售电", "客户", "用户", "数量", "金额", "总额", "费用",
    "成本", "效益", "收入", "支出", "预算", "执行", "完成", "进度", "计划", "目标",
    "指标", "考核", "评价", "里面", "上面", "下面", "这些", "那些", "我们", "你们",
    "请问", "麻烦", "帮忙", "一下", "一个", "这个", "那个", "是否", "怎么", "一下",
    "整体", "总体", "全部", "所有", "各个", "分别", "主要", "关键", "重点", "基本",
    "线损", "售电量", "供电量", "停电", "跳闸", "工单", "容量", "电压", "电流", "功率",
    "最近", "近期", "目前", "当前", "成效", "绩效", "安排", "分配", "安排", "使用",
    "全县", "全市", "全省", "全区", "全镇", "各村", "各地", "各所", "各公司", "各供电所",
    # 行业通用词。库里有「电力住宿公变 / 电力宾馆公变 / 电力修试厂变压器」这类台区，
    # 于是用户说「电力」时，地名层归一会把它们统统收进来、拼出「电力台区」——
    # 而「电力」是行业词，不是地名。这类词必须显式排除。
    "电力", "电网", "变电", "配电", "发电", "送电", "电业", "能源", "变压", "开关",
)

# 组合词里允许出现的虚词 —— 用于判断「公司的情况」这种是词的组合、不是专有名词
FILLER_CHARS = set("的了和与是在有个些之等中里为对从把及其这那多少")

# 出现这些字说明片段是问句用词而不是专有名词（「看各」「查一下」之类）。
# 专有地名几乎不会用到它们，用来抑制「未知对象」的误报。
STOP_CHARS = set("看查问说想找给帮请要知道这个那个哪些什么怎么如何是否"
                 "我你他她它们了吧呢吗的啊呀哦嗯")


def normalize(text: str) -> str:
    t = unicodedata.normalize("NFKC", str(text or ""))
    return PUNCT.sub("", t).lower()


_PINYIN_TABLE_CACHE: dict[str, tuple[str, str]] | None = None


def _load_pinyin_table() -> dict[str, tuple[str, str]]:
    """读「汉字 → (声母, 韵母)」表。

    这张表是离线生成的（tools/18_build_pinyin_table.py），
    表里的读音**按库内业务名称的语境投票定音**，所以和 catalog.csv 里
    预计算的拼音码口径一致 —— 多音字不会因为"单字默认音"而对不上。
    """
    global _PINYIN_TABLE_CACHE
    if _PINYIN_TABLE_CACHE is None:
        table: dict[str, tuple[str, str]] = {}
        try:
            with (DATA / PINYIN_TABLE).open(encoding="utf-8-sig", newline="") as f:
                for row in csv.DictReader(f):
                    ch = (row.get("字") or "").strip()
                    if len(ch) == 1:
                        table[ch] = ((row.get("声母") or "").strip(),
                                     (row.get("韵母") or "").strip())
        except FileNotFoundError:
            # 表缺失时退化为「不认拼音」：纠错的中文精确匹配仍然可用，
            # 只是同音错字兜不住。宁可少一个能力，也不要直接抛错让服务起不来。
            pass
        _PINYIN_TABLE_CACHE = table
    return _PINYIN_TABLE_CACHE


def _encode_with(text: str, table: dict, fuzzy: bool = True) -> str:
    """用给定的拼音表算编码（生成脚本要拿它和预计算列做一致性自检）。"""
    parts = []
    for ch in text:
        ini, fin = table.get(ch, (ch, ""))
        if not fin:
            parts.append(ini)
            continue
        parts.append((INITIAL_FUZZY.get(ini, ini) if fuzzy else ini)
                     + (FINAL_FUZZY.get(fin, fin) if fuzzy else fin))
    return "".join(parts)


def _syllables(text: str):
    """逐字给出 (声母, 韵母)。

    表里没有的字（含字母、数字、生僻字）**原样保留、韵母留空** ——
    注意不要 lower()：库里的拼音码对「10kV」「AC00101」这类是保留原大小写的，
    统一转小写会让这些名字的码对不上（实测 20183 条里差了 3700 条）。
    """
    table = _load_pinyin_table()
    out = []
    for ch in text:
        hit = table.get(ch)
        if hit is None:
            out.append((ch, ""))
        else:
            out.append(hit)
    return out


def encode(text: str, fuzzy: bool = True) -> str:
    parts = []
    for a, b in _syllables(text):
        if not b:
            parts.append(a)
            continue
        parts.append((INITIAL_FUZZY.get(a, a) if fuzzy else a)
                     + (FINAL_FUZZY.get(b, b) if fuzzy else b))
    return "".join(parts)


UNIT_PREFIX = ("国网湖北省电力有限公司", "湖北省电力有限公司", "国网", "国家电网")
UNIT_SUFFIX = ("供电服务有限公司", "供电有限公司", "供电公司", "供电服务中心",
               "供电服务站", "供电营业站", "供电中心", "供电所", "供电部",
               "服务站", "营业站", "服务站", "分公司")

# 台区名的「类型后缀」—— 按长度降序，必须先匹配长词
# 库里的台区名大多带类型词（「白鹤二组公变」），而用户口语里会说成别的类型词
# （「白鹤二组台区」）。不剥掉再比，整名相似度永远过不了阈值。
AREA_SUFFIX = ("配电变压器", "柱上变压器", "箱式变压器", "变压器",
               "柱上变", "箱变", "公变", "专变", "室变", "变台", "台区", "配变", "变")

AREA_SUFFIX_RE = re.compile("|".join(AREA_SUFFIX))   # 已按长度降序，先匹配长词

# 台区名向左最多回溯多少个字符。库内最长 66 字，取 70 保证长企业名不被切掉。
# 汉字表那遍是纯字符串比较（约 3us/步），70 步也不过 0.2ms，可以放宽。
AREA_MAX_LEN = 70

# 拼音表那遍贵得多（约 180us/步），只用于兜短地名的同音错字（白鹤/白何），
# 窗口收窄到 24 字控制成本 —— 几十字的企业全名很少是靠听写错的。
AREA_PY_MAX_LEN = 24

# 模糊回退的分桶前缀长度（取拼音码前几位）。
# 3 位太粗：「gongjiapeng…」（龚家棚）和「gongbian…」（公变）前缀都是 gon，
# 一个桶里混进四百多个「公变…」；取 5 位后分成 gongj / gongb，互不干扰。
AREA_PFX_LEN = 5
# 模糊命中的最低相似度。调低会放进弱候选，宁可漏、不可错。
AREA_FUZZY_MIN = 0.78

# 从核心词里抽出「编号段」（连续数字），用于校验编号是否一致。
# 「公家棚3号」的用户说的就是 3 号，不能拿「龚家棚新2#台区」来当候选。
DIGIT_RUN = re.compile(r"\d+")

# 名称归一里「相似度」这条路的门槛，比 AREA_FUZZY_MIN 严。
# 0.78 在校错场景够用（候选会全部列给下游反问），但归一要给**唯一答案**，
# 必须更保守：实测「电力局」会被 0.824 的「电信局专变」命中，这是明显误改。
RESOLVE_FUZZY_MIN = 85.0

# 前缀匹配要求「用户说的地名」至少覆盖候选核心词的这么长。
# 光靠前缀会出现这类误判：「电力局」是「电力局住宿台区」的前缀，但用户说的是单位不是那个台区；
# 「白鹤」是十九个白鹤系台区的前缀，随便挑一个都是掷骰子。
# 要求覆盖度 >= 70% 之后，只剩「说的地名本身就是候选核心的大半」才算数。
RESOLVE_PREFIX_MIN_COV = 70.0

# 「中台内部编码」——名字里 4 位以上的连续数字（如 …台区40123）。
# 用户明确说了编号（「龚家棚3号」）时，优先带内部编码的那个：
# 它说明这个台区在中台正式登记过，比只有简称的条目更可能是用户指的那个。
LONG_CODE = re.compile(r"\d{4,}")

# 行政区划 / 人群后缀字。片段后面紧跟着这些字时，片段是**地名的一部分**而不是一个对象名 ——
# 「淅河镇」里的「淅河」不是淅河供电所，「我是随县人」里的「随县」不是随县供电公司。
# 少了这道检查，「曾都区淅河镇」会被改成「国网曾都区供电公司淅河供电所镇」。
ADMIN_TAIL_CHARS = set("镇乡村组街社区县市区盟旗屯寨堡人民")

_CN_DIGIT = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
             "六": 6, "七": 7, "八": 8, "九": 9}
_CN_DIGIT_RE = re.compile(r"[一二三四五六七八九十]+")


def _cn2num(m) -> str:
    s = m.group()
    if "十" not in s:
        return "".join(str(_CN_DIGIT[c]) for c in s)
    if s == "十":
        return "10"
    head, _, tail = s.partition("十")
    return (str(_CN_DIGIT.get(head, 1)) if head else "1") + \
           (str(_CN_DIGIT.get(tail, 0)) if tail else "0")


def canon_digits(text: str) -> str:
    """把数字与序号的常见写法统一，便于两侧比对。

    语音转写会把「3#」写成人念的「3号」，把「②」写成「2」，
    中文数字和阿拉伯数字也会混用 —— 不统一，两侧就对不上。
    注意：这个变换**两侧都用**，所以即使规则保守也不会引入偏差。
    """
    s = unicodedata.normalize("NFKC", text or "")   # 全角、①③、＃ 一并归一
    s = s.replace("#", "号").replace("♯", "号")
    return _CN_DIGIT_RE.sub(_cn2num, s)


# 通用词表层专用的声母表：去掉尖团音 j/q/x -> z/c/s。
# 随州属西南官话，并不合并尖团，j 和 zh 在普通话里也完全不同音；
# 保留这条规则只会把不同音的词判成同音，纯属制造误改。实测两例：
#   「基础」(jichu) 与「支出」(zhichu) 同码 -> 把「中国水电基础局」改成「中国水电支出局」
#   「区淅」与「趋势」同码               -> 把「曾都区淅河镇」改成「曾都区势河镇」
# 名称匹配层仍用完整规则（那一层候选被限定在真实名称里，代价可控）。
LIGHT_INITIAL = {k: v for k, v in INITIAL_FUZZY.items() if k not in ("j", "q", "x")}


def encode_light(text: str) -> str:
    """通用词表层的编码：保留平翘舌/鼻音等真实方言混淆，去掉尖团音。

    误改比漏改危害大 —— 词表层是在整句上滑窗替换，必须要精度。
    """
    parts = []
    for a, b in _syllables(text):
        if not b:
            parts.append(a)
            continue
        parts.append(LIGHT_INITIAL.get(a, a) + FINAL_FUZZY.get(b, b))
    return "".join(parts)


def _area_core(name: str) -> str:
    """剥掉台区名末尾的「类型后缀」和「内部编号」，留下核心部分。

    白鹤二组公变             -> 白鹤二组
    龚家棚村3#台区40123      -> 龚家棚村3#
    龚家棚小区还建房3#变压器  -> 龚家棚小区还建房3#
    晓山1#                   -> 晓山1#    （没有类型后缀，保持原样）
    """
    s = (name or "").strip()
    for _ in range(4):
        before = s
        # 末尾 4 位以上的纯数字是内部编号，剥掉
        i = len(s)
        while i > 0 and s[i - 1].isdigit():
            i -= 1
        if i >= 2 and len(s) - i >= 4:
            s = s[:i]
        for suf in AREA_SUFFIX:
            if s.endswith(suf) and len(s) - len(suf) >= 2:
                s = s[: -len(suf)]
                break
        if s == before:
            break
    return s


def _only_suffix(frag: str) -> bool:
    """整个片段是不是**只由类型词组成**（「变压器」「台区」「变压器台区」「公变」）。

    这类不是名字，只是类型词的堆叠。必须显式排除，因为
    库里恰好有一条台区就叫「变压器」（脏数据）——
    不挡的话「白鹤变压器」会被切成「变压器」丢掉地名，
    「变压器台区」会被当成「变压器」这个台区去查。
    """
    s = frag
    while s:
        for suf in AREA_SUFFIX:                # 已按长度降序，先匹配长词
            if s.startswith(suf):
                s = s[len(suf):]
                break
        else:
            return False
    return True


def _area_core_content(core: str) -> str:
    """剥掉核心词开头的「类型词」，剩下的才是真正的内容。

    「公变和专变」这种是**类型词的并列**，不是台区名 —— 核心算出来是「公变和」，
    去掉开头的「公变」只剩功能字「和」，内容为空，应当整条排除。
    （这类输入整句都是通用词，不排除的话模糊回退会拿「公变和」去撞「公变1号」。）
    """
    s = core
    for _ in range(3):
        for suf in AREA_SUFFIX:
            if len(s) > len(suf) and s.startswith(suf):
                s = s[len(suf):]
                break
        else:
            break
    return s.strip("的了和与及之").strip()


def _area_core_ok(core: str) -> bool:
    """核心词是否有「识别力」（**传入的必须是归一前的原始核心**）。

    库里存在「3#变压器」这类脏数据名，核心词就是「3#」——
    放进索引后，「龚家棚3号台区」会被截成「3号台区」再对到它身上，
    得出「龚家棚3#变压器」这种错误结果。所以要滤掉纯编号核心。

    判定：至少有一个汉字，且不是「号」。
      · 「3#」    -> 没有汉字             -> 滤掉
      · 「3号」   -> 只有「号」，仍是编号  -> 滤掉
      · 「五一」  -> 五一                 -> 保留（真实地名）
      · 「五星5#」-> 五星                 -> 保留

    注意必须用归一**之前**的串：canon_digits 会把「五一」压成「51」、
    「五星5#」压成「5星5号」，拿压完的串来数汉字就会把好名字误杀。
    """
    return any(ch.isalpha() and ch != "号" for ch in core)


def _unit_core(name: str) -> str:
    """取出单位名的「核心地名」，如 国网随县供电公司 → 随县、两水供电所 → 两水。

    简称匹配**只认核心词及其前缀**，绝不认整名的任意切片 —— 这是关键。
    否则「电公」（来自「供电公司」）这种无意义切片会同时命中几十个单位，
    触发一堆没必要的反问。
    """
    s = (name or "").strip()
    for p in UNIT_PREFIX:
        if s.startswith(p) and len(s) > len(p):
            s = s[len(p):]
    changed = True
    while changed:
        changed = False
        for suf in UNIT_SUFFIX:
            if s.endswith(suf) and len(s) > len(suf):
                s = s[: -len(suf)]
                changed = True
                break
    return s


@dataclass
class Correction:
    original: str
    corrected: str = ""
    changed: bool = False
    word_fixes: list = field(default_factory=list)
    name_fixes: list = field(default_factory=list)
    need_clarify: list = field(default_factory=list)
    protected_terms: list = field(default_factory=list)
    # 识别到的「简称 / 地名」及其可能的完整单位名。
    # 注意：这里**只做标注，不改动正文** —— 用户说的是「随县」就保留「随县」，
    # 到底指「随县这个地方」还是「国网随县供电公司」，交给下游大模型结合问句判断。
    entities: list = field(default_factory=list)
    # 看着像对象名、但标准名录里根本找不到的片段。
    # 例：用户说「陈晓怎么样」，库里只有「陈巷供电所」——
    # 原来这种情况静默通过，下游会把它当成真实存在的对象去查，
    # 查不到就答「暂无数据」，用户根本不知道自己说错了名字。
    unknown_names: list = field(default_factory=list)
    # 台区名识别结果。处置分两档：
    #   · 唯一候选 —— 已改正正文，同时记进 name_fixes（已改正=True）；
    #   · 多候选   —— 不动正文，只在这里列出候选（已改正=False），由下游反问。
    # 多候选是真的歧义：库里可能同时存在「龚家棚新3#台区」和「龚家棚村3#台区40123」，
    # 硬挑一个就是替用户做决定。
    areas: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "corrected": self.corrected,
            "changed": self.changed,
            "need_clarify": self.need_clarify,
            "entities": self.entities,
            "unknown_names": self.unknown_names,
            "areas": self.areas,
            # 下面两项供排查用，百炼输出变量可以不接
            "word_fixes": self.word_fixes,
            "name_fixes": self.name_fixes,
            "protected_terms": self.protected_terms,
            "original": self.original,
        }


def _map_span(ops: list, s: int, e: int) -> tuple[int, int]:
    """把「纠错后文本」里的区间 [s, e) 映射回「原始输入」的区间。

    用途：report 里要写用户**实际说的**那个词。
    「凉水」经过纠错变成「两水」，如果不映射回去，日志会写成「两水 → 两水供电所」，
    对不上用户的原话，排查时看不出来。
    """
    hit = [op for op in ops if not (op[3] <= s or op[4] >= e)]
    if not hit:
        return s, e
    first, last = hit[0], hit[-1]
    if len(hit) == 1 and first[0] == "equal":
        # 区间整体落在一个「未改动」段里，按位移映射回去，保持长度
        return first[1] + (s - first[3]), first[1] + (e - first[3])
    # 跨段（或落在替换段里）：取所有相交段的并集。
    # 早先这里直接返回第一段的 (i1, i2)，于是「量水 → 两水」只映射出「量」一个字 ——
    # SequenceMatcher 会把「量水→两水」拆成 replace(量→两) + equal(水)，
    # 命中两段时若取第一段，区间就短了一截，日志里报的「原文」对不上用户原话。
    return first[1], last[2]


# 用户自己写在名字后面的类型词 —— 补全时要把它并进替换区间，别再补一遍。
# 「随县供电公司」：滑窗只切出「随县」，补成「国网随县供电公司」，
# 可「供电公司」四个字还在原地 → 叠成「国网随县供电公司供电公司」。
_ABSORB_TAILS = tuple(sorted(set(UNIT_KINDS) | set(AREA_SUFFIX), key=len, reverse=True))


def _absorb_tail(text: str, e: int, std: str) -> int:
    """把片段后面用户已经写出来的类型词，并进替换区间。

    只有当候选标准名**本身就以这个尾词收尾**时才并 ——
    「白鹤变压器」补成「白鹤变压器」不该并，「白鹤」补成「白鹤台区」也不该并，
    唯有「随县」→「国网随县供电公司」这种"名字里已经含了用户写的后缀"才适用。
    """
    for tail in _ABSORB_TAILS:
        if std.endswith(tail) and text.startswith(tail, e):
            return e + len(tail)
    return e


@dataclass
class NameResolution:
    """一个名称片段的归一结果。"""
    text: str                                  # 用户说的
    std: str                                   # 库里的标准名（补全后）
    kind: str = ""                             # 供电公司 / 供电所 / 供电服务站 / 台区
    score: float = 0.0
    how: str = ""                              # 匹配方式
    alts: list = field(default_factory=list)   # 其余候选标准名
    multi: bool = False                        # 库里是否还有别的同名对象
    exact: bool = True                         # 库里是否存在这个精确名
    cand_count: int = 0                        # 同名候选总数
    prefix: str = ""                           # 合成名对应的 LIKE 前缀（供下游查库）

    def to_dict(self) -> dict:
        d = {"原文": self.text, "标准名": self.std, "类别": self.kind,
             "置信度": self.score, "匹配方式": self.how, "多候选": self.multi}
        if not self.exact:
            # 明确告知下游：这是「地名层」合成名，库里没有这个精确名，
            # 应当按前缀去查，而不是拿它做等值匹配。
            d["库内精确名"] = False
            if self.prefix:
                d["建议前缀"] = self.prefix
        if self.cand_count > 1:
            d["同名候选数"] = self.cand_count
        if self.alts:
            d["其他候选"] = self.alts
        return d


@dataclass
class Resolution:
    """整句的「名称归一」结果。

    与 Correction 的区别：
      · Correction —— 保守。只改「确定是错的」字，多候选一律不猜。
      · Resolution —— 积极。一定给出库里的标准名，用于「只说半截 / 说错音」的场景。
    两者互不影响，可以串着用，也可以只用其中一个。
    """
    original: str
    resolved: str = ""
    changed: bool = False
    names: list = field(default_factory=list)        # list[NameResolution]
    unresolved: list = field(default_factory=list)   # 像名字、但库里对不上的片段

    def to_dict(self) -> dict:
        return {
            "original": self.original,
            "resolved": self.resolved,
            "changed": self.changed,
            "names": [n.to_dict() for n in self.names],
            "unresolved": self.unresolved,
        }


def _read(name: str) -> list:
    path = DATA / name
    if not path.exists():
        raise FileNotFoundError(
            f"知识库文件缺失：{path}\n{describe_data()}\n"
            "请确认 data/ 目录与其 7 个 csv 文件与本模块放在同一层"
            "（上传 FC 时随 ZIP 一起传；当库用时跟 name_corrector.py 放一起）。"
        )
    with path.open(encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


class Corrector:
    def __init__(self) -> None:
        # ---- 错字表（长词优先，避免「供电服务站」被「供电所」部分替换）
        self.typos = sorted(_read(TYPO), key=lambda r: -len(r["错误写法"]))

        # ---- 术语保护
        self.terms = _read(TERMS)
        self.term_set = {r["术语"] for r in self.terms}
        self.strict_terms = {r["术语"] for r in self.terms if len(r["术语"]) >= 3}

        # ---- 名称索引：直接用 CSV 里预计算好的拼音码，不重算
        self.by_code: dict[str, list[str]] = {}
        self.buckets: dict[int, list[str]] = {}   # 按拼音码长度分桶，供模糊回退使用
        self.kind: dict[str, str] = {}
        self.names: list[str] = []
        self.norm_names = set()
        for r in _read(CATALOG):
            nm, code = r["名称"], r["拼音码"]
            self.names.append(nm)
            if code not in self.by_code:
                self.buckets.setdefault(len(code), []).append(code)
            self.by_code.setdefault(code, []).append(nm)
            self.kind[nm] = r["类别"]
            self.norm_names.add(normalize(nm))

        # ---- 首字索引：供「未知对象」检测时快速找近似候选，避免全表扫描
        self.by_first: dict[str, list[str]] = {}
        self.unit_names: list[str] = []
        # 所有标准名用到的汉字。名称归一要在整句上滑窗试探，先拿这个集合过滤：
        # 片段里只要有一个字从没在名录里出现过，它就不可能是名字 —— 纯集合查找，几乎免费。
        # （不做这一步，光「售电量和线损率是多少」这句就要跑 1.4 万次 difflib 相似度。）
        self.name_chars: set[str] = set()
        for nm in self.names:
            self.name_chars.update(nm)
        for nm in self.names:
            if not nm:
                continue
            self.by_first.setdefault(nm[0], []).append(nm)
            if self.kind.get(nm) in UNIT_KINDS:
                self.unit_names.append(nm)

        # ---- 台区名「核心词」索引
        #   库里的台区名带类型后缀（「白鹤二组公变」），用户口语会说成别的类型词
        #   （「白鹤二组台区」），整名相似度只有 67 分，永远过不了阈值。
        #   剥掉类型后缀 + 数字归一后再比，核心完全相同 -> 100 分。
        #
        #   核心码**离线预计算**在 area_core.csv 里：两万条现算拼音码要 4 秒，
        #   冷启动扛不住 —— 和「拼音码」列同一个思路，重计算前置到数据文件。
        #
        #   处置分两档：唯一候选改正正文；多候选只给候选、不改正文（见 _match_areas）。
        #
        #   这里同时建两张表：
        #     · area_core_cn    —— 汉字核心 -> 台区名。纯字符串比较，3us/次，快；
        #     · area_core_index —— 拼音核心 -> 台区名。能挡同音错字，但 180us/次，慢。
        #   查询时先查 area_core_cn，未中再查拼音表 —— 顺序决定了整体快慢。
        self.area_core_index: dict[str, list[str]] = {}
        self.area_core_cn: dict[str, list[str]] = {}
        self.area_core_first: dict[str, list[str]] = {}
        # 按「拼音码前 3 位」分桶的模糊候选表 —— 比首字分桶更能兜住同音错字，
        # 且完全从 CSV 现成的「核心码」列派生，加载时零额外计算。
        self.area_core_pfx: dict[str, list[str]] = {}
        self.area_core_code: dict[str, str] = {}      # 名称 -> 核心码
        self.area_core_str: dict[str, str] = {}
        self._scache: dict[str, str] = {}
        try:
            for r in _read(AREA_INDEX):
                nm, code = r["名称"], r["核心码"]
                if not code or self.kind.get(nm) != "台区":
                    continue
                raw = _area_core(nm)
                if not _area_core_ok(raw):
                    continue       # 纯编号核心（3# / 1号）没有识别力，不收
                core = canon_digits(raw)
                self.area_core_index.setdefault(code, []).append(nm)
                self.area_core_cn.setdefault(core, []).append(nm)
                self.area_core_str[nm] = core
                self.area_core_code[nm] = code
                self.area_core_pfx.setdefault(code[:3], []).append(nm)
                if core:
                    self.area_core_first.setdefault(core[0], []).append(nm)
        except FileNotFoundError:
            # 没有索引文件时退化为现算（慢，仅作兜底，别指望冷启动好看）
            for nm in self.names:
                if self.kind.get(nm) != "台区":
                    continue
                raw = _area_core(nm)
                if len(raw) < 2 or not _area_core_ok(raw):
                    continue
                core = canon_digits(raw)
                c2 = encode(core, fuzzy=False)
                self.area_core_index.setdefault(c2, []).append(nm)
                self.area_core_cn.setdefault(core, []).append(nm)
                self.area_core_str[nm] = core
                self.area_core_code[nm] = c2
                self.area_core_pfx.setdefault(c2[:3], []).append(nm)
                self.area_core_first.setdefault(core[0], []).append(nm)

        # ---- 歧义表
        self.ambiguous: dict[str, list[str]] = {}
        for r in _read(AMBIGUOUS):
            g = self.ambiguous.setdefault(r["拼音码"], [])
            for k in ("名称A", "名称B"):
                if r[k] not in g:
                    g.append(r[k])

        # ---- 单位名简称索引：只收「核心地名」及其前缀
        #   「量水」→ 核心「两水」→ 两水供电所
        #   「广水」→ 核心「广水市」的前缀 → 国网广水市供电公司
        #   绝不收整名的任意切片：否则「电公」（供电公司的中间两字）会命中一堆单位。
        self.unit_spans: dict[str, list[str]] = {}
        for nm in self.names:
            if self.kind.get(nm) not in UNIT_KINDS:
                continue
            core = _unit_core(nm)
            if len(core) < 2:
                continue
            for k in range(2, len(core) + 1):
                bucket = self.unit_spans.setdefault(encode(core[:k], fuzzy=False), [])
                if nm not in bucket:
                    bucket.append(nm)

        # ---- 名称保护区：单位核心词 + 台区核心词
        #   通用词表层（_fix_vocab）整句滑窗做拼音替换，窗口切出来的碎片也会拿去比。
        #   实测翻车案例：「曾都区淅河镇」里滑出「区淅」(qu+xi)，
        #   与业务词「趋势」同码，于是被改成「曾都区势河镇」。
        #   这就是「任意切片做模糊匹配」的老毛病 —— 索引必须是有意义的完整词。
        #   做法：先把「已确认属于某个标准名称」的字位圈出来，词表层一律绕开。
        self.name_core_set: set[str] = set(self.area_core_cn)
        for nm in self.names:
            if self.kind.get(nm) in UNIT_KINDS:
                c = _unit_core(nm)
                if len(c) >= 2:
                    self.name_core_set.add(c)

        # 通用词拼音码：命中即跳过，防止「公司」这类词误触发简称匹配
        self.generic_codes = [encode(w, fuzzy=False) for w in GENERIC_WORDS]
        # 禁入词表：行业术语 + 常用词，这些一律不参与简称匹配
        self.no_partial = {normalize(t) for t in self.term_set}
        self.no_partial |= {normalize(w) for w in COMMON_WORDS}
        self.no_partial |= {normalize(w) for w in GENERIC_WORDS}

        # 切分词典：判断一个片段是否只是常用词的组合（见 _decomposable）
        self.split_words = set(self.no_partial)

        # ---- 业务标准词表（电力 + 政务）：按拼音索引，自动覆盖同音错字
        self.vocab_by_code: dict[str, list[str]] = {}
        self.vocab_domain: dict[str, str] = {}
        self.vocab_norm: set[str] = set()
        try:
            for r in _read(WORDLIST):
                wd = r["标准词"]
                # 用 encode_light 现算，而不是读 CSV 的「拼音码」列：
                # 那一列是用完整规则离线算的，与词表层的查询码不一致。
                # 词表只有两百来条，现算约 30ms，换来的是两边口径统一。
                self.vocab_by_code.setdefault(encode_light(wd), []).append(wd)
                self.vocab_domain[wd] = r["领域"]
                self.vocab_norm.add(normalize(wd))
        except FileNotFoundError:
            pass                              # 词表可选，缺失时跳过这一层
        self.split_words |= self.vocab_norm   # 业务词也参与「组合词」判断
        # 业务词里最长的字数 —— 供「词碎片保护区」扫描用（见 _vocab_spans）
        self.vocab_max_len = max((len(w) for w in self.vocab_norm), default=0)

        self._cache: dict[str, str] = {}
        self._best_cache: dict[str, object] = {}     # _best_name 结果缓存

    def _code(self, s: str) -> str:
        c = self._cache.get(s)
        if c is None:
            c = encode(s)
            self._cache[s] = c
        return c

    # ------------------------------------------------ ① 通用错字
    def _fix_words(self, text: str, out: Correction) -> str:
        for row in self.typos:
            w, r = row["错误写法"], row["正确写法"]
            if w != r and w in text:
                if any(t in text and w in t for t in self.strict_terms):
                    continue
                text = text.replace(w, r)
                out.word_fixes.append({"错误": w, "正确": r,
                                       "频率": row["频率等级"], "错因": row["错因类型"]})
        return text

    # ------------------------------------------------ ② 名称片段定位（词典驱动）
    def _candidates(self, text: str):
        n = len(text)
        raw = []
        for i in range(n):
            if not NAME_CHAR.match(text[i]):
                continue
            for L in range(min(MAX_NAME_LEN, n - i), 1, -1):
                frag = text[i:i + L]
                if not any(s in frag for s in SUFFIX_HINT):
                    continue
                code = self._code(frag)
                if code in self.by_code:
                    raw.append((i, i + L, frag, code))
                    break
        raw.sort(key=lambda x: (x[0], -(x[1] - x[0])))
        kept = []
        for s, e, frag, code in raw:
            if any(s < ke and e > ks for ks, ke, *_ in kept):
                continue
            kept.append((s, e, frag, code))
        return sorted(kept, key=lambda x: x[0])

    def _best(self, frag: str, code: str):
        """先查精确拼音码（覆盖全部同音错字）；没命中再走模糊回退（覆盖形近、漏字）。"""
        if code in self.by_code:
            cands = self.by_code[code]
            return 100.0, (cands[0] if len(cands) == 1 else None), cands

        # 模糊回退：只在「拼音码长度接近」的桶里比，避免扫描全部两万条
        n = len(code)
        pool: list[str] = []
        for L in range(max(1, n - 3), n + 4):
            pool.extend(self.buckets.get(L, ()))

        best, best_codes = 0.0, []
        for c in pool:
            r = SequenceMatcher(None, code, c).ratio()
            if r > best:
                best, best_codes = r, [c]
            elif r == best and c not in best_codes:
                best_codes.append(c)

        cands: list[str] = []
        for c in best_codes:
            cands.extend(self.by_code.get(c, ()))
        return best * 100, (cands[0] if len(cands) == 1 else None), cands

    def _fix_names(self, text: str, out: Correction) -> str:
        for s, e, frag, code in reversed(self._candidates(text)):
            if normalize(frag) in self.norm_names:
                continue
            score, best, cands = self._best(frag, code)
            if score < 88:
                continue                      # 阈值偏保守：宁可漏改不可误改
            if best is None:
                same_kind = [c for c in cands if self.kind.get(c) == self.kind.get(cands[0])]
                if len(same_kind) > 1:
                    out.need_clarify.append({
                        "原文片段": frag, "候选": same_kind[:5],
                        "提示": "以下名称读音相同，请确认是哪一个：" + " / ".join(same_kind[:5]),
                    })
                continue
            text = text[:s] + best + text[e:]
            out.name_fixes.append({"错误": frag, "正确": best,
                                   "置信度": round(score, 1),
                                   "类别": self.kind.get(best, "")})
        return text

    # ------------------------------------------------ ③ 简称匹配（只说地名的情况）
    def _unit_hits(self, qcode: str) -> list[str]:
        """在「单位名核心词」索引里查 qcode（**严格拼音码**，不做模糊音）。

        为什么必须用严格音：
          模糊音会把 sh→s、zh→z 合并，于是「司厉」(sī lì) 和「十里」(shí lǐ) 同码。
          而「司厉」是「公司」+「厉山」的**跨词切片**，字面上毫无意义 ——
          一旦命中就会把「厉山」错改成「十里」。简称匹配是「这是哪个单位」的
          高置信度判断，宁可漏、不可错，所以只用严格音。
        """
        if len(qcode) < 4:                       # 少于 2 个汉字不猜
            return []
        if any(qcode in g or g in qcode for g in self.generic_codes):
            return []                            # 「公司」「供电」这类通用词直接跳过
        return list(self.unit_spans.get(qcode, ()))

    def _slice_by_code(self, name: str, start: int, length: int) -> str | None:
        """按拼音码对齐，切出 name 里对应 [start, start+length) 的那几个汉字。

        用来把「量水」在**原位置**换成「两水」，而不是整段替换成「两水供电所」。
        """
        total = 0
        begin = None
        for i, ch in enumerate(name):
            if begin is None and total == start:
                begin = i
            total += len(self._code(ch))
            if begin is not None and total == start + length:
                return name[begin:i + 1]
            if total > start + length:
                return None
        return None

    def _annotate_partial_names(self, text: str, out: Correction) -> str:
        """识别「量水」「厉山」「随县」这类只有地名 / 简称的片段。

        两条原则：
          1. **只把错的字改对，不补全后缀** —— 「量水」改「两水」，而不是「两水供电所」。
             用户问的是「随县这个地方」还是「随县供电公司」，代码不替他决定。
          2. **识别结果放进 entities**，由下游大模型结合问句意图自行判断。
        """
        # 逐个起点、从长到短试切片 —— 不能用 finditer，它会把整句切成 6 字块，
        # 导致「随县」这种真正想找的片段被淹没。
        n = len(text)
        raw = []
        for i in range(n):
            if not HAN.match(text[i]):
                continue
            for L in range(min(6, n - i), 1, -1):
                frag = text[i:i + L]
                if not HAN_RUN.fullmatch(frag):
                    break
                if normalize(frag) in self.no_partial:
                    continue                     # 常用词/术语，不当作地名
                if any(h in frag for h in SUFFIX_HINT):
                    # 带后缀的是完整名称的形态，交给 ③ 完整名称匹配处理；
                    # 但**不能 break** —— 「随县供电公司」里的「随县」正是我们要找的简称，
                    # 必须继续往短了试。
                    continue
                # 用严格音查（不用 self._code 的模糊音），避免跨词切片误命中
                hits = self._unit_hits(encode(frag, fuzzy=False))
                if hits:
                    raw.append((i, i + L, frag, hits))
                    break
        # 重叠时保留更长的那个
        raw.sort(key=lambda x: (x[0], -(x[1] - x[0])))
        pending = []
        for s, e, frag, hits in raw:
            if any(s < ke and e > ks for ks, ke, *_ in pending):
                continue
            pending.append((s, e, frag, hits, normalize(frag) in self.norm_names))

        rows = []
        for s, e, frag, hits, already_ok in reversed(pending):
            code = self._code(frag)
            best = hits[0] if len(hits) == 1 else None
            cur = frag

            # —— 只做「正字替换」：把错字换成正确写法，粒度保持不变 ——
            # 若这个音同时也可能是一个业务词（典型：府河 / 负荷），
            # 说明无法判断，交给 _fix_vocab 层去反问，这里不动。
            ambiguous = bool(self.vocab_by_code.get(code))
            if not already_ok and best is not None and not ambiguous:
                off = self._code(best).find(code)
                if off >= 0:
                    fixed = self._slice_by_code(best, off, len(code))
                    if fixed and fixed != frag:
                        cur = fixed
                        text = text[:s] + fixed + text[e:]
                        out.name_fixes.append({
                            "错误": frag, "正确": fixed, "置信度": 92.0,
                            "类别": self.kind.get(best, ""),
                            "依据": "地名/简称按拼音校正（不补全后缀）",
                        })

            if len(hits) > 1:
                out.need_clarify.append({
                    "原文片段": cur, "候选": hits[:5],
                    "提示": "以下单位包含相同的字，请确认是哪一个：" + " / ".join(hits[:5]),
                })

            rows.append({
                "文本": cur,
                "可能指": hits[:5],
                "说明": "简称/地名，也可能是单位名，请结合问句意图判断",
            })

        out.entities.extend(reversed(rows))
        return text

    # ------------------------------------------------ ④ 业务标准词纠错
    def _word_ambiguous(self, frag: str, code: str) -> list[str]:
        """同一个音既是业务词、又是地名时，无法判断，必须反问。

        典型：「负荷」(fuhe) 与供电所「府河」(fuhe) 拼音完全相同。
        """
        if not self.vocab_by_code.get(code):
            return []
        return self._unit_hits(encode(frag, fuzzy=False))

    def _protect_spans(self, text: str) -> list[tuple[int, int]]:
        """圈出文本里「属于某个标准名称」的字位区间，供通用词表层绕开。

        只做 2~6 字的窗口查询，命中的都是完整核心词（「淅河」这类），
        纯字典查询、不做拼音编码，成本可忽略。
        """
        spans: list[tuple[int, int]] = []
        n = len(text)
        for i in range(n):
            if not HAN.match(text[i]):
                continue
            for L in range(min(6, n - i), 1, -1):
                frag = text[i:i + L]
                if not HAN_RUN.fullmatch(frag):
                    break
                if frag in self.name_core_set:
                    spans.append((i, i + L))
                    break
        return spans

    def _vocab_spans(self, text: str) -> list[tuple[int, int]]:
        """圈出文本里**已经写对了**的业务词的区间。

        词层纠错只认读音、不看上下文，于是：
        「产生经济效益」里能滑出「济效」(jixiao)，而词表里正好有「绩效」(jixiao)，
        两个字一换，「经济效益」就成了「经绩效益」—— 把对的改错了。

        所以：凡落在某个已写对的业务词内部的片段，一律不许动。
        真写错了的话整词就不在词表里，也就圈不出这个区间，纠错照常生效。
        """
        spans: list[tuple[int, int]] = []
        if not self.vocab_norm or not self.vocab_max_len:
            return spans
        n = len(text)
        for i in range(n):
            if not HAN.match(text[i]):
                continue
            for L in range(min(self.vocab_max_len, n - i), 1, -1):
                frag = text[i:i + L]
                if not HAN_RUN.fullmatch(frag):
                    break
                # 类型词（「变压器」「台区」「公变」…）本身就是名字的组成部分，
                # 不是独立业务词。把它圈进保护区，「白鹤变压器」整段就被跳过了，
                # 反倒归一不了 —— 所以这里要排掉纯类型词。
                if normalize(frag) in self.vocab_norm and not _only_suffix(frag):
                    spans.append((i, i + L))
                    break
        return spans

    def _fix_vocab(self, text: str, out: Correction) -> str:
        """把错写的业务词按拼音还原成标准词。

        与错法表的分工：
          · 错法表（typo.csv）—— 人写的最高频错法，精确字符串替换，最可靠
          · 本层（词表）    —— 自动覆盖「没写出来的同音错法」，专治长尾
        例：预蒜→预算、变鸭器→变压器、完成律→完成率、执行绿→执行率
        """
        if not self.vocab_by_code:
            return text

        # 标准名称 + 已写对的业务词，两边都不许从内部切开去纠
        blocked = self._protect_spans(text) + self._vocab_spans(text)
        n = len(text)
        raw = []
        for i in range(n):
            if not HAN.match(text[i]):
                continue
            for L in range(min(6, n - i), 1, -1):
                frag = text[i:i + L]
                if not HAN_RUN.fullmatch(frag):
                    break
                if any(h in frag for h in SUFFIX_HINT):
                    break                      # 带名称后缀的交给名称匹配
                norm = normalize(frag)
                if norm in self.vocab_norm or norm in self.no_partial:
                    continue                   # 本身已是标准词 / 属常用词
                cands = self.vocab_by_code.get(encode_light(frag))
                if cands:
                    if any(i < be and i + L > bs for bs, be in blocked):
                        break                  # 落在标准名称内部，不许改
                    raw.append((i, i + L, frag, cands))
                    break
        raw.sort(key=lambda x: (x[0], -(x[1] - x[0])))
        kept = []
        for s, e, frag, cands in raw:
            if any(s < ke and e > ks for ks, ke, *_ in kept):
                continue
            kept.append((s, e, frag, cands))

        for s, e, frag, cands in reversed(kept):
            code = encode_light(frag)
            places = self._word_ambiguous(frag, code)
            if places:
                out.need_clarify.append({
                    "原文片段": frag,
                    "候选": cands + places[:5],
                    "提示": f"「{frag}」读音既像业务词（{'/'.join(cands)}）"
                            f"又像地名（{'/'.join(places[:3])}），请确认指的是哪一个",
                })
                continue                       # 宁可反问，不要猜错
            uniq = [c for c in cands if normalize(c) != normalize(frag)]
            if len(uniq) != 1:
                continue
            best = uniq[0]
            text = text[:s] + best + text[e:]
            out.word_fixes.append({
                "错误": frag, "正确": best,
                "领域": self.vocab_domain.get(best, ""),
                "依据": "业务词表·拼音匹配",
            })
        return text

    # ------------------------------------------------ ⑤ 台区名核心词匹配
    def _trim_head(self, frag: str) -> str:
        """从左侧剥掉疑问用词和已知的常用词，留下真正的名字部分。

        「查一下白鹤二组台区」->「白鹤二组台区」
        「看看龚家棚3号台区」->「龚家棚3号台区」
        """
        s = frag
        while len(s) > 2:
            if s[0] in STOP_CHARS:
                s = s[1:]
                continue
            cut = 0
            for k in (3, 2):
                if len(s) > k and s[:k] in self.split_words:
                    cut = k
                    break
            if not cut:
                break
            s = s[cut:]
        return s

    def _scode(self, s: str) -> str:
        """严格拼音码（带缓存）—— 台区匹配全程用它。

        子串扫描会对同一个位置的不同长度反复编码，缓存能省掉大部分重复计算。
        """
        c = self._scache.get(s)
        if c is None:
            c = encode(s, fuzzy=False)
            self._scache[s] = c
        return c

    def _area_exact(self, frag: str) -> bool:
        q = canon_digits(_area_core(frag))
        # 先查汉字表（快），再查拼音表（慢，兜同音）
        return len(q) >= 2 and (q in self.area_core_cn or self._scode(q) in self.area_core_index)

    def _match_areas(self, text: str, out: Correction) -> str:
        """找出文本里提到的台区，容忍「类型后缀不同」和「语音丢字」。

        两类真实错法：
          · 「白鹤二组台区」  vs 库里的「白鹤二组公变」 —— 类型词说成了别的；
            整名相似度只有 67 分，永远过不了阈值；剥掉类型后缀后核心完全相同。
          · 「龚家棚3号台区」 vs 库里的「龚家棚村3#台区40123」 —— 丢字 + 「#」说成「号」。

        做法：剥掉类型后缀、把数字写法归一，再比核心词。

        处置分两档（**这是关键**）：
          · **唯一候选** → 直接改正正文，并记入 name_fixes。
            理由：核心词完全相同、库里只有一个对应台区，这就是确定性事实，
            不是猜测。「白鹤二组台区」在库里只有「白鹤二组公变」一个，
            此时不改反而让下游拿着错名字去查库，必然查不到。
          · **多个候选** → **不改正文**，把候选如实列进 areas。
            理由：库里确实有多个同名台区（如「龚家棚新3#台区」与
            「龚家棚村3#台区40123」），硬选一个就是替用户做决定，必须反问。
        """
        found: list[tuple[int, int, str]] = []

        # 形态一：带类型后缀的片段（覆盖「把公变说成台区」这类换词说法）
        for m in AREA_SUFFIX_RE.finditer(text):
            end = m.end()
            frag = self._area_frag_at(text, end)
            if len(frag) >= 2:
                found.append((end - len(frag), end, frag))

        # 形态二：子串能跟核心词精确对上的（覆盖「龚家棚八组」这类本身不带类型后缀的台区名）
        #   · 只认**精确**命中的核心，不做模糊 —— 台区有两万条，放开模糊会大面积误伤；
        #   · 只在形态一什么都没找到时才跑 —— 它是 O(文本长度 × 12) 的扫描，别每次都做。
        n = len(text)
        if not found:
            for i in range(n):
                if not NAME_CHAR.match(text[i]):
                    continue
                for j in range(min(i + 12, n), i + 2, -1):
                    frag = text[i:j]
                    if self._area_exact(frag):
                        found.append((i, j, frag))
                        break

        # 长的优先，被包含的短片段丢掉
        found.sort(key=lambda x: (x[0], -(x[1] - x[0])))
        kept: list[tuple[int, int, str]] = []
        for s, e, frag in found:
            if any(s < ke and e > ks for ks, ke, _ in kept):
                continue
            kept.append((s, e, frag))

        seen: set[str] = set()
        # 从右往左替换，前面的偏移量才不会被写坏
        for s, e, frag in reversed(kept):
            key = normalize(frag)
            if len(key) < 2 or key in seen or key in self.norm_names:
                continue
            if _only_suffix(frag):
                continue           # 光一个「变压器」「台区」是类型词，不是台区名
            if not _area_core_content(canon_digits(_area_core(frag))):
                continue           # 「公变和专变」这类类型词的并列，不是台区名
            if key in self.no_partial:
                # 「供电所」「线损」这类词一律不当台区名。
                # 库里确实有脏数据台区叫「供电所公变」「供电所变压器公变」，
                # 核心词正好是「供电所」—— 不拦的话「厉山供电所」会被改成
                # 「厉山供电所公变」，属于典型的「误改比漏改危害大」。
                continue
            seen.add(key)
            hits = self._area_candidates(frag)
            if not hits:
                continue
            top = hits[0][0]
            # 只有明显领先（拉开 8 分）才给单一答案，否则如实列出全部候选
            close = [n for sc, n in hits if sc >= top - 8]
            uniq = len(close) == 1
            out.areas.append({
                "文本": frag,
                "标准名": close[:5],
                "置信度": round(top, 1),
                "匹配方式": "核心词精确" if top >= 99.9 else "核心词模糊",
                "已改正": uniq,
            })
            if not uniq:
                # 多候选 —— 不猜，但必须把「该反问」这件事**显式报出去**。
                # 只放在 areas 里容易被下游漏看；need_clarify 才是统一的反问接口。
                out.need_clarify.append({
                    "原文片段": frag,
                    "候选": close[:5],
                    "提示": "「" + frag + "」在库中对应多个台区，请确认是哪一个："
                            + " / ".join(close[:5]),
                })
                continue
            best = close[0]
            text = text[:s] + best + text[e:]
            out.name_fixes.append({
                "错误": frag, "正确": best, "置信度": round(top, 1),
                "类别": "台区", "依据": "台区名核心词匹配",
            })
        return text

    def _area_frag_at(self, text: str, end: int) -> str:
        """定位「以 end 结尾」的台区名片段。

        关键点：**片段边界由「核心词能不能对上标准名录」决定，而不是由固定窗口决定。**
        早先写死 12 字窗口，把长名字的开头切掉了 ——
        「随县环潭镇铁门坎电站2#室变」只剩「潭镇铁门坎电站2#室变」，
        查库必然落空。而 24.5% 的台区名都超过 12 字（90 分位 30 字）。

        所以改成从右往左逐字扩展，取**最长的、核心词能命中的**片段。

        两条铁律，缺一不可：
          1. i 从小到大 = 片段从长到短，**第一个命中就是最长命中，命中即可停**。
             写反过一回（保留最后一个命中），等于取最短片段 ——
             「磷肥厂2#箱变」被截成「2#箱变」，反而对到别的台区上。
          2. **两遍扫描必须共享「最长」这个目标**，不能第一遍命中了就直接返回。
             踩过的坑：「辰巷公用5#台区」里「公用5#台区」正好是个已登记的台区核心，
             第一遍在 i=2 就命中并返回，可正确答案是 i=0 的整串（靠同音字「辰/陈」才能对上）。
             所以第一遍只记位置，第二遍**只在「比它更长」的范围内**再找一次。
        """
        lo = max(0, end - AREA_MAX_LEN)

        best: int | None = None
        for i in range(lo, end - 1):
            frag = text[i:end]
            if _only_suffix(frag):
                continue
                # 光一个「变压器」「台区」是类型词，不是名字。
                # 库里恰好有一条台区就叫「变压器」（脏数据），不挡的话
                # 「白鹤变压器」会被切成「变压器」，前面的地名整个丢掉。
            if canon_digits(_area_core(frag)) in self.area_core_cn:
                best = i
                break

        # 拼音那遍很贵（约 180us/次），只扫「还能比当前命中更长」的位置：
        #   已有汉字命中 -> 只看它左边的位置；完全没有 -> 限一个窗口兜底。
        hi = best if best is not None else min(end - 1, lo + AREA_PY_MAX_LEN)
        for i in range(lo, hi):
            frag = text[i:end]
            if _only_suffix(frag):
                continue
            q = canon_digits(_area_core(frag))
            if len(q) >= 2 and self._scode(q) in self.area_core_index:
                best = i
                break

        if best is not None:
            return text[best:end]

        # 两遍都没精确命中 —— 退回到「短窗口 + 模糊」，
        # 这种情况是靠 _area_candidates 里的相似度兜（如「龚家棚3号」对「龚家棚村3#」）。
        return self._trim_head(text[max(0, end - 12):end])

    def _area_candidates(self, frag: str) -> list[tuple[float, str]]:
        """返回 (置信度, 标准台区名) 列表，按置信度降序。"""
        q = canon_digits(_area_core(frag))
        if len(q) < 2:
            return []
        exact = self.area_core_cn.get(q)          # 汉字完全相同 —— 最快路径
        if exact:
            return [(100.0, n) for n in exact]
        exact = self.area_core_index.get(self._scode(q))   # 同音错字
        if exact:
            return [(100.0, n) for n in exact]
        if len(q) < 3:
            return []                          # 太短不猜

        # ---- 模糊回退：按「拼音码前 3 位」分桶，**不是按首字分桶**
        #   按首字分桶会漏掉「同音不同字」的错法：
        #   用户说「公家棚3号台区」（公 / 龚 同音），按首字只会在「公」桶里找，
        #   而那里是 300 多条「公变…」，压根不会去看只有 34 条的「龚」桶 —— 结果一个候选都没有。
        #   改成按拼音码前缀分桶，两个「gong jia peng」自然落进同一个桶。
        #
        #   相似度也必须比**拼音码**而不是比汉字：
        #     「公家棚3号」vs「龚家棚村3号」按汉字只有 0.667（低于阈值），
        #     按拼音码是 0.882 —— 因为真正该相等的是读音，不是字形。
        qc = self._scode(q)
        qd = tuple(DIGIT_RUN.findall(q))
        # 先窄后宽：窄桶精度高、也快；窄桶没捞出东西再放宽一档兜底
        for plen in (AREA_PFX_LEN, 3):
            scored: list[tuple[float, str]] = []
            for n in self.area_core_pfx.get(qc[:3], ()):
                code = self.area_core_code.get(n, "")
                if code[:plen] != qc[:plen]:
                    continue                    # 不属于更窄的那一档
                if abs(len(code) - len(qc)) > max(5, len(qc) // 2):
                    continue                    # 拼音码长度差太多，先排掉 —— difflib 很贵
                nd = tuple(DIGIT_RUN.findall(self.area_core_str.get(n, "")))
                if qd and nd and nd != qd:
                    continue                    # 编号对不上，直接排除：说 3 号就不是 2 号
                r = SequenceMatcher(None, qc, code).ratio()
                if r >= AREA_FUZZY_MIN:
                    scored.append((round(r * 100, 1), n))
            if scored:
                scored.sort(key=lambda x: (-x[0], x[1]))
                return scored[:5]
        return []

    # ------------------------------------------------ ⑥ 未知对象检测
    def _decomposable(self, frag: str) -> bool:
        """整个片段能否切成已知词 —— 能切就说明它是常用词的组合，不是专有名词。

        例：「公司的情况」= 公司 + 的 + 情况  -> 不是专有名词，跳过
            「台区线损」  = 台区 + 线损        -> 不是专有名词，跳过
            「陈晓」      -> 切不开            -> 可能是专有名词，报未知
        没有这道校验，「整体情况怎么样」这类问句会被整段当成未知对象。
        """
        n = len(frag)
        if n == 0:
            return True
        reach = [False] * (n + 1)
        reach[0] = True
        for i in range(n):
            if not reach[i]:
                continue
            if frag[i] in FILLER_CHARS:
                reach[i + 1] = True
            for j in range(i + 2, min(n, i + 5) + 1):
                if frag[i:j] in self.split_words:
                    reach[j] = True
        return reach[n]

    def _known_substring(self, frag: str, _memo=None) -> bool:
        """frag 是不是某个标准名称的一部分。"""
        if _memo is None:
            _memo = getattr(self, "_sub_memo", None)
            if _memo is None:
                _memo = self._sub_memo = {}
        if frag in _memo:
            return _memo[frag]
        ok = any(frag in n for n in self.names)
        _memo[frag] = ok
        return ok

    def _detect_unknown(self, text: str, out: Correction) -> None:
        """标出「长得像对象名，但标准名录里没有」的片段。

        为什么需要：「陈晓」不是真实供电所（库里只有「陈巷供电所」）。
        原来这种情况静默通过 —— 下游把它当成真实存在的所去查，
        查不到就答「暂无数据」，用户根本不知道自己说错了名字。
        """
        seen: set[str] = set()

        def note(frag: str) -> None:
            key = normalize(frag)
            if len(key) < 2 or key in seen:
                return
            seen.add(key)
            if any(c in STOP_CHARS for c in frag):
                return                         # 含问句用词，是普通短语不是专名
            if key in self.no_partial or key in self.norm_names:
                return
            if self._decomposable(frag):
                return                         # 是常用词的组合，不是专有名词
            if self._known_substring(frag):
                return                         # 是某个标准名的一部分，不算未知
            out.unknown_names.append({
                "文本": frag,
                "最接近": self._nearest(frag),
                "说明": "未在标准名称库中找到，可能是识别错误或该对象不存在；"
                        "建议把范围退化为默认值处理，不要猜测相近的名称",
            })

        # 形态一：整句里出现单位后缀，但**没有任何真实单位名**被提到
        #        -> 用户说了一个不存在的单位。只取后缀前两个字作为候选。
        if not any(u in text for u in self.unit_names):
            m = UNIT_SUFFIX_RE.search(text)
            if m:
                note(text[max(0, m.start() - 2):m.start()])

        # 形态二：「XX 怎么样 / 如何」里的 XX —— 这是话题对象。
        # 若这段里含单位后缀（如「查一下供电所」），说明是形态一要管的事，跳过。
        for m in QUESTION_HEAD.finditer(text):
            head = m.group(1)
            if UNIT_SUFFIX_RE.search(head):
                continue
            note(head)

    def _nearest(self, frag: str, topn: int = 3) -> list[str]:
        """在名录里找与 frag 最接近的名字。

        单位名按「核心词」比对 —— 这样「陈晓」才会跟「陈巷供电所」对上，
        而不是跟整名算相似度（那样会因为长度差被稀释掉）。
        """
        scored: list[tuple[float, str]] = []
        for n in self.by_first.get(frag[0], ()):
            core = _unit_core(n) if self.kind.get(n) in UNIT_KINDS else n
            target = core if len(core) >= 2 else n
            scored.append((SequenceMatcher(None, frag, target).ratio(), n))
        scored.sort(reverse=True)
        return [n for s, n in scored[:topn] if s >= 0.4]

    # ------------------------------------------------ 名称归一（resolve）
    def resolve(self, text: str) -> Resolution:
        """把用户口语里的名称，补成**库里那个标准名**。

        适用场景：用户只说半截或说错音 ——
          「凉水」→ 两水供电所   「公家棚」→ 龚家棚…（库内标准名）
          「厉山」→ 厉山供电所   「量水」→ 两水供电所

        选名规则（按优先级）：
          1. **裸地名优先单位名**。用户只丢一个地名时，默认他问的是供电所
             （「凉水」→ 两水供电所，而不是某个台区）。
          2. **片段自带类型后缀时优先同类型**（「公家棚台区」→ 名字里也带「台区」的）。
          3. 编号必须一致（说了「3 号」就不会给「2#」）。
          4. 再按「汉字写法接近度 → 核心词覆盖度 → 名字长度 → 字典序」，
             保证同样输入永远同样输出。

        候选处置分两档：
          · **唯一候选** → 直接用库内标准名（「龚家棚村3号台区」→「龚家棚村3#台区40123」）；
          · **多候选台区** → 归到「地名层 + 台区」（「公家棚」→「龚家棚台区」）。
            库里通常**没有**这个精确名（结果里 `库内精确名=false`），
            但它是下游做前缀查询（LIKE '龚家棚%'）最合适的输入：
            知道是台区、但不知道是哪一个时，硬挑一个等于替用户做决定，
            一个都不给又没法查。全部候选同时放进 `其他候选` 供人工核对。
        """
        out = Resolution(original=text)
        base = self.correct(text)             # 先走常规纠错，拿到干净文本
        t = base.corrected
        # 纠错可能已经把字改过一遍（「凉水」→「两水」），
        # 但用户真正说的是「凉水」—— 拿原文和纠错结果对齐，把片段映射回原文再报，
        # 否则日志里写「两水 → 两水供电所」，对不上用户实际说的词。
        ops = SequenceMatcher(None, text, t).get_opcodes()
        hits: list[NameResolution] = []
        for s, e, frag in reversed(self._resolve_spans(t)):
            hit = self._best_name(frag)
            if hit is None:
                out.unresolved.append({"文本": frag, "最接近": self._nearest(frag)})
                continue
            if hit.exact and hit.std == frag:
                continue                       # 本身已是标准名，不算一次归一
            e = _absorb_tail(t, e, hit.std)    # 用户已写的类型尾词并进来，别补第二遍
            o_s, o_e = _map_span(ops, s, e)
            hit.text = text[o_s:o_e]
            if hit.std != frag:
                t = t[:s] + hit.std + t[e:]
            hits.append(hit)
        hits.reverse()
        out.names = hits
        out.unresolved.reverse()
        out.resolved = t
        out.changed = t != text
        return out

    def _std_spans(self, text: str) -> list[tuple[int, int]]:
        """圈出文本里**已经存在**的标准名的区间。

        这是 resolve() 的第一道护栏，缺了会出大事故：
          「量水供电所上个月的线损」经过常规纠错后已经是「两水供电所…」，
          若再滑窗命中「两水」并补成「两水供电所」，就变成
          「两水供电所**供电所**上个月的线损」——同一层信息补了两遍。
        同理「厉山供电所」会被补成「厉山供电所供电所」。
        凡是已经被标准名覆盖的区间，一律不许再动。
        """
        spans: list[tuple[int, int]] = []
        n = len(text)
        for i in range(n):
            if not HAN.match(text[i]):
                continue
            for L in range(min(30, n - i), 1, -1):
                frag = text[i:i + L]
                if not HAN_RUN.fullmatch(frag[:1]) and not frag[0].isalnum():
                    break
                if _only_suffix(frag):
                    continue
                    # 光一个「变压器」是类型词，不算「已经存在的标准名」。
                    # 库里那条叫「变压器」的记录是脏数据，认它做标准名会把
                    # 「白鹤变压器」整段保护起来，后面的地名归一就没法做了。
                if normalize(frag) in self.norm_names:
                    spans.append((i, i + L))
                    break
        return spans

    def _resolve_spans(self, text: str) -> list[tuple[int, int, str]]:
        """框出「可能是名字」的片段，两类形态：

        A. **带类型后缀**的 —— 边界交给 `_area_frag_at`（「白鹤二组台区」「公家棚台区」）；
        B. **裸片段** —— 用户只说了地名（「公家棚」「凉水」「厉山」）。
           这类没有后缀可锚，只能滑窗试探，再用库里查证来确认；
           因此过滤要严：标准名、常用词、业务词、含问句用词、能切分成词的一律跳过。

        两种形态都要避开 `_std_spans` 覆盖的区间 —— 那里已经是标准名了，不能再补。
        """
        # 标准名 + 已写对的业务词，区间内部都不许再切片段去猜名字。
        # 少了后者，「产生经济效益」会被滑出「生经济」→ 归一到台区「盛晶」。
        protected = self._std_spans(text) + self._vocab_spans(text)

        def blocked(s: int, e: int) -> bool:
            return any(s < pe and e > ps for ps, pe in protected)

        found: list[tuple[int, int, str]] = []
        for m in AREA_SUFFIX_RE.finditer(text):
            end = m.end()
            frag = self._area_frag_at(text, end)
            s = end - len(frag)
            if len(frag) < 2 or blocked(s, end):
                continue
            if _only_suffix(frag) or not _area_core_content(canon_digits(_area_core(frag))):
                continue
            if normalize(frag) in self.no_partial:
                continue
            found.append((s, end, frag))

        n = len(text)
        for i in range(n):
            if not HAN.match(text[i]):
                continue
            for L in range(min(6, n - i), 1, -1):
                frag = text[i:i + L]
                if not HAN_RUN.fullmatch(frag):
                    break
                if blocked(i, i + L):
                    continue              # 已经在某个标准名里面
                key = normalize(frag)
                if key in self.norm_names or key in self.no_partial or key in self.vocab_norm:
                    continue              # 已经是标准名 / 常用词 / 业务词
                if any(c in STOP_CHARS for c in frag):
                    continue              # 含问句用词，是普通短语
                if not all(ch in self.name_chars for ch in frag):
                    continue              # 有字从没在名录里出现过 —— 不可能是个名字
                if self._decomposable(frag):
                    continue              # 能切成已知词，不是专有名词
                hit = self._best_name(frag)
                if hit is not None:
                    # 行政区划护栏：片段后面紧跟「镇 / 乡 / 村 / 区」这类字时，
                    # 它是地名的一部分，不是独立对象 ——
                    # 「曾都区淅河镇」里的「淅河」不该被当成淅河供电所。
                    nxt = text[i + L] if i + L < n else ""
                    if nxt and nxt in ADMIN_TAIL_CHARS:
                        continue
                    found.append((i, i + L, frag))
                    break

        found.sort(key=lambda x: (x[0], -(x[1] - x[0])))
        kept: list[tuple[int, int, str]] = []
        for s, e, frag in found:
            if any(s < ke and e > ks for ks, ke, _ in kept):
                continue              # 被更长的片段包含
            kept.append((s, e, frag))
        return kept

    def _best_name(self, frag: str) -> NameResolution | None:
        """给一个片段找出库里的标准名；返回 None 表示库里没有像的。

        两条路都走：
          · **前缀**：`gongjiapeng` 正是 `gongjiapengcuen3hao` 的开头 ——
            用户只说地名（「公家棚」）时整名相似度会被尾部编号稀释到 0.73 而落选，
            按前缀看语义清楚、判据稳定；
          · **相似度**：用户说了编号（「公家棚3号台区」）时，库里名字中间多一个「村」字，
            前缀对不上，只能靠相似度兜（复用 _area_candidates）。

        还有一道**同音栅栏**：单纯靠读音对上的（汉字一个都不重合）要求片段里不含功能字。
        否则「售电**量和**线损」里的「量和」会被读成「两河一组」——
        量和(lianghe) 与 两河(lianghe) 同音，但前者显然不是地名。
        汉字能对上的（「万和」→「万和供电所」）不受这条限制。
        """
        if frag in self._best_cache:
            return self._best_cache[frag]          # type: ignore[return-value]
        res = self._best_name_uncached(frag)
        self._best_cache[frag] = res
        return res

    def _best_name_uncached(self, frag: str) -> NameResolution | None:
        core = canon_digits(_area_core(frag))
        if len(core) < 2 or _only_suffix(frag):
            return None
        # 已经是库内标准名 —— 一个字节都不要动。
        # 库里有「中国水电基础局有限公司变压器」这种带尾缀的长名，
        # 它的核心词恰好和另一个更短的名字相同，不先拦下来就会被"归一"掉尾缀。
        if normalize(frag) in self.norm_names:
            return NameResolution(text=frag, std=frag, kind=self.kind.get(frag, ""),
                                  score=100.0, how="已是标准名", exact=True)
        qc = self._scode(core)
        if len(qc) < 4:                        # 不足两个汉字，不去猜
            return None
        qd = tuple(DIGIT_RUN.findall(core))
        has_suffix = AREA_SUFFIX_RE.search(frag) is not None

        # ---- 收集候选：名 / 类别 / 覆盖度 / 匹配方式 / 名字里是否带类型后缀
        cands: list[tuple[str, str, float, str, bool]] = []
        exact_hit = False

        # ① 单位名 —— 用「核心地名」前缀匹配（厉山 → 厉山供电所、凉水 → 两水供电所）
        for code, names in self.unit_spans.items():
            if not code.startswith(qc):
                continue
            cov = round(len(qc) / max(1, len(code)) * 100, 1)
            if cov < RESOLVE_PREFIX_MIN_COV:
                continue
            for nm in names:
                uc = _unit_core(nm)
                if uc.startswith(core):
                    exact_hit = True
                cands.append((nm, self.kind.get(nm, ""), cov, "单位名核心前缀", False))

        # ② 台区/标准名 —— 前缀 + 相似度两条路并集
        # 宽池：只要拼音码以查询码开头就算数，**不设覆盖度门槛**。
        # 这是给「地名层归一」数候选用的 —— 用户说「白鹤」时，「白鹤1#台区」的码是
        # baihe1hao，覆盖度只有 62%，严格门槛会把它挡掉，于是一个候选都没有，
        # 地名层归一也就无从谈起（而这恰恰是最该归一的场景）。
        prefix_all = [nm for nm in self.area_core_pfx.get(qc[:3], ())
                      if self.area_core_code.get(nm, "").startswith(qc)]
        pool: list[str] = []
        seen: set[str] = set()
        for nm in prefix_all:
            if len(qc) / max(1, len(self.area_core_code.get(nm, ""))) * 100 < RESOLVE_PREFIX_MIN_COV:
                continue                       # 地名只覆盖了候选核心的一小半，不算命中
            pool.append(nm)
            seen.add(nm)
        for sc, nm in self._area_candidates(frag):
            # 归一必须给唯一答案，相似度这条路要收严：
            # 0.78 会把「电力局」对到「电信局专变」(0.824)，属于明显误改。
            if sc < RESOLVE_FUZZY_MIN and sc < 99.9:
                continue
            if nm not in seen:
                pool.append(nm)
                seen.add(nm)
        for nm in pool:
            nc_core = self.area_core_str.get(nm, "")
            # 只说了两个字时，不认「读音像」，只认「字也对得上」。
            # 反例：「平均停电时长」会被滑窗切出「电时」(dianshi)，而库里有个台区叫
            # 「电视台专变」(核心「电视台」，码 dianshitai)——码是前缀、覆盖率刚好 70.0，
            # 卡在门槛线上溜了进来，结果整句被改成「平均停电视台专变长」。
            # 两个字毫无"地名感"可言，全凭同音就下手，误改代价远大于漏改；
            # 三个字起才放开同音，好让「公家棚」「公家彭」能归一到「龚家棚」。
            if len(core) <= 2 and not nc_core.startswith(core):
                continue
            if nc_core.startswith(core):
                exact_hit = True
            nd = tuple(DIGIT_RUN.findall(nc_core))
            if qd and nd and nd != qd:
                continue                       # 编号对不上：说了 3 号就不是 2#
            cov = round(len(qc) / max(1, len(self.area_core_code.get(nm, ""))) * 100, 1)
            cov = min(cov, 100.0)              # 前缀比查询还短时会算出 >100，封顶
            cands.append((nm, "台区", cov, "核心词前缀", AREA_SUFFIX_RE.search(nm) is not None))

        # ---- 多候选台区 → 归到「地名层 + 台区」（不给某个具体台区）----
        # 位置很关键：要放在「候选为空就返回」**之前**。用户说「白鹤」时严格候选是空的
        # （所有白鹤系台区的覆盖度都不够），但宽池里有 24 个 —— 而这正是最该归一的场景：
        # 知道是台区，但不知道是哪一个。
        unit_cands = [c for c in cands if c[1] in UNIT_KINDS]
        norm_pool = prefix_all if len(prefix_all) >= 2 else [
            c[0] for c in cands if c[1] == "台区"]
        if len(norm_pool) >= 2 and not unit_cands:
            got = self._place_level_name(norm_pool, core, qc, qd, frag)
            if got:
                place, examples, prefix = got
                return NameResolution(
                    text=frag, std=place, kind="台区", score=100.0,
                    how="地名层归一", alts=examples,
                    multi=True, exact=False, cand_count=len(norm_pool),
                    prefix=prefix,
                )

        # 同音栅栏：全靠读音命中、且片段里夹了功能字 —— 判为噪声
        if cands and not exact_hit and any(ch in FILLER_CHARS for ch in frag):
            return None
        if not cands:
            return None

        q_has_digit = bool(qd)

        def same_chars(nm: str) -> int:
            """候选核心与用户所说地名的汉字重合数。

            库里存在同音异形（「龚家棚八组」与「龚加棚村4#」），
            排序时优先挑写法更接近用户输入的那个 —— 否则地名层归一
            可能拿「龚加棚」去当模板，把一个错字名字固化进输出。
            """
            nm_core = self.area_core_str.get(nm, "")
            return sum(1 for a, b in zip(core, nm_core) if a == b)

        def rank(c: tuple[str, str, float, str, bool]) -> tuple:
            nm, kind, cov, _how, name_has_suffix = c
            r_unit = 0 if (not has_suffix and kind in UNIT_KINDS) else 1
            r_suf = 0 if (has_suffix and name_has_suffix) else 1
            # 用户点明了编号（「龚家棚3号」）时，优先带中台内部编码的条目：
            # 有 4 位以上编号说明它在中台正式登记过，比只有简称的更可能是用户指的那个。
            r_code = 0 if (q_has_digit and LONG_CODE.search(nm)) else 1
            return (r_unit, r_suf, r_code, -same_chars(nm), -cov, len(nm), nm)

        cands.sort(key=rank)
        top = cands[0]

        alts = [c[0] for c in cands[1:] if c[0] != top[0]][:5]
        return NameResolution(
            text=frag, std=top[0], kind=top[1], score=top[2], how=top[3],
            alts=alts, multi=len(cands) > 1, cand_count=len(cands),
        )

    def _place_level_name(self, pool: list, core: str, qc: str,
                          qd: tuple, frag: str) -> tuple[str, list[str], str] | None:
        """从多个台区候选里提炼出「地名层」的合成名。

        库里「龚家棚」有 15 个台区（八组 / 村3#台区40123 / 新3#台区 …）。
        用户只说「公家棚」时，硬挑一个是替用户做决定；一个都不给又没法查。
        做法是取出共同的地名部分，拼成「龚家棚台区」——
        库里**没有**这个精确名，但它是下游做前缀查询（LIKE '龚家棚%'）最合适的输入。

        地名只取自**与用户输入汉字重合度最高的那一批**候选，不取全部：
        库里存在同音异形的条目（「龚加棚村4#台区40129」），
        全量求公共前缀会一路退化成「龚」。

        **类型词尊重用户自己的说法**：用户说了「白鹤变压器」，就输出「白鹤变压器」，
        不能自作主张换成「白鹤台区」—— 那会变成「白鹤台区变压器」，多出一层。

        返回 (合成名, 该批候选原名)，后者给下游人工核对用。
        """
        scored: list[tuple[int, str, str]] = []
        for nm in pool:
            h = self.area_core_str.get(nm, "")
            if not h:
                continue
            scored.append((sum(1 for a, b in zip(core, h) if a == b), h, nm))
        if len(scored) < 2:
            return None
        best = max(s for s, _, _ in scored)
        # 门槛只要求「至少一个汉字对上」，不是两个：
        # 候选池的前提已经是「整段拼音码以查询码开头」（读音完全吻合），
        # 汉字重合数只是用来在几族同音异形里挑最像的那一族。
        # 要求两个会漏掉「公家彭」（棚→彭 同音不同字，只剩「家」一个字对上）。
        if best < 1:
            return None
        group = [(h, nm) for s, h, nm in scored if s == best]
        if len(group) < 2:
            return None                        # 只有一条「写法接近」的，不算多候选

        cp = group[0][0]
        for h, _ in group[1:]:
            i = 0
            while i < len(cp) and i < len(h) and cp[i] == h[i]:
                i += 1
            cp = cp[:i]
        cp = cp.strip()
        if len(cp) < 2:
            return None

        # 护栏 A：地名不能是业务词 / 常用词。
        # 库里有「电力住宿公变」「电力宾馆公变」这类台区，用户说「电力」就会被
        # 归一成「电力台区」—— 而「电力」是行业词，不是地名。
        if normalize(cp) in self.no_partial or normalize(cp) in self.vocab_norm:
            return None

        # 护栏 B：台区名的常规形态是「地名 + 编号」（白鹤1# / 龚家棚村3#）。
        # 若候选去掉地名后多数不含数字（电力宾馆 / 电力修试厂 / 电力局住宿），
        # 说明这族名字不是按这个地名排下来的，不做归一。
        with_digit = sum(1 for h, _ in group if any(ch.isdigit() for ch in h[len(cp):]))
        if with_digit * 2 < len(group):
            return None

        # 地名必须**就是用户说的那个词**（同音），否则说明候选不成一族，别硬凑。
        # 允许尾巴上多出编号（「公家棚3号」的地名是「龚家棚」+ 编号 3）。
        pc = self._scode(cp)
        if not qc.startswith(pc):
            return None
        rest = qc[len(pc):]
        if len(rest) > 6 or (rest and not any(ch.isdigit() for ch in rest)):
            return None

        name = cp + (f"{qd[0]}#" if qd else "") + (self._said_suffix(frag) or "台区")
        # 第三个返回值是「LIKE 前缀」= 地名核心，由引擎给出而不是让下游自己猜 ——
        # 下游要写 `name LIKE '龚家棚%'`，若自己从合成名里截类型词，
        # 「白鹤变压器」会被截成「白鹤变压器%」（查不到「白鹤1#变压器」）。
        return name, [nm for _, nm in group[:10]], cp

    @staticmethod
    def _said_suffix(frag: str) -> str:
        """取用户在片段里说的类型词（「白鹤**变压器**」→「变压器」）。

        用户已经明确说了类型，就照他说的输出；没说的话才默认补「台区」。
        否则「白鹤变压器」会被补成「白鹤台区变压器」—— 多出一层，读着别扭。
        """
        m = AREA_SUFFIX_RE.search(frag)
        return m.group() if m else ""

    # ------------------------------------------------ 主入口
    def correct(self, text: str) -> Correction:
        out = Correction(original=text)
        t = self._fix_words(text, out)          # ① 错法表精确替换
        out.protected_terms = [x for x in self.term_set if len(x) >= 2 and x in t]
        t = self._fix_vocab(t, out)             # ② 业务词表拼音纠错
        t = self._fix_names(t, out)             # ③ 标准名称匹配
        t = self._annotate_partial_names(t, out)  # ④ 简称/地名标注
        t = self._match_areas(t, out)             # ⑤ 台区名核心词匹配（唯一候选会改正正文）
        self._detect_unknown(t, out)            # ⑥ 未知对象检测（只提示，不改正文）
        out.corrected = t
        out.changed = t != text
        return out
