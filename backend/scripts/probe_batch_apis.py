# -*- coding: utf-8 -*-
"""实测候补接口：每类打印列名 + 样例行，验证能否补缺口。"""
import sys, io, traceback
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
import akshare as ak

def probe(title, fn):
    print(f"\n{'='*20} {title} {'='*20}")
    try:
        df = fn()
        if df is None or len(df) == 0:
            print("!! 返回空"); return
        print("rows:", len(df), "| columns:", list(df.columns))
        print(df.head(2).to_string())
    except Exception as e:
        print("!! ERROR:", repr(e)[:200])

# ---- 批量接口（一次调用覆盖全市场）----
probe("fund_purchase_em 申购状态", lambda: ak.fund_purchase_em().head(3000))
probe("fund_value_estimation_em 盘中估值", lambda: ak.fund_value_estimation_em().head(3000))
probe("fund_rating_all 评级", lambda: ak.fund_rating_all())
probe("fund_manager_em 基金经理", lambda: ak.fund_manager_em())
probe("fund_hold_structure_em 持有人结构", lambda: ak.fund_hold_structure_em())
