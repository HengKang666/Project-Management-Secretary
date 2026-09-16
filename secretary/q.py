# -*- coding: utf-8 -*-
import sys
sys.stdout.reconfigure(encoding='utf-8')
import tools_db

SQL = """SELECT stat_month, summary_type, loss_rate, month_loss_rate, sales_volume,
average_outage_duration, fault_repair_work_order, feedback_work_order, trip_count
FROM t_power_push_city_month_summary ORDER BY stat_month DESC LIMIT 8"""
for r in tools_db.run_sql(SQL)['rows']:
    print(r)
