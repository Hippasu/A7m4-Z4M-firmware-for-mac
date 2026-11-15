#!/usr/bin/env bash
# Publish github/ tree + Release assets (ZM4-release-v1.0.dmg, BODYDATA.DAT)
set -euo pipefail
cd "$(dirname "$0")"
export PATH="/opt/homebrew/bin:$PATH"

REPO_NAME="${1:-A7m4-Z4M-firmware-for-mac}"
VISIBILITY="${2:-public}"   # public | private
TAG="${3:-v1.0}"

need() { command -v "$1" >/dev/null || { echo "missing $1" >&2; exit 1; }; }
need gh
need git

if ! gh auth status >/dev/null 2>&1; then
  echo "先登录：gh auth login --web" >&2
  echo "浏览器打开 https://github.com/login/device 并输入终端显示的一次性代码" >&2
  exit 2
fi

OWNER="$(gh api user -q .login)"
FULL="$OWNER/$REPO_NAME"
echo "GitHub user: $OWNER"
echo "Target repo: $FULL ($VISIBILITY)"

# Create repo if missing
if ! gh repo view "$FULL" >/dev/null 2>&1; then
  gh repo create "$REPO_NAME" --"$VISIBILITY" --source=. --remote=origin \
    --description "A7m4(Z4M) firmware for mac"
else
  git remote remove origin 2>/dev/null || true
  git remote add origin "https://github.com/$FULL.git"
fi

git push -u origin main

# Ensure assets exist (Release only — not in git)
DMG="ZM4-release-v1.0.dmg"
DAT="BODYDATA.DAT"
[[ -f "$DMG" ]] || { echo "missing $DMG" >&2; exit 1; }
[[ -f "$DAT" ]] || { echo "missing $DAT" >&2; exit 1; }

# Create or update release
if gh release view "$TAG" --repo "$FULL" >/dev/null 2>&1; then
  echo "Release $TAG exists — uploading/replacing assets"
  gh release upload "$TAG" "$DMG" "$DAT" --repo "$FULL" --clobber
else
  gh release create "$TAG" "$DMG" "$DAT" \
    --repo "$FULL" \
    --title "A7m4(Z4M) firmware for mac $TAG" \
    --notes "$(cat <<NOTES
## A7m4(Z4M) firmware for mac · $TAG

### 下载
- \`ZM4-release-v1.0.dmg\` — Apple Silicon（M 系列）安装包
- \`BODYDATA.DAT\` — 按 App 操作说明放入相机卡槽 1 根目录

### 安装
1. 打开 DMG，拖到「应用程序」
2. 若被拦截：右键 → 打开
3. 目标机无需 Homebrew / Python / Zig

源码树为公开诊断壳；完整安装能力在 DMG 内。
NOTES
)"
fi

echo
echo "OK: https://github.com/$FULL"
echo "Release: https://github.com/$FULL/releases/tag/$TAG"
