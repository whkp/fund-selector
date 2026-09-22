# -*- coding: utf-8 -*-
"""实测逐只接口：风险等级/持仓/分析/盈利概率 + 天天基金 mobapi。"""
import sys, io, json, urllib.request
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
import akshare as ak

CODE = "161725"

def probe(title, fn):
    print(f"\n{'='*20} {title} {'='*20}")
    try:
        df = fn()
        if df is None or len(df) == 0:
            print("!! 返回空"); return
        print("rows:", len(df), "| columns:", list(df.columns))
        print(df.head(8).to_string())
    except Exception as e:
        print("!! ERROR:", repr(e)[:200])

probe("fund_individual_detail_info_xq 概况(找风险等级)", lambda: ak.fund_individual_detail_info_xq(symbol=CODE))
probe("fund_individual_analysis_xq 分析", lambda: ak.fund_individual_analysis_xq(symbol=CODE))
probe("fund_portfolio_hold_em 股票持仓", lambda: ak.fund_portfolio_hold_em(symbol=CODE, date="2025"))
probe("fund_portfolio_industry_allocation_em 行业配置", lambda: ak.fund_portfolio_industry_allocation_em(symbol=CODE, date="2025"))
probe("fund_individual_profit_probability_xq 盈利概率", lambda: ak.fund_individual_profit_probability_xq(symbol=CODE))

# ---- 天天基金 mobapi：风险等级候选 ----
print(f"\n{'='*20} fundmobapi FundMNBasicInformation {'='*20}")
try:
    url = ("https://fundmobapi.eastmoney.com/FundMNBasicInformation/GetFundMNBasicInformation"
           "?FCODE=161725&deviceid=pc&plat=PC&product=EFund&version=6.4.9&pageIndex=1&pageSize=100")
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
    data = json.loads(urllib.request.urlopen(req, timeout=15).read().decode("utf-8"))
    body = data.get("Datas", data)
    keys = [k for k in body.keys() if any(t in k.upper() for t in ("RISK", "RLEVEL", "FETYPE", "STT", "ENDNAV", "MANAGER", "ESTAB"))]
    for k in keys:
        print(k, "=", body[k])
    print("(总字段数:", len(body), ")")
except Exception as e:
    print("!! ERROR:", repr(e)[:200])
