# -*- coding: utf-8 -*-
"""生成「数据地图」：想查什么 → 大概该看哪张表。素材：业务库表注释 + 上一版服务 app/repositories.py 的白名单查询。"""
import re
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding='utf-8')
import tools_db

REPO = Path(r'D:\skill回答后端\app\repositories.py')

# 1) 全表清单（表名 / 中文注释 / 估算行数）
tables = tools_db.list_tables('')['rows']
byname = {t['tbl_name']: t for t in tables}
print('表数 =', len(tables))

# 2) 上一版服务的白名单查询：fetch 键 → 用到的表
src = REPO.read_text(encoding='utf-8')
methods = re.findall(r'def (_query_[a-z0-9_]+)\(([^)]*)\):(.*?)(?=\n    def |\Z)', src, re.S)
mapping = []
for name, _args, body in methods:
    found = []
    for m in re.finditer(r'(?:FROM|JOIN)\s+([A-Za-z_][A-Za-z0-9_]*)', body, re.I):
        t = m.group(1)
        if t not in found:
            found.append(t)
    mapping.append((name.replace('_query_', ''), [t for t in found if t in byname]))
print('白名单查询 =', len(mapping))

# 3) 主题分组（按表名前缀 + 注释关键词）
THEMES = [
    ('预算与资金', ['budget', 'cost', 'contract', 'pay', 'fund']),
    ('项目与工单', ['work_order', 'project', 'acceptance', 'audit_task', 'flow']),
    ('线损与供电可靠性', ['loss', 'summary', 'outage', 'saidi', 'fault', 'feedback', 'ticket']),
    ('供电所 / 公司主数据', ['company', 'supply_company', 'station']),
    ('线路 / 台区 / 设备', ['line', 'area', 'transformer', 'device', 'asset']),
    ('成本定额与物料价', ['engineering', 'material']),
    ('推送汇总（中台）', ['push']),
]
def theme_of(name, comment):
    s = (name + ' ' + (comment or '')).lower()
    for label, keys in THEMES:
        if any(k in s for k in keys):
            return label
    return '其他'

groups = {}
for t in tables:
    groups.setdefault(theme_of(t['tbl_name'], t['tbl_comment']), []).append(t)

lines = []
lines.append('# 数据地图：想查什么，大概去哪张表')
lines.append('')
lines.append('这份文件回答"我要的数据大概在哪张表"。表名与注释都来自业务库 dlj_data 实测（133 张表）。')
lines.append('口径与公式见同库的《口径库》；本文件只负责"去哪找"。')
lines.append('')
lines.append('## 一、先看这几条')
lines.append('')
lines.append('1. 表名前缀有含义：t_power_push_* 是中台推送的月度/年度汇总（最常用、最干净）；')
lines.append('   t_power_* 是业务明细与台账；t_cost_* 是成本定额与物料价；stg_* 是暂存表（通常为空，别用）；')
lines.append('   *_bak_* / *_copy* / *_ago_img_* 是备份或历史副本，**不要当数据源**。')
lines.append('2. 表注释（中文）是判断用途的第一线索，先 list_tables 按关键词筛，再 describe_table 看字段。')
lines.append('3. 汇总类数据（线损率、停电时长、工单量、售电量）优先看 t_power_push_*_month_summary / _year_summary，')
lines.append('   不要从明细表自己加总，除非口径库明确要求。')
lines.append('4. 估算行数是 InnoDB 估算值，可能严重偏离（有的估算 25 万实际 0 行）。**用前先 COUNT 一次**。')
lines.append('')

lines.append('## 二、按主题分组')
lines.append('')
for label, _ in THEMES + [('其他', [])]:
    items = groups.get(label) or []
    if not items:
        continue
    lines.append('### ' + label + '（%d 张）' % len(items))
    lines.append('')
    lines.append('| 表 | 注释 | 估算行数 |')
    lines.append('|---|---|---|')
    for t in items:
        lines.append('| %s | %s | %s |' % (t['tbl_name'], (t['tbl_comment'] or '').replace('|', '/'), t['tbl_rows']))
    lines.append('')

lines.append('## 三、上一版服务实际用过的表（按查询键）')
lines.append('')
lines.append('下面是上一版「项目管理秘书」白名单查询实际访问的表，说明这些表在业务上是可信数据源：')
lines.append('')
lines.append('| 查询键 | 用到的表 |')
lines.append('|---|---|')
for key, ts in mapping:
    if ts:
        lines.append('| %s | %s |' % (key, ', '.join(ts)))
lines.append('')

out = Path(Path(__file__).resolve().parent / '数据地图.md')
out.write_text('\n'.join(lines), encoding='utf-8')
print('written', out, len(lines), 'lines')
