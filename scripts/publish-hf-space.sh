#!/usr/bin/env bash
#
# 把本仓库的后端组装成一个 Hugging Face Space 仓库并推送。
# 适用场景：Render 免费版 512MB 内存跑 AKShare 不够用，改走 Hugging Face Spaces
#（免费 CPU basic：2 vCPU / 16GB 内存 / 48 小时无访问才休眠）。
#
# 前置：
#   1. 在 https://huggingface.co/new-space 创建 Space，SDK 选 Docker，名称与 SPACE_NAME 一致
#   2. 在 https://huggingface.co/settings/tokens 生成一个有 Write 权限的 Access Token
#
# 用法（Git Bash / Linux / macOS）：
#   HF_USER=你的用户名 HF_TOKEN=hf_xxxxxxxx bash scripts/publish-hf-space.sh
#
# 可选环境变量：
#   SPACE_NAME   默认 fund-compass-api
set -euo pipefail

: "${HF_USER:?请先设置 HF_USER（Hugging Face 用户名）}"
: "${HF_TOKEN:?请先设置 HF_TOKEN（Hugging Face Access Token，需 Write 权限）}"
SPACE_NAME="${SPACE_NAME:-fund-compass-api}"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT

echo "==> 组装 Space 内容到 $STAGE"
mkdir -p "$STAGE"
cp -R "$REPO_ROOT/backend" "$STAGE/backend"
cp -R "$REPO_ROOT/config" "$STAGE/config"
cp "$REPO_ROOT/Dockerfile" "$STAGE/Dockerfile"

# 清理不需要进入 Space 的产物（测试、迁移脚本、字节码）
find "$STAGE" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
rm -rf "$STAGE/backend/tests" "$STAGE/backend/migrations"
rm -f "$STAGE/backend/alembic.ini" "$STAGE/backend/pyproject.toml"

# Hugging Face 只认根目录的 README.md 作为 Space 元信息载体
cat > "$STAGE/README.md" <<'YAML'
---
title: Fund Compass API
emoji: 📈
colorFrom: blue
colorTo: green
sdk: docker
app_port: 7860
pinned: false
---

# Fund Compass API

基金罗盘后端：FastAPI + AKShare 公开数据参考源。仅供研究参考，不构成投资建议。
YAML

cd "$STAGE"
git init -q -b main
git add -A
git -c user.name="deploy" -c user.email="deploy@localhost" commit -qm "deploy: fund compass api"

echo "==> 推送到 https://huggingface.co/spaces/${HF_USER}/${SPACE_NAME}"
git remote add space "https://${HF_USER}:${HF_TOKEN}@huggingface.co/spaces/${HF_USER}/${SPACE_NAME}"
git push -q space main --force

echo
echo "==> 完成。构建状态：https://huggingface.co/spaces/${HF_USER}/${SPACE_NAME}"
echo "    构建完成后接口地址：https://${HF_USER}-${SPACE_NAME}.hf.space"
echo "    把该地址加 /api 填到前端 VITE_API_BASE，并加入后端 FUND_COMPASS_CORS_ORIGINS。"
