# -*- coding: utf-8 -*-
"""独立核对真值：不看模型答案，直接查库算。"""
import json
import sys
sys.stdout.reconfigure(encoding='utf-8')
import tools_db


def q(label, sql, key=None):
    try:
        rows = tools_db.run_sql(sql)['rows']
        print('%-34s %s' % (label, json.dumps(rows[:6], ensure_ascii=False, default=str)[:300]))
    except Exception as e:
        print('%-34s ERR %s' % (label, str(e)[:130]))


q('预算总额(is_new=1,2026)', "SELECT SUM(total_budget) s, COUNT(*) n FROM t_power_company_budget_allocation WHERE year=2026 AND is_new=1 AND deleted_flag<>'1'")
q('预算总额(全量2026)', "SELECT SUM(total_budget) s, COUNT(*) n FROM t_power_company_budget_allocation WHERE year=2026 AND deleted_flag<>'1'")
q('预算按公司(is_new=1)', "SELECT county_company_id, SUM(total_budget) s FROM t_power_company_budget_allocation WHERE year=2026 AND is_new=1 AND deleted_flag<>'1' GROUP BY county_company_id")
q('预算分配结果表 表头', "SELECT COUNT(*) n FROM t_power_company_budget_result WHERE deleted_flag<>'1'")
q('预算分配结果 按供电所Top5', "SELECT company_name, SUM(budget_amount) s FROM t_power_company_budget_result WHERE year=2026 AND deleted_flag<>'1' GROUP BY company_name ORDER BY s DESC LIMIT 5")
q('执行率 2026', "SELECT SUM(budget_amount) b, SUM(budget_execution_amount) e FROM t_power_company_assessment WHERE year=2026 AND deleted_flag<>'1'")
q('执行率 2025', "SELECT SUM(budget_amount) b, SUM(budget_execution_amount) e FROM t_power_company_assessment WHERE year=2025 AND deleted_flag<>'1'")
q('工单 状态分布', "SELECT work_status, COUNT(*) c FROM t_power_work_order WHERE deleted_flag<>'1' GROUP BY work_status ORDER BY work_status")
q('工单 总数', "SELECT COUNT(*) n FROM t_power_work_order WHERE deleted_flag<>'1'")
q('逾期未完工', "SELECT COUNT(*) n FROM t_power_work_order WHERE deleted_flag<>'1' AND plan_end_time IS NOT NULL AND plan_end_time < '2026-09-30' AND work_status NOT IN ('7','5')")
q('线损率 202609 两行', "SELECT summary_type, loss_rate, month_loss_rate, sales_volume, average_outage_duration, fault_repair_work_order, feedback_work_order, trip_count FROM t_power_push_city_month_summary WHERE stat_month='202609'")
q('区县 202609 up', "SELECT district_name, loss_rate FROM t_power_push_district_month_summary WHERE stat_month='202609' AND summary_type='upCurrentMonth' ORDER BY loss_rate")
q('供电所数', "SELECT COUNT(*) n FROM t_power_company WHERE status=1 AND deleted_flag<>'1'")
q('台区数', "SELECT COUNT(*) n FROM t_power_area WHERE deleted_flag<>'1'")
q('线路数', "SELECT COUNT(*) n FROM t_power_line WHERE deleted_flag<>'1'")
q('台区线损率最大', "SELECT MAX(loss_rate) m FROM t_power_area WHERE deleted_flag<>'1'")
q('所级售电量Top5', "SELECT power_station_name, power_sold_current FROM t_power_company_assessment WHERE year=2026 AND deleted_flag<>'1' ORDER BY power_sold_current DESC LIMIT 5")
q('所级意见工单Top5', "SELECT county_company_name, power_station_name, opinion_order_count_current FROM t_power_company_assessment WHERE year=2026 AND deleted_flag<>'1' ORDER BY opinion_order_count_current DESC LIMIT 5")
q('按公司 意见工单合计', "SELECT county_company_name, SUM(opinion_order_count_current) s FROM t_power_company_assessment WHERE year=2026 AND deleted_flag<>'1' GROUP BY county_company_name ORDER BY s DESC")
q('有无 三公/GDP/酒店 相关表', "SELECT COUNT(*) n FROM information_schema.tables WHERE table_schema='dlj_data' AND (table_name LIKE '%gdp%' OR table_name LIKE '%hotel%' OR table_name LIKE '%sangong%')")
