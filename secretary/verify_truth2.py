# -*- coding: utf-8 -*-
import json
import tools_db

def q(sql, label):
    try:
        print(label, '=>', json.dumps(tools_db.run_sql(sql)['rows'][:8], ensure_ascii=False, default=str)[:420])
    except Exception as e:
        print(label, '=> ERR', str(e)[:130])

q("SELECT stat_month, loss_rate, month_loss_rate, sales_volume FROM t_power_push_city_month_summary WHERE deleted_flag<>'1' ORDER BY stat_month DESC LIMIT 4", '全市月汇总(正确列名)')
q("SELECT year, is_new, COUNT(*) n, SUM(total_budget) s FROM t_power_company_budget_allocation WHERE deleted_flag<>'1' GROUP BY year, is_new", '预算配置 按年+is_new')
q("SELECT SUM(actual_duration) s FROM t_power_work_order LIMIT 1", '冒烟')
q("SELECT work_status, COUNT(*) c, SUM(budget_amount) b FROM t_power_work_order WHERE deleted_flag<>'1' GROUP BY work_status ORDER BY c DESC", '工单状态+预算')
