#!/bin/sh
# 端到端演练：把线上数据只读复制到临时目录，用假的飞书命令行和模拟推送把各条链路跑一遍。
# 不碰线上的数据、配置、xinxiang 入口和飞书；结果写在临时目录的 results.json。
#   tools/e2e/run.sh            跑一遍，退出码 0 = 全部通过
#   KEEP=1 tools/e2e/run.sh     跑完保留临时目录，方便查看
set -e
ROOT=$(cd "$(dirname "$0")/../.." && pwd)
TMP=$(mktemp -d "${TMPDIR:-/tmp}/xinxiang-e2e.XXXXXX")
SANDBOX="$TMP/演练-心响/sandbox"   # 带中文，和真实项目路径一样，AI 工作文件夹会放到测试主目录下
mkdir -p "$TMP/home"
"$ROOT/.venv/bin/python" "$ROOT/tools/e2e/setup.py" "$ROOT" "$SANDBOX" "$ROOT/tools/e2e/lark-cli" >/dev/null
set +e
(cd "$SANDBOX" && env HOME="$TMP/home" PYTHONPATH="$ROOT/src" "$ROOT/.venv/bin/python" -u "$ROOT/tools/e2e/run.py" 2>&1 \
  | grep -v '^[0-9][0-9]-[0-9][0-9] [0-9:]* ')
code=$?
set -e
if [ -n "$KEEP" ]; then echo "临时目录：$TMP"; else rm -rf "$TMP"; fi
exit $code
