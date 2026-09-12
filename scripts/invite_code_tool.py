"""打印当前生效的注册邀请码。

邀请码只可能来自两个地方：`config/fund-compass.json` 里显式配置的值（或
`FUND_COMPASS_INVITE_CODE` 环境变量），或者未配置时自动生成、落盘在
`backend/data/.invite-code` 的那个。这个脚本把两种情况归一成同一段输出，
省得每次都要去翻启动日志。

用法：双击 `scripts\\show-invite-code.cmd`，或

    cd backend && ..\\.venv\\Scripts\\python.exe ..\\scripts\\invite_code_tool.py
"""
from __future__ import annotations

import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1] / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app import auth  # noqa: E402  (必须先补 sys.path)


def main() -> int:
    rule = "=" * 52
    print(rule)
    print("  基金罗盘 · 注册邀请码")
    print(rule)

    try:
        policy = auth.invite_policy()
    except Exception as exc:  # pragma: no cover - 取决于部署环境
        print(f"  读取失败：{exc}")
        return 1

    if not policy["inviteRequired"]:
        print("  当前状态：开放注册（不校验邀请码）")
        print()
        print("  任何人都可以注册。想恢复邀请制：把")
        print("  config/fund-compass.json 里 security.invite_code")
        print("  的值删掉，然后重启服务。")
        return 0

    codes = auth.active_invite_codes()
    print(f"  当前状态：邀请制（{len(codes)} 个可用码）")
    print()
    for code in codes:
        print(f"      {code}")
    print()
    print("  发给要邀请的人，填在注册页的「邀请码」栏。")
    print("  大小写、空格、连字符都会被自动忽略。")
    print()
    print("  换码：改 config/fund-compass.json 的 security.invite_code")
    print("  （逗号分隔可配多个），或删掉 backend/data/.invite-code 再重启。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
