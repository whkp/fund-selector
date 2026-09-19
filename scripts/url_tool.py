#!/usr/bin/env python3
"""Show the live public URL, check the whole chain, and hand over a QR code.

Run through scripts\\my-url.cmd so the console stays open long enough to read.
"""
import os
import re
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOG = ROOT / "logs" / "tunnel.log"
QR_PATH = ROOT / "logs" / "public-qr.png"
PORT = 8080

# Named tunnel: the hostname is bound once via
# "cloudflared tunnel route dns fund-compass funds.kpcode.xyz" and never
# changes, unlike the quick-tunnel random prefix.
NAMED_URL = "https://funds.kpcode.xyz"

# cloudflared prints the address once per tunnel; a restart appends a new one,
# so the last match is the live one. Matching only on https:// skips the
# "Requesting new quick Tunnel on trycloudflare.com..." banner lines.
URL_RE = re.compile(r"https://[a-z0-9][a-z0-9-]*\.trycloudflare\.com")


def read_url():
    # Quick tunnel legacy: an old log line still yields the random URL.
    if LOG.exists():
        text = LOG.read_text(encoding="utf-8", errors="replace")
        found = URL_RE.findall(text)
        if found:
            return found[-1]
    # Named tunnel: fixed hostname. A successful start logs its registration;
    # a missing log line still counts when the process itself is alive.
    if cloudflared_running():
        return NAMED_URL
    return None


def port_open(port):
    with socket.socket() as s:
        s.settimeout(2)
        return s.connect_ex(("127.0.0.1", port)) == 0


def cloudflared_running():
    try:
        out = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq cloudflared.exe", "/NH"],
            capture_output=True, text=True, timeout=20,
        ).stdout
        return "cloudflared.exe" in out
    except Exception:
        return False


def probe(url, timeout=20):
    """GET /health through the tunnel: proves the public path really works."""
    req = urllib.request.Request(url + "/health", headers={"User-Agent": "fund-compass"})
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, time.time() - t0, None
    except urllib.error.HTTPError as e:
        return e.code, time.time() - t0, None
    except Exception as e:
        return None, time.time() - t0, type(e).__name__


def to_clipboard(text):
    try:
        subprocess.run("clip", input=text, text=True, shell=True,
                       timeout=15, check=True)
        return True
    except Exception:
        return False


def make_qr(url):
    try:
        import qrcode
    except ImportError:
        return None
    # The default sizing renders a ~250px code, which phones struggle to read
    # off a monitor. box_size 12 lands around 400px and scans first try.
    qr = qrcode.QRCode(
        box_size=12,
        border=3,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
    )
    qr.add_data(url)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")

    # Writing a fixed path fails with EINVAL on Windows while an image viewer
    # still holds the previous PNG open, which is exactly what happens after
    # open_qr() shows it. Each run therefore gets its own filename, and the
    # leftovers are pruned (the one on screen stays, since it is still locked).
    target = QR_PATH.with_name(f"public-qr-{uuid.uuid4().hex[:6]}.png")
    try:
        img.save(target)
    except OSError:
        return None

    for stale in QR_PATH.parent.glob("public-qr*.png"):
        if stale != target:
            try:
                stale.unlink()
            except OSError:
                pass
    return target


def mark(ok):
    return "[OK]  " if ok else "[--]  "


def main():
    line = "=" * 58
    print(line)
    print("  Fund Compass - 公网访问地址")
    print(line)
    print()

    url = read_url()
    if not url:
        print("  还没拿到地址。隧道可能刚启动，等 15 秒再跑一次。")
        print("  如果一直这样，双击 start-public.cmd 把服务拉起来。")
        return 1

    print("     " + url)
    print()

    backend = port_open(PORT)
    tunnel = cloudflared_running()
    print("  " + mark(backend) + f"本地后端      127.0.0.1:{PORT}")
    print("  " + mark(tunnel) + "隧道进程      cloudflared")

    if backend and tunnel:
        status, dt, err = probe(url)
        ok = status == 200
        if err:
            detail = f"连不上 ({err})"
        else:
            detail = f"HTTP {status}  {dt:.2f}s"
        print("  " + mark(ok) + "公网可达     " + detail)
        print()
        if not ok:
            print("  地址在，但公网打不通。常见原因：网络断了、或隧道正在重连。")
            print("  等一会儿再试；还是不行就双击 start-public.cmd。")
    else:
        print()
        print("  服务没起全，公网现在访问不了。")
        print("  双击 start-public.cmd 拉起来，然后再跑一次本脚本。")

    print()
    if to_clipboard(url):
        print("  地址已复制到剪贴板 —— 直接 Ctrl+V 粘给别人。")
    else:
        print("  复制失败，手动选中上面的地址吧。")

    qr = make_qr(url)
    if qr:
        print("  二维码已生成，手机扫它就能打开。")
    print(line)

    if qr:
        try:
            os.startfile(qr)
        except Exception:
            print(f"  二维码在: {qr}")
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    # --url-only keeps show-public-url.cmd a thin wrapper over one parser,
    # so the two entry points can never disagree about the live address.
    if "--url-only" in sys.argv:
        found = read_url()
        print(found if found else "no url logged yet")
        sys.exit(0 if found else 1)
    sys.exit(main())
