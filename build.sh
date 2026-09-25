#!/usr/bin/env bash
# 실행파일 하나(dist/TokenWidget)를 만들고 바로 실행한다.
#   - 실행파일에는 파이썬·Tk·Pillow·D-Bus 모듈이 전부 들어 있다 → 다른 PC 에는 이 파일 하나만 복사해 실행하면 된다.
#   - 실행파일은 처음 실행될 때 ~/.local 에 스스로 설치되고 앱 목록·로그인 자동 실행에 등록된다(sudo 불필요).
# 빌드에만 필요한 것: python3(tkinter 포함) + PyPI 접속. 프록시·사내 인증서는 알아서 찾아 쓴다.
set -euo pipefail
cd "$(dirname "$0")"

say() { printf '\n\033[1m%s\033[0m\n' "$*"; }
warn() { printf '\033[33m%s\033[0m\n' "$*" >&2; }

APP=TokenWidget
# 표준 python-build-standalone 은 Tk 9.0 이라 PyInstaller onefile 이 libtcl9 를 못 찾는다.
# → 반드시 배포판 파이썬(Tk 8.6)으로 만든다. 오래된 배포판에서 빌드할수록 더 많은 PC 에서 돈다(glibc).
PY=/usr/bin/python3
[ -x "$PY" ] || PY="$(command -v python3 || true)"
[ -n "$PY" ] || { warn "python3 가 없습니다. 먼저 python3 를 설치하세요."; exit 1; }

# ---------- 0. 네트워크: 프록시 · 사내 인증서 ----------
# 셸에 프록시가 없으면 ~/.claude/settings.json 의 env → GNOME 수동 프록시 순으로 찾아 쓴다.
# 사내 루트 인증서(SSL 검사)는 시스템 저장소 + NODE_EXTRA_CA_CERTS 를 합쳐서 pip/uv 에 넘긴다.
read -r cl_proxy cl_ca < <("$PY" - <<'EOF' 2>/dev/null || echo "- -"
import json, os, pathlib
p = pathlib.Path(os.environ.get("CLAUDE_CONFIG_DIR") or pathlib.Path.home() / ".claude") / "settings.json"
try:
    env = json.loads(p.read_text(encoding="utf-8")).get("env") or {}
except Exception:
    env = {}
px = next((env[k] for k in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy") if env.get(k)), "-")
print(px, env.get("NODE_EXTRA_CA_CERTS") or "-")
EOF
)
if [ -z "${HTTPS_PROXY:-}${https_proxy:-}" ]; then
  px=""
  [ "$cl_proxy" != "-" ] && px="$cl_proxy"
  if [ -z "$px" ] && command -v gsettings >/dev/null \
     && [ "$(gsettings get org.gnome.system.proxy mode 2>/dev/null)" = "'manual'" ]; then
    h="$(gsettings get org.gnome.system.proxy.https host | tr -d "'")"
    p="$(gsettings get org.gnome.system.proxy.https port | awk '{print $NF}')"
    [ -n "$h" ] && [ "$p" != 0 ] && px="http://$h:$p"
  fi
  if [ -n "$px" ]; then
    case "$px" in *://*) ;; *) px="http://$px";; esac
    export HTTPS_PROXY="$px" HTTP_PROXY="$px" https_proxy="$px" http_proxy="$px"
    say "프록시 사용: $px"
  fi
fi
sys_ca=""
for f in /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/certs/ca-bundle.crt /etc/ssl/ca-bundle.pem; do
  [ -f "$f" ] && { sys_ca="$f"; break; }
done
extra_ca="${NODE_EXTRA_CA_CERTS:-}"
[ -z "$extra_ca" ] && [ "$cl_ca" != "-" ] && extra_ca="$cl_ca"
if [ -n "$extra_ca" ] && [ -f "$extra_ca" ] && [ -n "$sys_ca" ]; then
  mkdir -p build
  cat "$sys_ca" "$extra_ca" > build/ca-bundle.pem
  sys_ca="$PWD/build/ca-bundle.pem"
  say "사내 인증서 사용: $extra_ca"
fi
if [ -n "$sys_ca" ]; then
  export PIP_CERT="${PIP_CERT:-$sys_ca}" SSL_CERT_FILE="${SSL_CERT_FILE:-$sys_ca}"
fi
# uv 도 시스템 인증서 저장소를 쓰게(새 uv 는 UV_SYSTEM_CERTS, 옛 uv 는 UV_NATIVE_TLS)
if command -v uv >/dev/null && uv pip install --help 2>/dev/null | grep -q system-certs; then
  export UV_SYSTEM_CERTS=1
else
  export UV_NATIVE_TLS=1
fi
# sudo 는 환경변수를 지우므로 프록시는 명시적으로 넘긴다.
SUDO=(sudo env "http_proxy=${http_proxy:-}" "https_proxy=${https_proxy:-}" \
      "HTTP_PROXY=${HTTP_PROXY:-}" "HTTPS_PROXY=${HTTPS_PROXY:-}")

# ---------- 1. 빌드용 시스템 패키지 (tkinter, venv) ----------
# 실행파일 안에 다 들어가므로, 실행하는 PC 에는 아무것도 설치할 필요가 없다. 빌드하는 PC 에만 필요.
need=()
"$PY" -c 'import tkinter' 2>/dev/null || need+=(tk)
command -v uv >/dev/null || "$PY" -c 'import venv, ensurepip' 2>/dev/null || need+=(venv)

if [ ${#need[@]} -gt 0 ]; then
  say "[1/4] 빌드에 필요한 패키지 설치: ${need[*]} (sudo)"
  pkgs=()
  if command -v apt-get >/dev/null; then
    for n in "${need[@]}"; do case $n in tk) pkgs+=(python3-tk);; venv) pkgs+=(python3-venv);; esac; done
    "${SUDO[@]}" apt-get update -qq && "${SUDO[@]}" apt-get install -y "${pkgs[@]}"
  elif command -v dnf >/dev/null; then
    for n in "${need[@]}"; do case $n in tk) pkgs+=(python3-tkinter);; venv) pkgs+=(python3-pip);; esac; done
    "${SUDO[@]}" dnf install -y "${pkgs[@]}"
  elif command -v pacman >/dev/null; then
    for n in "${need[@]}"; do case $n in tk) pkgs+=(tk);; venv) pkgs+=(python);; esac; done
    "${SUDO[@]}" pacman -S --needed --noconfirm "${pkgs[@]}"
  else
    warn "지원하는 패키지 관리자를 못 찾았습니다. 파이썬 tkinter 와 venv 를 직접 설치하세요."
    exit 1
  fi
