#!/bin/bash
# LOTA 引擎启停（服务器上运行）
# 用法：bash engine.sh start | stop | restart | status   （后台启停，自己用 ssh 管）
#       bash engine.sh run                               （前台运行，给进程管理器 / systemd 用）
# 访问：.env 里设了 LOTA_API_TOKEN 就监听 0.0.0.0，直接开 http://<服务器>:8795/flow（网页第一次会问这个口令）；
#       没设就只监听 127.0.0.1，需要 ssh -L 8795:127.0.0.1:8795 <用户>@<服务器> 后开 http://127.0.0.1:8795
#
# 部署相关的路径都写在引擎目录的 deploy.env 里（不进仓库，模板 deploy.env.example），没有就用默认值。
set -u
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"   # 引擎目录：engine.sh、.venv/、app/、runtime/、.env 所在
[ -f "$ROOT/deploy.env" ] && { set -a; . "$ROOT/deploy.env"; set +a; }
: "${LOTA_DATA_ROOT:=$HOME/lota-data}"
: "${LOTA_HOME:=$ROOT/runtime}"
: "${LOTA_IMAGES_DIR:=$LOTA_DATA_ROOT/images}"
: "${LOTA_IMPORTS_DIR:=$LOTA_DATA_ROOT/imports}"
: "${LOTA_LEGACY_PIPELINE:=$LOTA_DATA_ROOT/pipeline}"
# 引擎只允许读这些目录里的原图 / 权重：LOTA_SOURCE_ROOTS、LOTA_WEIGHTS_ROOTS 用冒号分隔，可以加多个
: "${LOTA_SOURCE_ROOTS:=$LOTA_IMAGES_DIR:$LOTA_IMPORTS_DIR}"
: "${LOTA_WEIGHTS_ROOTS:=$HOME/.cache/huggingface/hub}"
export LOTA_DATA_ROOT LOTA_HOME LOTA_IMAGES_DIR LOTA_IMPORTS_DIR LOTA_LEGACY_PIPELINE
PY=$ROOT/.venv/bin/python
ARGS=(--home "$LOTA_HOME")
IFS=: read -ra _src <<<"$LOTA_SOURCE_ROOTS"; for d in "${_src[@]}"; do [ -n "$d" ] && ARGS+=(--source-root "$d"); done
IFS=: read -ra _w <<<"$LOTA_WEIGHTS_ROOTS"; for d in "${_w[@]}"; do [ -n "$d" ] && ARGS+=(--weights-root "$d"); done

running() { pgrep -f "[l]ota --home $LOTA_HOME" >/dev/null; }

# 机密配置（Label Studio 令牌等）放在 $ROOT/.env，格式 LABEL_STUDIO_TOKEN=xxxx；只加载、不打印
if [ -f "$ROOT/.env" ]; then set -a; . "$ROOT/.env"; set +a; fi
# 引擎要求：对外监听必须设访问口令 LOTA_API_TOKEN（防止同一网络里任何人都能操作）
# 改名兼容（原名 AILabel）：.env 里还是旧名 AILABEL_API_TOKEN 也认
LOTA_API_TOKEN="${LOTA_API_TOKEN:-${AILABEL_API_TOKEN:-}}"; export LOTA_API_TOKEN
if [ -n "${LOTA_API_TOKEN:-}" ]; then HOST=0.0.0.0; else HOST=127.0.0.1; fi

start() {
  if running; then echo "已在运行"; status; return; fi
  mkdir -p "$ROOT/logs"
  cd "$ROOT/app" || exit 1
  nohup "$PY" -B -m lota "${ARGS[@]}" serve --host "$HOST" --port 8795 >>"$ROOT/logs/api.log" 2>&1 &
  echo $! >"$ROOT/api.pid"
  nohup "$PY" -B -m lota "${ARGS[@]}" worker >>"$ROOT/logs/worker.log" 2>&1 &
  echo $! >"$ROOT/worker.pid"
  sleep 3; status
}

# 前台运行 API + worker（进程管理器用）：收到停止信号时两个一起停
run() {
  if running; then echo "引擎已在后台运行，先 bash engine.sh stop 再用 run 启动"; exit 1; fi
  [ "$HOST" = 127.0.0.1 ] && echo "提示：.env 里没有 LOTA_API_TOKEN，只能本机/ssh 隧道访问"
  mkdir -p "$ROOT/logs"
  cd "$ROOT/app" || exit 1
  "$PY" -B -m lota "${ARGS[@]}" worker >>"$ROOT/logs/worker.log" 2>&1 &
  W=$!
  "$PY" -u -B -m lota "${ARGS[@]}" serve --host "$HOST" --port 8795 &
  A=$!
  echo "API pid $A · worker pid $W · http://$HOST:8795/flow"
  trap 'kill $A $W 2>/dev/null; wait; exit 0' TERM INT
  wait -n; kill $A $W 2>/dev/null; wait
}

stop() {
  pkill -f "[l]ota --home $LOTA_HOME" && echo "已停止" || echo "本来就没在运行"
  sleep 2
}

status() {
  pgrep -af "[l]ota --home $LOTA_HOME" | sed -E 's/--source-root.*(serve|worker)/… \1/' || echo "未运行"
  curl -s -m 3 http://127.0.0.1:8795/api/health && echo
}

case "${1:-status}" in
  start) start ;; run) run ;; stop) stop ;; restart) stop; start ;; status) status ;;
  *) echo "用法：$0 start|run|stop|restart|status"; exit 1 ;;
esac
