# 名称纠错库（name_correction_lib）

中文名称与业务词的**确定性**处理：同音错字、类型后缀差异、丢字、简称。
算法在本地跑，**0 token、毫秒级、同样输入永远同样输出**。

## 一、接入（三步）

**1. 不需要装任何第三方包**

只用 Python 标准库（3.9+）。拼音靠 `data/pinyin_table.csv` 这张离线表查，
所以**没有 pypinyin 依赖** —— 可以直接进「零额外依赖」的项目。

**2. 把 `name_correction_lib/` 整个目录放进你的项目**

```
your_project/
├── app/
└── libs/
    └── name_correction_lib/      ← 放这里
```

**3. 调用**

```python
from libs.name_correction_lib import Corrector

corrector = Corrector()          # 全局建一次，常驻复用（无状态、可并发）
```

> 构造会加载约 2 万条索引（**200~300ms**）。请放在模块级或应用启动时建一次，
> **不要每次请求都 `Corrector()`** —— 那样每次都付一次加载成本。

先跑一下 `example_usage.py` 确认环境没问题：

```bash
python libs/name_correction_lib/example_usage.py
```

## 二、两个能力

| 方法 | 定位 | 行为 |
|---|---|---|
| `correct(text)` | **保守纠错** | 只改「确定是错的」字；多候选**不猜**，只报出来 |
| `resolve(text)` | **名称归一** | 把口语名补成库里的标准名，实在定不了就给「地名层」合成名 |

```python
corrector.correct("白鹤2组台区线损").corrected    # 白鹤二组公变线损
corrector.resolve("凉水的线损").resolved          # 两水供电所线损
```

推荐串起来用（先纠错、再归一）：

```python
t = corrector.correct(text).corrected
r = corrector.resolve(t)
final = r.resolved
```

## 三、⚠️ 最关键的一条约定：合成名必须前缀匹配

`resolve()` 可能给出一个**库里不存在**的名字。例如：

```
公家棚  →  龚家棚台区      ← 库里没有「龚家棚台区」这五个字
```

因为库里「龚家棚」系有 **15 个**台区（八组 / 村3#台区40123 / 新3#台区 …）。
用户只说了地名、没说哪一个时，硬挑一个是替用户做决定，所以引擎给出**共同地名 + 类别**。

**判断方式：看 `names[].库内精确名`**

| 值 | 含义 | 你该怎么做 |
|---|---|---|
| `true`（默认不输出） | 这就是库里的真实名字 | 可以等值匹配 `WHERE name = '…'` |
| **`false`** | **合成名** | **必须前缀匹配**，用引擎给的 `建议前缀` |

**用错会怎样**：拿「龚家棚台区」做等值查询 → 一条都查不到 → 答「暂无数据」，
而用户会以为真的没数据 —— **比不归一还糟**。

```python
r = corrector.resolve("公家棚线损")
n = r.names[0]
if not n.exact:
    sql_where = f"name LIKE '{n.prefix}%'"        # name LIKE '龚家棚%'
    # n.cand_count 是命中的台区个数，可在答复里说明这是汇总口径
```

> **前缀一定要用 `n.prefix`，不要自己从 `n.std` 里截类型词。**
> `n.std` 是「白鹤变压器」时，自己截会得到 `白鹤变压器%` —— 查不到「白鹤1#变压器」；
> 引擎给的 `n.prefix` 是地名核心「白鹤」，才对。

## 四、返回字段

### `correct(text)` → `Correction`

| 字段 | 说明 |
|---|---|
| `corrected` | 纠错后的文本（**下游用这个**） |
| `changed` | 是否发生过改动 |
| `areas` | 台区名识别。`已改正=true` 表示正文已改成标准名；`false` 表示库里有多候选，**需反问** |
| `unknown_names` | 说的对象不在名录里（**不要反问，把范围退化为默认值**） |
| `entities` | 简称/地名候选，如「随县」可能指「国网随县供电公司」 |
| `need_clarify` | **非空就该反问**，`提示` 是可直接用的话术 |
| `word_fixes` / `name_fixes` / `protected_terms` | 排查用 |
| `original` | 原始输入 |

### `resolve(text)` → `Resolution`

| 字段 | 说明 |
|---|---|
| `resolved` | 归一后的文本 |
| `names[]` | 每处名称的明细：`原文` `标准名` `类别` `置信度` `匹配方式` `多候选` `库内精确名` `建议前缀` `同名候选数` `其他候选` |
| `unresolved[]` | 库里找不到的片段（`文本` + `最接近`） |

## 五、三条使用原则

1. **误改比漏改危害大。** 置信度不够时引擎宁可不动 —— 不要在外部再放宽阈值。
   误改会把用户引到错误的供电所上，得出看似正常实则错误的结论。
2. **纠错和理解分开。** 本库只做「把字改对」；「随县是地名还是公司」这类语义判断交给大模型。
3. **`库内精确名=false` 一定要走前缀匹配**（见第三节）。

## 六、换成自己领域的数据

`data/` 下 7 个 CSV 就是全部知识：

| 文件 | 内容 | 是否要改 |
|---|---|---|
| `catalog.csv` | 标准名词典（名称 + **预计算拼音码**） | **必改**，换成你的名录 |
| `area_core.csv` | 台区核心词码表 | 同上，由 `catalog.csv` 派生 |
| `pinyin_table.csv` | **汉字 →（声母, 韵母）**，2 万字 | 换名录后重跑生成脚本 |
| `wordlist.csv` | 业务词表（电力/政务） | 按领域替换 |
| `typo.csv` | 同音错法表 | 可留可换 |
| `terms.csv` | 术语保护表（防止被改写） | 按领域替换 |
| `ambiguous.csv` | 同音歧义名对（读音撞车、必须反问） | 自动生成 |
| `rules.csv` | 模糊音规则（文档性质，实际规则在 `name_corrector.py` 顶部） | 少改 |

换领域时：改 `data/` + `name_corrector.py` 顶部的模糊音规则/阈值/禁入词表，**代码骨架不用动**。

**注意**：`catalog.csv` / `area_core.csv` 里的拼音码列，以及 `pinyin_table.csv`，
都是**离线预计算**的。**三个文件的口径必须一致**：

1. `pinyin_table.csv`：由 `tools/18_build_pinyin_table.py` 生成。
   它会用你自己的名录做「语境投票」定音 —— 所以换名录后要重跑，
   否则多音字（畜牧 chù/xù、模板 mú/mó）会和索引码对不上。
2. `catalog.csv` / `area_core.csv` 的码列：改了模糊音规则就要重算。

口径不一致的表现是「看着像却匹配不上」——不报错，只是匹配率悄悄下降。
