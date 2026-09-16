# -*- coding: utf-8 -*-
import sys, json
sys.stdout.reconfigure(encoding='utf-8')
import tools_db
def q(l, s):
    try:
        print('%-30s %s' % (l, json.dumps(tools_db.run_sql(s)['rows'][:6], ensure_ascii=False, default=str)[:260]))
    except Exception as e:
        print('%-30s ERR %s' % (l, str(e)[:110]))
q('2026年工单数(create_time)', "SELECT COUNT(*) n FROM t_power_work_order WHERE YEAR(create_time)=2026 AND deleted_flag=0")
q('2026年工单数(publish_time)', "SELECT COUNT(*) n FROM t_power_work_order WHERE YEAR(publish_time)=2026 AND deleted_flag=0")
q('工单表全部(不筛年)', "SELECT COUNT(*) n FROM t_power_work_order WHERE deleted_flag=0")
q('预算结果表 最高5', "SELECT power_station_name, budget_amount FROM t_power_company_budget_result WHERE year=2026 AND deleted_flag=0 ORDER BY budget_amount DESC LIMIT 5")
q('预算结果表 最低5', "SELECT power_station_name, budget_amount FROM t_power_company_budget_result WHERE year=2026 AND deleted_flag=0 ORDER BY budget_amount ASC LIMIT 5")
q('台区 loss_rate MAX(全)', 'SELECT MAX(loss_rate) m, MIN(loss_rate) n FROM t_power_area')
q('台区 loss_rate MAX(status=1)', 'SELECT MAX(loss_rate) m FROM t_power_area WHERE status=1')
q('台区 loss_rate Top5', 'SELECT area_name, loss_rate, line_name FROM t_power_area WHERE loss_rate IS NOT NULL ORDER BY loss_rate DESC LIMIT 5')
q('台区 loss_rate Top5(deleted=0)', "SELECT area_name, loss_rate FROM t_power_area WHERE deleted_flag=0 ORDER BY loss_rate DESC LIMIT 5")
