# -*- coding: utf-8 -*-
"""探测蛋卷(雪球)原始 API 与天天基金 mobapi 的风险等级字段。"""
import sys, io, json, urllib.request
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

CODE = "161725"
HDRS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "application/json"}

def get_json(url):
    req = urllib.request.Request(url, headers=HDRS)
    return json.loads(urllib.request.urlopen(req, timeout=15).read().decode("utf-8"))

def walk(obj, path=""):
    """遍历 JSON，打印 key 含 risk/RISK/风险 的字段。"""
    hits = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            p = f"{path}.{k}" if path else k
            if any(t in str(k).lower() for t in ("risk", "level")) or (isinstance(v, str) and "风险" in v and len(v) < 20):
                hits.append((p, v))
            hits += walk(v, p)
    elif isinstance(obj, list):
        for i, v in enumerate(obj[:3]):
            hits += walk(v, f"{path}[{i}]")
    return hits

# ---- 蛋卷 API ----
for url in (f"https://danjuanfunds.com/djapi/fund/{CODE}",
            f"https://danjuanfunds.com/djapi/fund/detail/{CODE}"):
    print("=" * 15, url, "=" * 15)
    try:
        data = get_json(url)
        for p, v in walk(data)[:30]:
            print(f"  {p} = {v}")
    except Exception as e:
        print("  !! ERROR:", repr(e)[:150])

# ---- 天天基金 mobapi 变体 ----
for path in ("FundMNDetailInformation/PageDetail", "FundMNBasicInformation/GetFundMNBasicInformation"):
    url = (f"https://fundmobapi.eastmoney.com/{path}?FCODE={CODE}"
           "&deviceid=Wap&plat=Wap&product=EFund&version=2.0.0&pageIndex=1&pageSize=100")
    print("=" * 15, path, "=" * 15)
    try:
        data = get_json(url)
        print("  ErrCode:", data.get("ErrCode"), "| Datas keys:", len(data.get("Datas") or {}))
        if data.get("Datas"):
            for p, v in walk({"Datas": data["Datas"]})[:30]:
                print(f"  {p} = {v}")
    except Exception as e:
        print("  !! ERROR:", repr(e)[:150])
