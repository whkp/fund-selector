# -*- coding: utf-8 -*-
"""盘点 AKShare 可用基金接口 + 实测候补接口能否补缺口字段。"""
import sys, io, json
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import akshare as ak

print("akshare version:", ak.__version__)
names = sorted(n for n in dir(ak) if n.startswith("fund_"))
print("fund_* 接口总数:", len(names))
for n in names:
    print(" -", n)
