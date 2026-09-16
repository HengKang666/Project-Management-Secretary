# -*- coding: utf-8 -*-
"""核对模型答案的客观真值（只读）。"""
import json
import tools_db

def q(sql, label):
    try:
        r = tools_db.run_sql(sql)
        print(label, '=>', json.dumps(r['rows'][:6], ensure_ascii=False, default=str)[:400])
    except Exception as e:
        print(label, '=> ERR', str(e)[:150])

print('=== 表是否存在 ===')
for t in ['t_governance_effectiveness', 't_power_area_benefit', 't_power_work_cost',
          't_power_push_city_month_summary', 't_power_company_budget_allocation', 't_power_company']:
    r = tools_db._exec("SELECT COUNT(*) c FROM information_schema.tables WHERE table_schema=%s AND table_name=%s",
                       [tools_db.config.SCHEMA, t])
    exists = (r['rows'][0]['c'] or 0) > 0
    print(' ', t, 'EXISTS' if exists else 'NOT-FOUND')

print('=== 关键字段 ===')
for t in ['t_power_push_city_month_summary', 't_power_company_budget_allocation', 't_governance_effectiveness']:
    try:
        cols = [c['col_name'] for c in tools_db.describe_table(t)['rows']]
        print(' ', t, '::', ','.join(cols[:18]))
    except Exception as e:
        print(' ', t, 'ERR', str(e)[:80])

print('=== 真值 ===')
q("SELECT COUNT(*) c, SUM(CASE WHEN status=1 THEN 1 ELSE 0 END) active FROM t_power_company", '供电所数/有效')
q("SELECT year, SUM(total_budget) s, COUNT(*) n FROM t_power_company_budget_allocation GROUP BY year", '预算配置表 按年')
q("SELECT work_status, COUNT(*) c FROM t_power_work_order WHERE deleted_flag<>'1' GROUP BY work_status ORDER BY c DESC", '工单状态分布')
q("SELECT stat_date, city_line_loss_rate FROM t_power_push_city_month_summary ORDER BY stat_date DESC LIMIT 4", '全市月汇总线损')
