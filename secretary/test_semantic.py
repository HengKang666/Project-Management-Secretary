# -*- coding: utf-8 -*-
"""语义层自检：白名单只认他们在线上维护的 ai_data.*，不含我们自己造的表。不依赖大模型。"""
import io, sys
sys.path.insert(0, __file__.rsplit(chr(92), 1)[0])
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
import semantic
import tools_db


def check(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print("  ok  " + msg)


print("1) 字典只来自他们的表")
allowed = semantic.allowed_tables()
print("   白名单 %d 张：%s" % (len(allowed), "、".join(sorted(allowed))))
check("t_power_work_order" in allowed, "工单表在名单里")
check("t_power_company_budget_allocation" in allowed, "预算配置表在名单里")
check("t_power_ai_metric_snapshot" in allowed, "六项指标快照表在名单里")
check("t_power_ai_metric_comparison" in allowed, "同比环比表在名单里")
check("t_power_station_monthly_stat" not in allowed, "字典里没有的表不在名单里")
check("t_power_area_bak_20260901" not in allowed, "备份表不在名单里")
check("t_power_work_cost" not in allowed, "定额表不在名单里")
menu = semantic.table_menu()
check(menu.count(chr(10)) > 16, "菜单含表目录与字段目录")
check("他们登记的指标口径" in menu, "菜单含他们登记的指标口径")
check(len(semantic.metrics()) >= 20, "读到指标口径 %d 条" % len(semantic.metrics()))

print("2) SQL 表名抽取")
cases = [
    ("SELECT * FROM t_power_work_order", {"t_power_work_order"}),
    ("SELECT * FROM dlj_data.t_power_company WHERE id=1", {"t_power_company"}),
    ("SELECT a.id FROM t_power_company a JOIN t_power_supply_company b ON a.id=b.id", {"t_power_company", "t_power_supply_company"}),
    ("SELECT * FROM t_power_company, t_power_company_assessment WHERE 1", {"t_power_company", "t_power_company_assessment"}),
    ("WITH x AS (SELECT id FROM t_power_work_order) SELECT * FROM x", {"t_power_work_order"}),
]
for sql, want in cases:
    got = semantic.tables_in_sql(sql)
    check(got == want, "抽表：%s -> %s" % (sql[:44], sorted(got)))

print("3) 白名单拦截")
r = tools_db.run_sql("SELECT COUNT(*) c FROM t_power_station_monthly_stat")
check("error" in r and "可用表" in r["error"], "没登记的表被拒并回可用表名")
r = tools_db.run_sql("SELECT COUNT(*) c FROM t_power_work_cost")
check("error" in r, "定额表被拒")
r = tools_db.run_sql("SELECT COUNT(*) c FROM information_schema.tables")
check("error" in r, "information_schema 被拒")
r = tools_db.run_sql("SELECT COUNT(*) c FROM t_power_supply_company")
check("error" not in r and r["rows"][0]["c"] == 4, "名单内表能查（供电公司 4 行）")

print("4) 三个看表工具")
r = tools_db.describe_table("t_power_station_monthly_stat")
check("error" in r, "describe 没登记的表被拒")
r = tools_db.describe_table("t_power_company_budget_allocation")
check("error" not in r and r["all_fields"] == 13, "describe 预算配置表 13 个字段")
check(any(x.get("col_cn_name") == "总预算" for x in r["rows"]), "describe 带出字典中文名")
r = tools_db.find_column("预算")
check(set(x["tbl_name"] for x in r["rows"]) <= allowed, "find_column 只回白名单表")
r = tools_db.list_tables()
check(r["row_count"] == len(allowed), "list_tables 只列白名单（%d 张）" % r["row_count"])

print("全部通过")
