#!/bin/sh
# マニュアル・オーバーライド用コントロールサーバーへのアクセスURLとQRコードを表示する。
# 実行例: ssh raspi-tesla "./tesla-solar-charge/show_control_url.sh"
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
CONFIG_FILE="$SCRIPT_DIR/tesla_config.json"

TOKEN=$(python3 -c "import json; print(json.load(open('$CONFIG_FILE'))['CONTROL_TOKEN'])")
HOST=$(tailscale status --self --json 2>/dev/null | python3 -c "import json,sys; print(json.load(sys.stdin)['Self']['DNSName'].rstrip('.'))" 2>/dev/null || true)

if [ -z "$HOST" ]; then
    echo "Tailscale MagicDNSホスト名を取得できませんでした。Tailscaleが有効か確認してください。" >&2
    exit 1
fi

URL="https://${HOST}/?token=${TOKEN}"

echo "$URL"
echo
qrencode -t ANSIUTF8 "$URL"
