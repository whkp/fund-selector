# -*- coding: utf-8 -*-
"""验证：purchase_em 全量行数 + 蛋卷 fund/{code} 完整字段。"""
import sys, io, json, urllib.request
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
import akshare as ak

df = ak.fund_purchase_em()
print("fund_purchase_em 全量行数:", len(df))
print("申购状态分布:", df["申购状态"].value_counts().to_dict())

req = urllib.request.Request("https://danjuanfunds.com/djapi/fund/161725",
                             headers={"User-Agent": "Mozilla/5.0"})
data = json.loads(urllib.request.urlopen(req, timeout=15).read())["data"]
print("\n蛋卷 data 顶层 key:", list(data.keys()))
fb = data.get("fund_basic_info", {})
print("fund_basic_info:", json.dumps(fb, ensure_ascii=False)[:600])
op = data.get("op_fund", {})
print("op_fund keys:", list(op.keys()))
print("fund_position:", json.dumps(data.get("fund_position", {}), ensure_ascii=False)[:300])
mgr = data.get("manager_list") or data.get("fund_manager_list") or []
print("manager_list:", json.dumps(mgr, ensure_ascii=False)[:300])
