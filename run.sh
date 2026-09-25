#!/usr/bin/env bash
# 单条本地启动命令：重建虚构样例数据（进出场/扣费/复核）并启动平台。
#   ./run.sh            端口 8000，如已有样例数据则直接启动
#   ./run.sh --reset    删除本地数据后重新生成全部样例
#   PARKING_PORT=9000 ./run.sh
set -euo pipefail
cd "$(dirname "$0")"

PORT="${PARKING_PORT:-8000}"
export PARKING_PORT="$PORT"

if [ "${1:-}" = "--reset" ]; then
  echo ">> 重建虚构样例数据 ..."
  python3 -m parking.seed --reset
else
  python3 -m parking.seed
fi

echo ">> 启动平台 http://localhost:${PORT}"
exec python3 -m parking.server
