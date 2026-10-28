#!/bin/bash
# 定期収集のラッパー (launchd / cron から呼ぶ)。
# .env を読み込み、期限の来た監視対象だけを収集する。ログは data/logs/ に追記。
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_DIR"

if [[ -f .env ]]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

mkdir -p data/logs
LOG="data/logs/watch-$(date +%Y%m).log"
{
  echo "=== $(date '+%Y-%m-%d %H:%M:%S') ==="
  .venv/bin/whisky-ai watch run "$@"
} >>"$LOG" 2>&1
