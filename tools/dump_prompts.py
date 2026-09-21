# -*- coding: utf-8 -*-
"""导出本项目用到的全部提示词到一个 Markdown 文件，便于集中查看与评审。

提示词分两处存放：
  ① 数据库 `ai_data.ai_prompt`  —— 主来源。运营在库里改，服务启动时读（config.PROMPTS 指定 key）
  ② 代码里硬编码                —— 问题补全（agent.COMPLETE_SYSTEM）、
                                   报告生成（report.BASE_SYSTEM）、兜底（agent._FALLBACK）
  ③ 外部文档                    —— report 还会拼 skills/ 下的两个 md（见文末「缺文件」提醒）

用法：
    python tools/dump_prompts.py                 # 输出到 工作区根/系统提示词清单.md
    python tools/dump_prompts.py <输出路径.md>
"""
from __future__ import annotations

import ast
import os
import re
import sys

import pymysql

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)                                  # deploy_server2.0/
SEC = os.path.join(ROOT, 'secretary')
OUT = os.path.normpath(os.path.join(ROOT, '..', '系统提示词清单.md'))

# 服务真正注入的 key（config.PROMPTS 的默认值）
IN_USE = ('answer_agent', 'business_rules', 'sql_plan_rules')

# 代码里硬编码的提示词：(文件, 变量名, 说明)
CODE_PROMPTS = [
    ('agent.py', 'COMPLETE_SYSTEM', '问题补全（第一步，把口语问法补成标准查询要求）'),
    ('agent.py', '_FALLBACK', '兜底系统提示（正常情况下用不到，数据库读不到时才生效）'),
    ('report.py', 'BASE_SYSTEM', '报告生成（写上报分析报告时的硬约束）'),
]


def read_env(path=None):
    path = path or os.path.join(ROOT, '.env')
    d = {}
    with open(path, encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            k, _, v = line.partition('=')
            d[k.strip()] = re.split(r'\s+#', v, maxsplit=1)[0].strip()
    return d


def grab_const(filename, varname):
    """从源码里取模块级字符串常量的值（用 ast，避免手工复制出错）。"""
    path = os.path.join(SEC, filename)
    try:
        tree = ast.parse(open(path, encoding='utf-8').read())
    except Exception as e:                                    # noqa: BLE001
        return None, '解析失败：%s' % e
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id == varname:
                    try:
                        return ast.literal_eval(node.value), None
                    except Exception as e:                    # noqa: BLE001
                        return None, '取值失败（不是纯字符串常量）：%s' % e
    return None, '源码里没找到这个变量'


def main():
    out_path = sys.argv[1] if len(sys.argv) > 1 else OUT
    env = read_env()
    conn = pymysql.connect(host=env['DB_HOST'], port=int(env['DB_PORT']),
                           user=env['DB_USER'], password=env['DB_PASSWORD'],
                           database='ai_data', charset='utf8mb4', connect_timeout=10,
                           cursorclass=pymysql.cursors.DictCursor)
    cur = conn.cursor()
    cur.execute("""SELECT id, prompt_key, prompt_name, prompt_content, remark,
                          deleted_flag, version, IS_new, update_time
                   FROM ai_prompt ORDER BY prompt_key, IS_new DESC, version DESC""")
    rows = cur.fetchall()
    conn.close()

    L = []
    L.append('# 系统提示词清单\n')
    L.append('> 由 `deploy_server2.0/tools/dump_prompts.py` 自动导出，**不要手改本文件**，'
             '改了重跑脚本即可。\n')
    L.append('\n## 一、提示词存放在哪（共三处）\n')
    L.append('| # | 位置 | 谁维护 | 服务怎么用 |\n|---|---|---|---|')
    L.append('| ① | 数据库 `ai_data.ai_prompt` 表 | 运营/业务，改完重启服务生效 | '
             '`agent._system()` 取 `config.PROMPTS` 指定的 key，拼进系统提示 |')
    L.append('| ② | 代码常量（3 处，见第四节） | 开发 | 问题补全 / 报告生成 / 兜底，直接写死在代码里 |')
    L.append('| ③ | `skills/*.md` 两个文档 | 开发 | `report._system()` 拼进报告提示（**部署包里目前缺这两个文件**） |')
    L.append('\n服务实际注入的 key（`config.PROMPTS` 默认值）：`%s`\n'
             % '`, `'.join(IN_USE))
    L.append('> 想换 key 或加 key：设环境变量 `SECRETARY_PROMPTS=key1,key2`。\n')

    L.append('\n## 二、数据库里的全部 %d 行\n' % len(rows))
    L.append('| id | prompt_key | 名称 | IS_new | version | 已删除 | 字数 | 更新时间 |')
    L.append('|---|---|---|---|---|---|---|---|')
    for r in rows:
        L.append('| %s | `%s` | %s | %s | %s | %s | %s | %s |' % (
            r['id'], r['prompt_key'], r['prompt_name'], r['IS_new'], r['version'],
            '是' if r['deleted_flag'] else '', len(r['prompt_content'] or ''),
            r['update_time']))

    L.append('\n## 三、服务实际在用的提示词（数据库）\n')
    for k in IN_USE:
        hit = [r for r in rows if r['prompt_key'] == k and r['IS_new'] == 1
               and not r['deleted_flag']]
        if not hit:
            L.append('\n### %s\n\n**表里没有可用的行（IS_new=1 且未删除）**\n' % k)
            continue
        r = hit[0]
        L.append('\n### %s —— %s\n' % (k, r['prompt_name']))
        L.append('- 取的是 id=%s，version=%s，更新时间 %s' % (r['id'], r['version'], r['update_time']))
        if r['remark']:
            L.append('- 备注：%s' % r['remark'])
        if len(hit) > 1:
            L.append('- ⚠️ **有多行 IS_new=1**（id=%s），取哪一行取决于排序，'
                     '建议只保留一行最新版' % '、'.join(str(x['id']) for x in hit))
        L.append('\n```text\n%s\n```\n' % (r['prompt_content'] or '').strip())

    L.append('\n## 四、代码里硬编码的提示词\n')
    for fname, var, note in CODE_PROMPTS:
        val, err = grab_const(fname, var)
        L.append('\n### `%s` 里的 `%s`\n' % (fname, var))
        L.append('- 用途：%s' % note)
        if err:
            L.append('- ⚠️ 导出失败：%s（请直接看 %s）' % (err, fname))
            continue
        L.append('\n```text\n%s\n```\n' % (val or '').strip())

    L.append('\n## 五、报告功能依赖的两个外部文档\n')
    for name in ('年度缺陷治理计划分析.md', '数据表说明书.md'):
        p = os.path.join(ROOT, 'skills', name)
        L.append('- `skills/%s` —— %s' % (name, '存在' if os.path.exists(p) else '**缺失**'))
    L.append('\n> `report._system()` 会把这两个文档拼进提示。读不到时不会报错，'
             '只在提示里留下「技能文档读取失败」字样，**报告质量会静默下降**。\n')

    with open(out_path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(L))
    print('已导出：%s' % out_path)
    print('  数据库提示词 %d 行，服务在用 %d 个 key，代码硬编码 %d 处'
          % (len(rows), len(IN_USE), len(CODE_PROMPTS)))


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    main()