else
  say "[1/4] 필요한 패키지가 이미 다 있습니다"
fi

# ---------- 2. 빌드 환경 ----------
say "[2/4] 빌드 환경 준비 (PyInstaller · Pillow · jeepney)"
PKGS=(pyinstaller pillow jeepney)
if command -v uv >/dev/null; then
  uv venv .venv --clear --python "$PY" >/dev/null
  uv pip install --python .venv --quiet "${PKGS[@]}"
else
  rm -rf .venv
  "$PY" -m venv .venv
  ./.venv/bin/pip install --quiet --upgrade pip
  ./.venv/bin/pip install --quiet "${PKGS[@]}"
fi

# ---------- 3. 빌드 ----------
# 표시줄 모듈(sni_tray.py)·Pillow·jeepney 는 import 를 따라 자동으로 들어간다. 아이콘만 따로 넣는다.
say "[3/4] 빌드"
# ROS 등이 잡아 둔 PYTHONPATH 가 있으면 엉뚱한 모듈이 섞여 들어갈 수 있다 → 빌드할 때는 비운다.
unset PYTHONPATH
./.venv/bin/pyinstaller --onefile --noconsole --name "$APP" --clean --noconfirm \
  --add-data "icons/tokenwidget.png:icons" ccusage_widget.py

# ---------- 4. 실행 = 설치 ----------
# 실행파일이 스스로 ~/.local/bin 에 설치·앱 등록을 하고 설치본을 띄운다.
# 이미 떠 있던 옛 버전은 새 버전이 알아서 정리한다(하나만 남음).
say "[4/4] 설치 및 실행"
had_desktop=0; [ -f "$HOME/.local/share/applications/tokenwidget.desktop" ] && had_desktop=1
setsid "./dist/$APP" >/dev/null 2>&1 < /dev/null &
sleep 2

echo
echo "완료: dist/$APP  →  설치본 ~/.local/bin/$APP"
echo "  · 상단 표시줄에 잔여량이 뜨고, 앱 목록에서 'Token Widget' 으로도 찾을 수 있습니다."
echo "  · 다른 PC 에는 dist/$APP 파일 하나만 복사해 실행하면 됩니다(파이썬·패키지 설치 불필요)."

# GNOME 은 실행 중인 세션에 새로 추가된 .desktop 을 바로 반영하지 않을 때가 있다.
# 처음 설치한 경우에만, 독 아이콘이 제대로 나오도록 셸 재시작을 물어본다.
if [ "$had_desktop" = 0 ] && [ "${XDG_CURRENT_DESKTOP:-}" != "" ] && [ -t 0 ]; then
  case "${XDG_CURRENT_DESKTOP}" in
    *GNOME*)
      echo
      read -r -p "독 아이콘 적용을 위해 GNOME Shell 을 다시 시작할까요? (창은 유지됩니다) [y/N] " a
      if [ "${a:-N}" = y ] || [ "${a:-N}" = Y ]; then
        if [ "${XDG_SESSION_TYPE:-}" = x11 ]; then
          busctl --user call org.gnome.Shell /org/gnome/Shell org.gnome.Shell \
            Eval s 'Meta.restart("재시작 중…")' >/dev/null 2>&1 \
            || warn "자동 재시작 실패 — Alt+F2 → r → Enter 로 직접 해주세요."
        else
          warn "Wayland 세션은 셸 재시작이 안 됩니다. 로그아웃 후 다시 로그인하면 적용됩니다."
        fi
      fi;;
  esac
fi
