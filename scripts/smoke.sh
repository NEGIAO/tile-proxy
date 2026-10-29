#!/usr/bin/env bash
# 部署后冒烟：四条纠偏路由 + 直通(ESRI) + 层级提示图。
# 用途：改动 rectfy/路由后立刻跑一遍，避免「某个符号被误删」这类静默 502。
#
#   ./scripts/smoke.sh                       # 默认打本机 127.0.0.1:9002
#   ./scripts/smoke.sh https://vpn.negiao.cn # 打线上
set -u

BASE="${1:-http://127.0.0.1:9002}"
AMAP="http://webrd01.is.autonavi.com/appmaptile?lang=zh_cn&size=1&scale=1&style=8&x=53956&y=24830&z=16"
BD="https://maponline0.bdimg.com/tile/?qt=vtile&x=53956&y=24830&z=16&styles=pl&scaler=1&udt=20240101"
CARTO="https://a.basemaps.cartocdn.com/rastertiles/voyager/16/53956/24830.png"
# wgs2bd 的输出索引是**百度网格**，不能拿标准 XYZ 的坐标试（会 400 越界）
CARTO_BDGRID="https://a.basemaps.cartocdn.com/rastertiles/voyager/16/1581/589.png"
ESRI="wayback-a.maptiles.arcgis.com/arcgis/rest/services/World_Imagery/WMTS/1.0.0/default028mm/MapServer/tile/26334/17/51994/107153"

fail=0
check() { # name expect_ct url [min_bytes]
  local name="$1" want="$2" url="$3" minb="${4:-500}"
  local out code ct size
  out=$(curl -sS -m 30 -o /tmp/smoke.$$ -w "%{http_code} %{content_type} %{size_download}" "$url" 2>/dev/null) || out="000 - 0"
  code=${out%% *}; rest=${out#* }; ct=${rest%% *}; size=${rest##* }
  # want="-" 表示不校验 content-type（线上 /health 由 nginx 直接回 "ok"）
  if [ "$code" = "200" ] && { [ "$want" = "-" ] || [ "${ct%%;*}" = "$want" ]; } && [ "$size" -ge "$minb" ]; then
    printf "  \033[32mPASS\033[0m %-10s %s %sB\n" "$name" "$ct" "$size"
  else
    printf "  \033[31mFAIL\033[0m %-10s http=%s ct=%s size=%s\n" "$name" "$code" "$ct" "$size"
    fail=1
  fi
  rm -f /tmp/smoke.$$
}

echo "smoke: $BASE"
check "health"   "-"                "$BASE/health" 2
check "gcj2wgs"  "image/png"        "$BASE/proxy/gcj2wgs/$AMAP"
check "wgs2gcj"  "image/png"        "$BASE/proxy/wgs2gcj/$CARTO"
check "bd2wgs"   "image/png"        "$BASE/proxy/bd2wgs/$BD"
check "wgs2bd"   "image/png"        "$BASE/proxy/wgs2bd/$CARTO_BDGRID"
check "esri直通" "image/jpeg"       "$BASE/proxy/$ESRI"

# 层级过大：应返回 error2 提示图（200 + image/png）
z19=$(curl -sS -m 20 -o /tmp/smoke19.$$ -w "%{http_code}" "$BASE/proxy/gcj2wgs/http://webst01.is.autonavi.com/appmaptile?style=6&x=107156&y=51996&z=19" 2>/dev/null)
if [ "$z19" = "200" ]; then
  printf "  \033[32mPASS\033[0m %-10s 200\n" "z19越界"
else
  printf "  \033[31mFAIL\033[0m %-10s http=%s\n" "z19越界" "$z19"; fail=1
fi
rm -f /tmp/smoke19.$$

[ "$fail" = "0" ] && echo "全部通过" || echo "有失败项"
exit "$fail"
