#!/usr/bin/env python3
"""
Token Widget — Claude Code · Codex 잔여량 위젯  (v3.0.0)

배터리 잔량처럼 "얼마나 남았는지"만 보여준다.

- Claude : ~/.claude/.credentials.json 의 OAuth 토큰으로 /v1/messages 에 최소 요청(≈23토큰)을
           보내고 응답 헤더 anthropic-ratelimit-unified-* 에서 5시간·주간(·월 한도) 사용률을 읽는다.
- Codex  : ~/.codex/auth.json 토큰으로 chatgpt 백엔드 /wham/usage 를 GET (무료) — 5시간·주간(·월 크레딧).
- 모델별 비중은 로컬 세션 로그(JSONL)에서 센다(무료).

리눅스 상단 표시줄 아이콘은 D-Bus 로 직접 띄운다(sni_tray.py) — gi·AppIndicator 같은 시스템 패키지가 필요 없다.
실행파일(PyInstaller)은 처음 실행될 때 ~/.local 에 스스로 설치되고 앱 목록·로그인 자동 실행에 등록된다.

창: 드래그 = 이동 · ↻ / 가운데클릭 / F5 = 새로고침 · ··· / 우클릭 = 설정 · Esc = 닫기(표시줄에는 남음)
"""

import base64
import encodings.idna  # noqa: F401  두 스레드가 처음 동시에 불러오면 'unknown encoding: idna' 가 난다
import hashlib
import importlib.util
import io
import json
import os
import queue
import re
import shutil
import signal
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ---- Windows: python.exe(콘솔) → pythonw.exe 재실행 ----
if sys.platform == "win32":
    try:
        exe = sys.executable or ""
        if exe.lower().endswith("python.exe") and "__file__" in globals():
            pyw = exe[:-len("python.exe")] + "pythonw.exe"
            if os.path.exists(pyw):
                subprocess.Popen([pyw, os.path.abspath(__file__)] + sys.argv[1:],
                                 creationflags=0x00000008 | 0x08000000, close_fds=True)
                sys.exit(0)
    except Exception:
        pass

# ---- Windows: DPI 선명도 ----
if sys.platform == "win32":
    try:
        import ctypes
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass

import tkinter as tk

VERSION = "3.0.0"
APP_NAME = "Token Widget"
FROZEN = getattr(sys, "frozen", False)
IS_LINUX = sys.platform.startswith("linux")
IS_WIN = sys.platform == "win32"

# ---------------- 설정 ----------------
# 갱신 주기(분). 설정에서 바꾸면 저장된다. Claude 확인은 1회당 ~23토큰을 쓰므로 너무 짧게 두지 말 것.
# 어차피 "질문하면 바로 반영"이 켜져 있으면 값은 바로바로 최신이 된다.
DEFAULT_INTERVAL_MIN = 10
INTERVAL_STEPS = (1, 2, 3, 5, 10, 15, 20, 30, 45, 60, 90, 120, 180, 240, 360, 720, 1440)
SIZE_STEPS = (0.8, 0.9, 1.0, 1.1, 1.2, 1.3, 1.4, 1.5, 1.6, 1.8, 2.0)
ACTIVITY_POLL_SEC = 4            # 로그 변화 감시 간격(파일 stat 만, 토큰 안 씀)
ACTIVITY_QUIET_SEC = 8           # 마지막 변화 후 이만큼 조용하면 = 답변 끝 → 그때 갱신
ACTIVITY_COOLDOWN_SEC = 45       # 질문이 잦아도 이 간격보다 자주는 안 부른다
API_MODEL = "claude-haiku-4-5"   # 헤더만 필요하므로 가장 싼 모델 + max_tokens=1
CODEX_USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"
CONFIG_PATH = Path.home() / ".ccusage_widget.json"
PID_PATH = Path.home() / ".ccusage_widget.pid"
# 상단 표시줄에 어느 창을 보여줄지. 기본은 5시간(가장 짧은 창).
PANEL_CHOICES = (("5h", "5시간"), ("7d", "주간"), ("low", "낮은 쪽"))

VERIFY_SSL = True                # 사내 프록시(SSL 검사)에서 인증서를 끝내 못 맞출 때만 False 로

# 색: 애플 다크 모드 팔레트
BG, CARD, CARD_HI = "#1c1c1e", "#2c2c2e", "#353538"
FILL, FILL_HI, SEP = "#3a3a3c", "#636366", "#3d3d41"
TEXT, TEXT2, TEXT3 = "#f5f5f7", "#98989f", "#6e6e73"
GREEN, ORANGE, RED, BLUE = "#30d158", "#ff9f0a", "#ff453a", "#0a84ff"
CLAUDE_COL, CODEX_COL = "#d97757", "#10a37f"

# 설치 위치 (리눅스 실행파일)
INSTALL_BIN = Path.home() / ".local" / "bin" / "TokenWidget"
DESKTOP_FILE = Path.home() / ".local" / "share" / "applications" / "tokenwidget.desktop"
ICON_FILE = Path.home() / ".local" / "share" / "icons" / "hicolor" / "256x256" / "apps" / "tokenwidget.png"
AUTOSTART_FILE = Path.home() / ".config" / "autostart" / "tokenwidget.desktop"

# 소스로 실행할 때 부족한 패키지를 받아 두는 곳(시스템 파이썬은 건드리지 않는다)
PYLIB = ((Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "TokenWidget") if IS_WIN
         else Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "tokenwidget"
         ) / f"py{sys.version_info[0]}{sys.version_info[1]}"


def claude_home():
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or (Path.home() / ".claude"))


def codex_home():
    return Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex"))


def sys_env():
    """시스템 프로그램(gsettings·fc-match·설치본 재실행)을 부를 때 쓰는 환경.
    PyInstaller 실행파일은 LD_LIBRARY_PATH 를 자기 임시폴더로 바꿔 두므로 원래대로 되돌린다."""
    env = dict(os.environ)
    if FROZEN:
        for k in list(env):
            if k.startswith("_PYI_") or k == "_MEIPASS2":
                env.pop(k)
        env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
        if IS_LINUX:
            orig = env.pop("LD_LIBRARY_PATH_ORIG", None)
            if orig is not None:
                env["LD_LIBRARY_PATH"] = orig
            else:
                env.pop("LD_LIBRARY_PATH", None)
    return env


_GSETTINGS = {}


def gsettings(schema, key):
    """GNOME 설정값(문자열). 없거나 GNOME 이 아니면 ''."""
    if (schema, key) not in _GSETTINGS:
        val = ""
        if IS_LINUX and shutil.which("gsettings"):
            try:
                val = subprocess.run(["gsettings", "get", schema, key], capture_output=True,
                                     text=True, timeout=3, env=sys_env()).stdout.strip()
            except (OSError, subprocess.SubprocessError):
                pass
        _GSETTINGS[(schema, key)] = val
    return _GSETTINGS[(schema, key)]


def desktop_scale():
    """GNOME 화면 배율(200% = 2). 자동(0)이거나 알 수 없으면 1."""
    try:
        return max(1, int(gsettings("org.gnome.desktop.interface", "scaling-factor").split()[-1]))
    except (ValueError, IndexError):
        return 1


def text_scale():
    """GNOME '큰 글씨' 배율(접근성). 알 수 없으면 1."""
    try:
        return min(2.0, max(0.5, float(gsettings("org.gnome.desktop.interface", "text-scaling-factor"))))
    except ValueError:
        return 1.0


def load_config():
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}


def save_config(cfg):
    try:
        tmp = CONFIG_PATH.with_name(CONFIG_PATH.name + ".tmp")
        tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, CONFIG_PATH)             # 쓰다가 꺼져도 설정 파일이 깨지지 않게
    except OSError:
        pass


# ---------------- 네트워크: 프록시 · 인증서 ----------------
# 독/자동 실행으로 뜨면 셸(.bashrc)의 HTTPS_PROXY 가 없다. 그래서 여러 곳에서 프록시를 찾아
# 차례로 시도하고(마지막엔 직접 연결), 성공한 경로를 기억해 다음엔 그것부터 쓴다.
_CA_FILES = ("/etc/ssl/certs/ca-certificates.crt", "/etc/pki/tls/certs/ca-bundle.crt",
             "/etc/ssl/ca-bundle.pem", "/etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem",
             "/etc/ssl/cert.pem")


class UserError(Exception):
    """화면에 그대로 보여줄 오류: 무엇이 문제인지(title) + 어떻게 하면 되는지(hint)."""

    def __init__(self, title, hint=""):
        super().__init__(title)
        self.title, self.hint = title, hint


def claude_env():
    """~/.claude/settings.json 의 "env". 사내망 사용자는 프록시·인증서를 여기에 적어 두는 경우가 많다."""
    try:
        env = json.loads((claude_home() / "settings.json").read_text(encoding="utf-8")).get("env") or {}
        return {str(k): str(v) for k, v in env.items() if v}
    except Exception:
        return {}


def net_env():
    """프록시·인증서 관련 값. 실제 환경변수가 우선, 없으면 Claude Code 설정의 env."""
    env = claude_env()
    env.update({k: v for k, v in os.environ.items() if v})
    return env


def gnome_proxy():
    """GNOME 설정 → 네트워크 → 프록시(수동)."""
    if gsettings("org.gnome.system.proxy", "mode").strip("'") != "manual":
        return None
    for sch in ("https", "http"):
        host = gsettings(f"org.gnome.system.proxy.{sch}", "host").strip("'")
        port = gsettings(f"org.gnome.system.proxy.{sch}", "port").split()[-1:] or ["0"]
        if host and port[0] != "0":
            return f"http://{host}:{port[0]}"
    return None


def proxy_candidates(manual=None):
    """시도할 경로 목록. 문자열 = 그 프록시 경유, None = 직접 연결(항상 마지막)."""
    env = net_env()
    raw = [manual] + [env.get(k) for k in ("HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy",
                                           "HTTP_PROXY", "http_proxy")] + [gnome_proxy()]
    out = []
    for p in raw:
        p = (p or "").strip()
        if not p:
            continue
        if "://" not in p:
            p = "http://" + p
        if p.split("://", 1)[0].lower() in ("http", "https") and p not in out:   # socks 는 urllib 불가
            out.append(p)
    return out + [None]


def _make_ssl_context():
    if not VERIFY_SSL:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx
    try:
        import truststore          # 윈도우/맥: OS 인증서 저장소(사내 CA 포함)
        return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    except Exception:
        pass
    # 실행파일에 들어간 OpenSSL 은 빌드한 배포판 경로만 알 수 있다 → 배포판별 인증서 묶음을 직접 읽는다.
    # 사내 루트 인증서는 시스템 묶음 또는 NODE_EXTRA_CA_CERTS(Claude Code 가 쓰는 값)에 있다.
    ctx = ssl.create_default_context()
    env = net_env()
    for f in (*_CA_FILES, env.get("SSL_CERT_FILE"), env.get("REQUESTS_CA_BUNDLE"),
              env.get("NODE_EXTRA_CA_CERTS")):
        if f and os.path.isfile(f):
            try:
                ctx.load_verify_locations(cafile=f)
            except (OSError, ssl.SSLError):
                pass
    return ctx


_SSL_CTX = _make_ssl_context()
_net = {"route": "unset", "proxy": None}      # 마지막으로 성공한 경로 / 설정의 수동 프록시


def http_open(req, timeout=10):
    """프록시·직접 연결을 차례로 시도해 처음 되는 경로로 연다."""
    cands = proxy_candidates(_net["proxy"])
    if _net["route"] in cands:
        cands.remove(_net["route"])
        cands.insert(0, _net["route"])
    last = None
    for p in cands:
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": p, "https": p} if p else {}),
            urllib.request.HTTPSHandler(context=_SSL_CTX))
        # 프록시 처리기가 Request 를 고쳐 쓰므로(set_proxy) 경로마다 새로 만든다.
        attempt = urllib.request.Request(req.full_url, data=req.data, headers=req.headers,
                                         method=req.get_method())
        try:
            resp = opener.open(attempt, timeout=timeout)
        except urllib.error.HTTPError:
            _net["route"] = p
            raise                              # 서버까지는 닿았다 — 다른 경로를 시도할 이유 없음
        except (urllib.error.URLError, OSError) as e:
            last = e
            continue
        _net["route"] = p
        return resp
    reason = getattr(last, "reason", last)
    if isinstance(reason, ssl.SSLCertVerificationError) or "CERTIFICATE_VERIFY" in str(reason):
        raise UserError("인증서를 확인할 수 없습니다",
                        "사내망(SSL 검사)이면 회사 루트 인증서를 시스템에 설치하세요")
    if isinstance(reason, socket.timeout) or "timed out" in str(reason):
        raise UserError("서버 응답이 없습니다", "네트워크 연결을 확인하세요")
    raise UserError("네트워크에 연결할 수 없습니다", "인터넷·프록시 설정을 확인하세요")


# ---------------- 단일 인스턴스 ----------------
def _proc_cmdline(pid):
    """해당 pid 의 실행 명령줄. 못 읽으면 빈 문자열(= 남의 프로세스로 취급)."""
    try:
        if IS_LINUX:
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                return f.read().decode("utf-8", "replace").replace("\0", " ")
        if sys.platform == "darwin":
            return subprocess.run(["ps", "-p", str(pid), "-o", "command="],
                                  capture_output=True, text=True, timeout=3).stdout
    except Exception:                       # noqa: BLE001
        pass
    return ""


def _same_program(pid):
    """떠 있는 것이 지금과 똑같은 실행파일인가. 실행파일이 교체(업데이트)됐으면 '(deleted)' 가 붙어 다르다."""
    if not (FROZEN and IS_LINUX):
        return False                        # 소스 실행은 늘 새 코드로 교체(개발 중)
    try:
        return os.readlink(f"/proc/{pid}/exe") == os.path.realpath(sys.executable)
    except OSError:
        return False


_early_show = []                            # 창이 만들어지기 전에 받은 '창 띄워줘' 신호


def take_single_instance(background):
    """이미 떠 있는 위젯이 있으면:
    - 같은 실행파일 → 그쪽 창을 띄우라고 알리고 나는 끝낸다(앱 아이콘을 다시 눌렀을 때).
    - 다른/새 실행파일 → 옛것을 끄고 내가 남는다(업데이트했을 때).
    pid 재사용으로 엉뚱한 프로세스를 건드리지 않도록, 명령줄에 우리 이름이 있을 때만 손댄다.
    """
    me = os.getpid()
    try:
        old = int(PID_PATH.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        old = 0
    if old and old != me:
        cmd = _proc_cmdline(old)
        if "TokenWidget" in cmd or "ccusage_widget" in cmd:
            if _same_program(old) and hasattr(signal, "SIGUSR1"):
                try:
                    if not background:
                        os.kill(old, signal.SIGUSR1)
                    sys.exit(0)
                except OSError:
                    pass                    # 그새 꺼졌다 → 내가 뜬다
            try:
                os.kill(old, signal.SIGTERM)
                for _ in range(40):         # 최대 4초까지 종료를 기다린다
                    time.sleep(0.1)
                    os.kill(old, 0)         # 아직 살아 있으면 예외 없이 통과
                os.kill(old, signal.SIGKILL)
            except OSError:
                pass                        # 이미 사라짐 = 정상
    if hasattr(signal, "SIGUSR1"):
        signal.signal(signal.SIGUSR1, lambda *_: _early_show.append(1))
    try:
        PID_PATH.write_text(str(me), encoding="utf-8")
    except OSError:
        pass


def release_single_instance():
    try:
        if PID_PATH.read_text(encoding="utf-8").strip() == str(os.getpid()):
            PID_PATH.unlink()
    except OSError:
        pass


# ---------------- 설치 · 자동 실행 ----------------
def app_icon_path():
    """앱 아이콘(창·독 표시용). 소스 실행/PyInstaller 실행/설치본 어디서든 찾도록 순서대로 시도."""
    here = getattr(sys, "_MEIPASS", None) or os.path.dirname(os.path.abspath(__file__))
    for p in (os.path.join(here, "icons", "tokenwidget.png"),
              os.path.join(here, "tokenwidget.png"),
              str(ICON_FILE), "/usr/share/icons/hicolor/256x256/apps/tokenwidget.png"):
        if os.path.exists(p):
            return p
    return None


def launch_cmd():
    """이 앱을 다시 띄우는 명령(자동 실행 등록용)."""
    if FROZEN:
        return [str(INSTALL_BIN) if IS_LINUX and INSTALL_BIN.exists() else sys.executable]
    return [sys.executable, os.path.abspath(__file__)]


def _desktop_exec(args):
    def q(a):
        a = a.replace("%", "%%")
        if any(c in a for c in ' \t\n"\'\\$`'):
            a = '"' + a.replace("\\", "\\\\").replace('"', '\\"').replace("$", "\\$").replace("`", "\\`") + '"'
        return a
    return " ".join(q(a) for a in args)


def desktop_entry(args, extra=""):
    # StartupWMClass 는 Tk 가 만드는 WM_CLASS 클래스명("Tokenwidget")과 같아야 독에서 이 앱으로 묶인다.
    return ("[Desktop Entry]\nType=Application\nName=Token Widget\n"
            "Comment=Claude Code · Codex 잔여량\n"
            f"Exec={_desktop_exec(args)}\nIcon=tokenwidget\nCategories=Utility;\n"
            "Terminal=false\nStartupNotify=true\nStartupWMClass=Tokenwidget\n" + extra)


def _write_if_changed(path, data, mode=0o644):
    """내용이 다를 때만 원자적으로 쓴다(실행 중인 파일도 교체 가능). 바꿨으면 True."""
    path = Path(path)
    try:
        if path.read_bytes() == data:
            return False
    except OSError:
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name("." + path.name + ".tmp")
    tmp.write_bytes(data)
    os.chmod(tmp, mode)
    os.replace(tmp, path)
    return True


def autostart_supported():
    return IS_LINUX or IS_WIN


def set_autostart(on):
    """로그인 시 자동 실행 켜기/끄기. 자동 실행은 창 없이 상단 표시줄로만 뜬다(--background)."""
    cmd = launch_cmd() + ["--background"]
    try:
        if IS_LINUX:
            if on:
                _write_if_changed(AUTOSTART_FILE, desktop_entry(
                    cmd, "X-GNOME-Autostart-enabled=true\nX-GNOME-Autostart-Delay=3\n").encode())
            elif AUTOSTART_FILE.exists():
                AUTOSTART_FILE.unlink()
        elif IS_WIN:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                r"Software\Microsoft\Windows\CurrentVersion\Run",
                                0, winreg.KEY_SET_VALUE) as k:
                if on:
                    winreg.SetValueEx(k, "TokenWidget", 0, winreg.REG_SZ, subprocess.list2cmdline(cmd))
                else:
                    try:
                        winreg.DeleteValue(k, "TokenWidget")
                    except FileNotFoundError:
                        pass
    except OSError as e:
        print(f"[알림] 자동 실행 설정 실패: {e}", file=sys.stderr)


def self_install(args):
    """리눅스 실행파일: ~/.local 에 스스로 설치하고 앱 목록에 등록한다(sudo 불필요).
    다른 곳(다운로드 폴더, dist/ 등)에서 실행됐으면 설치본을 띄우고 True — 호출자는 그대로 끝내면 된다."""
    if not (FROZEN and IS_LINUX):
        return False
    exe = Path(os.path.realpath(sys.executable))
    try:
        if exe != INSTALL_BIN.resolve():
            if _write_if_changed(INSTALL_BIN, exe.read_bytes(), 0o755):
                print(f"설치: {INSTALL_BIN}")
        icon = app_icon_path()
        if icon and Path(icon) != ICON_FILE:
            _write_if_changed(ICON_FILE, Path(icon).read_bytes())
        if _write_if_changed(DESKTOP_FILE, desktop_entry([str(INSTALL_BIN)]).encode()):
            for tool in (["update-desktop-database", str(DESKTOP_FILE.parent)],
                         ["gtk-update-icon-cache", "-q", str(ICON_FILE.parents[3])]):
                if shutil.which(tool[0]):
                    subprocess.run(tool, capture_output=True, timeout=10, env=sys_env())
            print("앱 목록에 'Token Widget' 을 등록했습니다")
    except (OSError, subprocess.SubprocessError) as e:
        print(f"[알림] 설치하지 못해 이 자리에서 실행합니다: {e}", file=sys.stderr)
        return False
    if exe == INSTALL_BIN.resolve():
        return False
    subprocess.Popen([str(INSTALL_BIN)] + args, env=sys_env(), start_new_session=True,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return True


def uninstall():
    try:
        old = int(PID_PATH.read_text(encoding="utf-8").strip())
        if "TokenWidget" in _proc_cmdline(old) or "ccusage_widget" in _proc_cmdline(old):
            os.kill(old, signal.SIGTERM)
    except (OSError, ValueError):
        pass
    set_autostart(False)
    for f in (INSTALL_BIN, DESKTOP_FILE, ICON_FILE, PID_PATH):
        try:
            f.unlink()
            print(f"삭제: {f}")
        except OSError:
            pass
    print(f"제거했습니다. 설정 파일({CONFIG_PATH})은 남겨 둡니다.")


# ---------------- 소스 실행용 의존성 ----------------
def _add_pylib():
    if not FROZEN and PYLIB.is_dir():
        sys.path.insert(0, str(PYLIB))


def _missing_packages():
    need = []
    try:
        from importlib.metadata import version
        pil_ok = tuple(int(x) for x in version("Pillow").split(".")[:2]) >= (8, 2)
    except Exception:
        pil_ok = False                      # 없거나, Ubuntu 20.04 기본(7.0)처럼 너무 오래됨
    if not pil_ok:
        need.append("pillow")
    tray_mod = "jeepney" if IS_LINUX else "pystray"
    if importlib.util.find_spec(tray_mod) is None:
        need.append(tray_mod)
    return need


def ensure_deps():
    """소스로 실행할 때 필요한 패키지(Pillow·jeepney/pystray)가 없으면 사용자 캐시 폴더에 알아서 받는다.
    시스템 파이썬은 건드리지 않고(sudo 불필요), 프록시·사내 인증서도 위와 같은 방식으로 찾아 쓴다.
    실행파일에는 이미 다 들어 있어서 아무것도 하지 않는다."""
    if FROZEN:
        return
    need = _missing_packages()
    if not need:
        return
    splash = None
    try:
        splash = tk.Tk()
        splash.title(APP_NAME)
        tk.Label(splash, text="처음 실행 준비 중…\n필요한 구성요소를 받고 있습니다",
                 padx=28, pady=20, justify="center").pack()
        splash.update()
    except tk.TclError:
        pass
    PYLIB.mkdir(parents=True, exist_ok=True)
    ca = next((f for f in _CA_FILES if os.path.isfile(f)), None)
    base = ["install", "--upgrade", "--quiet", "--disable-pip-version-check",
            "--target", str(PYLIB), *need]
    tries = []
    for px in [p for p in proxy_candidates(load_config().get("proxy")) if p][:1] + [None]:
        tries.append([sys.executable, "-m", "pip", *base]
                     + (["--cert", ca] if ca else []) + (["--proxy", px] if px else []))
    if shutil.which("uv"):
        tries.append(["uv", "pip", "install", "--python", sys.executable, "--target", str(PYLIB), *need])
    env = dict(os.environ, UV_NATIVE_TLS="1", UV_SYSTEM_CERTS="1")   # uv 도 시스템 인증서 저장소를 쓰게
    for cmd in tries:
        try:
            if subprocess.run(cmd, env=env, capture_output=True, timeout=600).returncode == 0:
                break
        except (OSError, subprocess.SubprocessError):
            continue
    if str(PYLIB) not in sys.path:
        sys.path.insert(0, str(PYLIB))
    importlib.invalidate_caches()
    if splash is not None:
        splash.destroy()
    if "pillow" in _missing_packages():
        msg = ("Pillow 를 설치하지 못했습니다.\n\n"
               "./build.sh 로 실행파일을 만들어 쓰거나,\n"
               "pip install pillow jeepney 후 다시 실행하세요.")
        print(msg, file=sys.stderr)
        try:
            from tkinter import messagebox
            r = tk.Tk()
            r.withdraw()
            messagebox.showerror(APP_NAME, msg)
            r.destroy()
        except tk.TclError:
            pass
        sys.exit(1)


# ---------------- 공통 계산 ----------------
def level_color(remain):
    """잔량(0~1) → 색. 20% 미만 빨강 / 50% 미만 주황 / 그 외 초록."""
    if remain is None:
        return TEXT3
    if remain < 0.2:
        return RED
    if remain < 0.5:
        return ORANGE
    return GREEN


def _mix(hex_fg, hex_bg, a):
    """hex_fg 를 hex_bg 쪽으로 a 만큼 섞는다. 채움색을 흐리게 만들어 글자를 살리는 용도."""
    f = [int(hex_fg[i:i + 2], 16) for i in (1, 3, 5)]
    b = [int(hex_bg[i:i + 2], 16) for i in (1, 3, 5)]
    return "#%02x%02x%02x" % tuple(int(f[i] * a + b[i] * (1 - a)) for i in range(3))


def fill_color(remain, bg):
    """배터리 채움색: 같은 계열이지만 흐리게 — 그 위의 숫자가 또렷하게 보이도록."""
    return _mix(level_color(remain), bg, 0.42)


_WEEKDAY = "월화수목금토일"


def fmt_reset(reset_dt):
    """리셋 시각 → ('4시간 29분 후 충전', '21:40'). 날짜가 다르면 '내일 08:00' / '9/29 (월) 08:00'."""
    if not reset_dt:
        return "", ""
    now = datetime.now()
    sec = (reset_dt - now).total_seconds()
    if sec <= 0:
        rel = "곧 충전"
    else:
        d, rem = divmod(int(sec), 86400)
        h, m = rem // 3600, rem % 3600 // 60
        rel = (f"{d}일 {h}시간 후 충전" if d else
               f"{h}시간 {m}분 후 충전" if h else f"{max(m, 1)}분 후 충전")
    days = (reset_dt.date() - now.date()).days
    hm = reset_dt.strftime("%H:%M")
    ab = (hm if days <= 0 else f"내일 {hm}" if days == 1
          else f"{reset_dt.month}/{reset_dt.day} ({_WEEKDAY[reset_dt.weekday()]}) {hm}")
    return rel, ab


def fmt_interval(m):
    if m < 60:
        return f"{m}분"
    return f"{m // 60}시간" + (f" {m % 60}분" if m % 60 else "")


def _num(v):
    try:
        return float(str(v).strip())
    except Exception:
        return None


def _epoch(ts):
    try:
        return datetime.fromtimestamp(int(ts))
    except Exception:
        return None


def _month_start(now, offset):
    """이번 달(0) 또는 다음 달(+1) 1일 0시."""
    if offset:
        return (now.replace(year=now.year + 1, month=1, day=1) if now.month == 12
                else now.replace(month=now.month + 1, day=1)).replace(
                    hour=0, minute=0, second=0, microsecond=0)
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


# ---------------- Claude: 사용률 헤더 ----------------
def read_claude_token():
    p = claude_home() / ".credentials.json"
    try:
        o = json.loads(p.read_text(encoding="utf-8")).get("claudeAiOauth") or {}
    except FileNotFoundError:
        raise UserError("로그인이 필요합니다", "터미널에서 claude 를 실행해 로그인하세요")
    except ValueError:
        raise UserError("로그인 정보를 읽을 수 없습니다", str(p))
    tok = o.get("accessToken")
    if not tok:
        raise UserError("로그인이 필요합니다", "터미널에서 claude 를 실행해 로그인하세요")
    exp = o.get("expiresAt")
    if exp and time.time() * 1000 > float(exp):
        raise UserError("로그인이 만료됐습니다", "터미널에서 claude 를 한 번 실행하면 갱신됩니다")
    return tok


def _parse_reset(v):
    s = str(v).strip()
    try:
        n = float(s)
        if n > 1e12:
            n /= 1000.0
        if n > 1e9:                     # epoch
            return datetime.fromtimestamp(n)
    except Exception:
        pass
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone().replace(tzinfo=None)
    except Exception:
        return None


# 헤더 이름 → 어떤 한도 창인지. 계정마다 오는 창이 달라서 오는 것만 그대로 쓴다.
# (개인 요금제는 5시간·주간, 회사/지출한도 계정은 overage=월 지출한도가 온다)
_UNIFIED = re.compile(r"^anthropic-ratelimit-unified-([a-z0-9_]+)-(utilization|reset)$")
_WIN_ALIAS = {"5h": "5h", "five_hour": "5h", "7d": "7d", "seven_day": "7d", "overage": "overage"}


def parse_claude_headers(headers):
    """anthropic-ratelimit-unified-* 헤더 → {창: (사용률0~1, 리셋)}"""
    used, reset = {}, {}
    for k, v in headers.items():
        m = _UNIFIED.match(k.lower())
        win = m and _WIN_ALIAS.get(m.group(1))
        if not win:
            continue
        if m.group(2) == "utilization":
            n = _num(v)
            if n is not None:
                used[win] = n
        else:
            r = _parse_reset(v)
            if r is not None:
                reset[win] = r
    if used and max(used.values()) <= 1.0:      # 0~1 분수로 오는 경우
        used = {k: v * 100.0 for k, v in used.items()}
    return {w: (min(used[w] / 100.0, 1.0), reset.get(w)) for w in used}


def fetch_claude():
    """[창 dict] — 짧은 창부터."""
    tok = read_claude_token()
    body = json.dumps({
        "model": API_MODEL, "max_tokens": 1,
        "system": "You are Claude Code, Anthropic's official CLI for Claude.",
        "messages": [{"role": "user", "content": "."}],
    }).encode("utf-8")
    req = urllib.request.Request("https://api.anthropic.com/v1/messages", data=body, method="POST")
    req.add_header("content-type", "application/json")
    req.add_header("authorization", f"Bearer {tok}")
    req.add_header("anthropic-version", "2023-06-01")
    req.add_header("anthropic-beta", "oauth-2025-04-20")
    status = 200
    try:
        resp = http_open(req, timeout=10)
        headers = resp.headers
        resp.read()
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            raise UserError("로그인이 만료됐습니다", "터미널에서 claude 를 한 번 실행하면 갱신됩니다")
        status, headers = e.code, e.headers      # 429 등에도 헤더는 실려 온다
    parsed = parse_claude_headers(headers)
    if not parsed:
        if status >= 500:
            raise UserError(f"서버 응답 오류 ({status})", "잠시 후 다시 시도합니다")
        raise UserError("사용률 정보를 받지 못했습니다", "probe_claude_headers.py 로 응답을 확인하세요")
    now = datetime.now()
    out = []
    for win, key, label, span in (("5h", "5h", "5시간", timedelta(hours=5)),
                                  ("7d", "7d", "주간", timedelta(days=7)),
                                  ("overage", "month", "월 한도", None)):
        if win not in parsed:
            continue
        used, reset = parsed[win]
        if span is None:                 # 월 지출한도: 리셋은 다음 달 1일, 창은 이번 달
            reset, since = _month_start(now, 1), _month_start(now, 0)
        else:                            # 창 시작 = 리셋 시각 - 창 길이
            since = (reset - span) if reset else (now - span)
        out.append({"key": key, "label": label, "remain": 1.0 - used, "reset": reset, "since": since})
    return out


# ---------------- Codex: wham/usage ----------------
def read_codex_auth():
    p = codex_home() / "auth.json"
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise UserError("로그인이 필요합니다", "터미널에서 codex login 을 실행하세요")
    except ValueError:
        raise UserError("로그인 정보를 읽을 수 없습니다", str(p))
    tokens = data.get("tokens") or {}
    tok = tokens.get("access_token")
    if not tok:
        if data.get("OPENAI_API_KEY"):
            raise UserError("API 키 로그인은 사용량을 볼 수 없습니다",
                            "codex login 으로 ChatGPT 계정에 로그인하세요")
        raise UserError("로그인이 필요합니다", "터미널에서 codex login 을 실행하세요")
    return tok, tokens.get("account_id") or data.get("account_id")


def _window_kind(sec):
    """창 길이(초) → (key, 이름). ≥20일 월간 / ≥2일 주간 / 그 외 N시간."""
    if not sec:
        return "other", "한도"
    days = sec / 86400.0
    if days >= 20:
        return "month", "월간"
    if days >= 2:
        return "7d", "주간"
    h = max(1, int(round(sec / 3600)))
    return ("5h" if h == 5 else f"{h}h"), f"{h}시간"


def _codex_window(w, now):
    pct = _num(w.get("used_percent"))
    if pct is None:
        return None
    reset = _epoch(w.get("reset_at"))
    if reset is None and w.get("reset_after_seconds") is not None:
        reset = now + timedelta(seconds=_num(w.get("reset_after_seconds")) or 0)
    sec = _num(w.get("limit_window_seconds")) or 0
    key, label = _window_kind(sec)
    if sec:
        span = timedelta(seconds=sec)
        since = (reset - span) if reset else (now - span)
    else:
        since = _month_start(now, 0)
    return {"key": key, "label": label, "remain": 1.0 - min(pct / 100.0, 1.0),
            "reset": reset, "since": since, "order": sec or 30 * 86400}


def fetch_codex():
    """[창 dict] — 짧은 창부터. rate limit 의 primary(5시간)·secondary(주간) 창과
    크레딧 한도(spend_control, 회사 계정)를 있는 대로 모두."""
    tok, acct = read_codex_auth()
    req = urllib.request.Request(CODEX_USAGE_URL, method="GET")
    req.add_header("authorization", f"Bearer {tok}")
    req.add_header("accept", "application/json")
    if acct:
        req.add_header("chatgpt-account-id", str(acct))
    try:
        data = json.loads(http_open(req, timeout=10).read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            raise UserError("로그인이 만료됐습니다", "터미널에서 codex login 을 다시 실행하세요")
        raise UserError(f"서버 응답 오류 ({e.code})", "잠시 후 다시 시도합니다")
    except ValueError:
        raise UserError("응답을 해석할 수 없습니다", "잠시 후 다시 시도합니다")
    if not isinstance(data, dict):
        raise UserError("응답을 해석할 수 없습니다", "잠시 후 다시 시도합니다")

    now = datetime.now()
    out = []
    rl = data.get("rate_limit") or {}
    for name in ("primary_window", "secondary_window"):
        w = rl.get(name)
        if isinstance(w, dict):
            row = _codex_window(w, now)
            if row:
                out.append(row)
    sc = (data.get("spend_control") or {}).get("individual_limit") or {}
    limit = _num(sc.get("limit")) or 0
    if limit > 0:                                    # 크레딧(월 지출한도) 계정 = 회사 계정
        pct = _num(sc.get("used_percent"))
        frac = pct / 100.0 if pct is not None else (_num(sc.get("used")) or 0) / limit
        out.append({"key": "month", "label": "월 크레딧", "remain": 1.0 - min(frac, 1.0),
                    "reset": _epoch(sc.get("reset_at")), "since": _month_start(now, 0),
                    "order": 31 * 86400})
    if not out:
        raise UserError("사용률 정보를 받지 못했습니다", "잠시 후 다시 시도합니다")
    return sorted(out, key=lambda r: r["order"])


# ---------------- 모델별 비중 (로컬 로그, 토큰 안 씀) ----------------
# API 는 창별 총 사용률만 준다. "그 안에서 어느 모델을 얼마나 썼는지"는 로컬 세션 로그로만
# 알 수 있다. 모델마다 한도를 깎는 무게가 다르므로(opus > sonnet > haiku) 단순 토큰 수가
# 아니라 아래 가중치를 곱해 비중을 낸다. 값은 상대비만 의미 있다.
MODEL_WEIGHT = {           # (입력, 출력) 1M 토큰 기준 상대 가중치
    "fable": (10.0, 50.0), "opus": (5.0, 25.0), "sonnet": (3.0, 15.0), "haiku": (1.0, 5.0),
    "gpt-5": (5.0, 30.0), "codex": (5.0, 30.0), "o4": (1.10, 4.40), "o3": (1.10, 4.40),
}
WEIGHT_DEFAULT = (3.0, 15.0)


def _weight(model):
    s = (model or "").lower()
    for k, w in MODEL_WEIGHT.items():
        if k in s:
            return w
    return WEIGHT_DEFAULT


_DATE_PART = re.compile(r"^\d{6,8}$")


def short_model(m):
    """표시용 모델명. 변종(sol·terra·codex·max …)과 버전은 살리고 날짜와 claude- 접두어만 뗀다.
    예: claude-haiku-4-5-20251001 → 'haiku 4.5' / gpt-5.6-sol → 'gpt 5.6 sol'"""
    s = (m or "").strip().lower()
    if not s or s.startswith("<"):
        return "기타"
    parts = [p for p in s.split("-") if p and not _DATE_PART.match(p)]
    if parts and parts[0] in ("claude", "anthropic", "openai"):
        parts = parts[1:] or parts
    out = []
    for p in parts:
        if out and p.isdigit() and out[-1].replace(".", "").isdigit():
            out[-1] += "." + p                  # haiku-4-5 → haiku 4.5
        else:
            out.append(p)
    return " ".join(out)[:18]


def _iter_jsonl(path):
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        yield json.loads(line)
                    except ValueError:
                        continue
    except OSError:
        return


def _recent_files(roots, suffix, cutoff_ts):
    """cutoff 이후에 수정된 로그 파일만 고른다(오래된 세션은 열어보지도 않는다)."""
    out = []
    for root in roots:
        for dirpath, _dirs, files in os.walk(root):
            for f in files:
                if not f.endswith(suffix):
                    continue
                p = os.path.join(dirpath, f)
                try:
                    if os.stat(p).st_mtime >= cutoff_ts:
                        out.append(p)
                except OSError:
                    continue
    return out


def _to_local(ts):
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone().replace(tzinfo=None)
    except ValueError:
        return None


def claude_records(since):
    """Claude 세션 로그에서 since 이후의 (시각, 모델, 가중치) 목록."""
    roots = [claude_home() / "projects", Path.home() / ".config" / "claude" / "projects"]
    recs, seen = [], set()
    for p in _recent_files(roots, ".jsonl", since.timestamp()):
        for obj in _iter_jsonl(p):
            if obj.get("type") != "assistant":
                continue
            msg = obj.get("message") or {}
            usage = msg.get("usage") or {}
            if not usage:
                continue
            mid = msg.get("id")
            if mid:
                if mid in seen:
                    continue
                seen.add(mid)
            ts = _to_local(obj.get("timestamp", ""))
            if ts is None or ts < since:
                continue
            pi, po = _weight(msg.get("model"))
            w = (int(usage.get("input_tokens", 0) or 0) * pi
                 + int(usage.get("output_tokens", 0) or 0) * po
                 + int(usage.get("cache_creation_input_tokens", 0) or 0) * pi * 1.25
                 + int(usage.get("cache_read_input_tokens", 0) or 0) * pi * 0.1)
            recs.append((ts, short_model(msg.get("model")), w))
    return recs


def codex_records(since):
    """Codex 세션 로그에서 since 이후의 (시각, 모델, 가중치) 목록.
    token_count 는 누적값이라 직전 값과의 차이를 쓴다."""
    home = codex_home()
    recs = []
    for p in _recent_files([home / "sessions", home / "archived_sessions"], ".jsonl",
                           since.timestamp()):
        prev = {"input": 0, "output": 0, "cached": 0}
        model = None
        for obj in _iter_jsonl(p):
            t, payload = obj.get("type"), (obj.get("payload") or {})
            if t in ("turn_context", "session_meta") and payload.get("model"):
                model = payload.get("model")
            elif t == "event_msg" and payload.get("type") == "token_count":
                tot = (payload.get("info") or {}).get("total_token_usage") or {}
                cur = {k: int(tot.get(f"{k}_tokens" if k != "cached" else "cached_input_tokens",
                                     0) or 0) for k in ("input", "output", "cached")}
                d = (dict(cur) if cur["input"] < prev["input"] or cur["output"] < prev["output"]
                     else {k: max(cur[k] - prev[k], 0) for k in cur})
                prev = cur
                ts = _to_local(obj.get("timestamp", ""))
                if ts is None or ts < since:
                    continue
                pi, po = _weight(model)
                w = max(d["input"] - d["cached"], 0) * pi + d["cached"] * pi * 0.1 + d["output"] * po
                recs.append((ts, short_model(model), w))
    return recs


def model_mix(recs, top=3):
    """[(모델, 비중0~1)] 을 비중 순으로. 합은 항상 1(나머지는 '기타'로 묶음)."""
    agg = {}
    for _ts, model, w in recs:
        agg[model] = agg.get(model, 0.0) + w
    total = sum(agg.values())
    if total <= 0:
        return []
    rows = sorted(agg.items(), key=lambda kv: kv[1], reverse=True)
    out = [(m, w / total) for m, w in rows[:top] if w / total >= 0.005]
    rest = 1.0 - sum(s for _m, s in out)
    if rest >= 0.005:
        out.append(("기타", rest))
    return out


# ---------------- 모으기 ----------------
# (키, 이름, 색, 잔량 조회, 로컬 로그, 설정 폴더)
SERVICES = (("claude", "Claude", CLAUDE_COL, fetch_claude, claude_records, claude_home),
            ("codex", "Codex", CODEX_COL, fetch_codex, codex_records, codex_home))


def active_services():
    """이 PC 에서 쓰는 서비스만(설정 폴더가 있는 것). 하나도 없으면 둘 다 — 로그인 안내를 띄우려고."""
    return [s for s in SERVICES if s[5]().exists()] or list(SERVICES)


def fetch_all():
    """서비스들을 동시에 조회해 창(행) 목록을 만든다. 전체 대기시간 = 가장 느린 호출."""
    svcs = active_services()
    res = {}

    def run(svc, fetch):
        try:
            res[svc] = fetch()
        except UserError as e:
            res[svc] = e
        except Exception as e:                       # noqa: BLE001
            res[svc] = UserError("확인하지 못했습니다", str(e)[:80])

    ts = [threading.Thread(target=run, args=(s[0], s[3]), daemon=True) for s in svcs]
    for t in ts:
        t.start()
    for t in ts:
        t.join()

    rows = []
    for svc, name, accent, _fetch, reader, _home in svcs:
        r = res.get(svc) or UserError("확인하지 못했습니다")
        if isinstance(r, UserError):
            rows.append({"svc": svc, "name": name, "accent": accent, "err": (r.title, r.hint)})
            continue
        # 각 창의 시작 시각 이후 로그를 모델별로 묶어 비중을 붙인다(로컬 파일만 읽음 = 무료).
        try:
            recs = reader(min(w["since"] for w in r))
        except Exception:                            # noqa: BLE001
            recs = None
        for w in r:
            w.update(svc=svc, name=name, accent=accent, err=None)
            if recs is not None:
                w["mix"] = model_mix([x for x in recs if x[0] >= w["since"]])
            rows.append(w)
    return rows


def panel_items(rows, pref):
    """상단 표시줄 그림용 [(이름, 잔량)]. pref: 5h = 가장 짧은 창 / 7d = 주간 / low = 가장 낮은 창."""
    out = []
    for svc, name, *_ in active_services():
        rs = [r for r in rows if r["svc"] == svc and not r.get("err")]
        if not rs:
            out.append((name, None))
            continue
        if pref == "low":
            r = min(rs, key=lambda r: r["remain"])
        elif pref == "7d":
            r = next((r for r in rs if r["key"] == "7d"), rs[-1])
        else:
            r = rs[0]
        out.append((name, round(r["remain"], 3)))
    return out


def tray_lines(rows):
    """표시줄 메뉴 맨 위의 요약 줄들."""
    out = []
    for r in rows:
        if r.get("err"):
            out.append(f"{r['name']}  ·  {r['err'][0]}")
            continue
        rel, _ab = fmt_reset(r["reset"])
        out.append(f"{r['name']} {r['label']}   {max(0.0, r['remain']) * 100:.0f}%"
                   + (f"  ·  {rel}" if rel else ""))
    return out


def mix_text(row):
    """'opus 5.5 40% · opus 5 7%' — 창 전체 대비 각 모델이 쓴 몫(합 = 사용한 %)."""
    used = 1.0 - max(0.0, min(1.0, row["remain"]))
    return " · ".join(f"{m} {s * used * 100:.0f}%" for m, s in row.get("mix") or []
                      if s * used >= 0.005)


# ---------------- 상단 표시줄 그림 ----------------
PANEL_BG = "#1c1c1c"          # 상단 표시줄 배경(채움색을 흐리게 섞을 기준)
PANEL_NAME = "#d2d6e2"
PANEL_EDGE = "#8b90a3"
# 맥 메뉴 막대 스타일: 흰색 단색(이름은 조금 흐리게, 배터리 테두리는 반투명), 낮을 때만 빨강
PANEL_TEXT, PANEL_DIM = (255, 255, 255, 240), (255, 255, 255, 165)
PANEL_LINE, PANEL_LOW = (255, 255, 255, 110), (255, 69, 58, 255)
_FONT_FILES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf",
    "/usr/share/fonts/dejavu-sans-fonts/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
    "C:/Windows/Fonts/segoeuib.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
]


def _load_font(px):
    from PIL import ImageFont
    for p in _FONT_FILES:
        if os.path.exists(p):
            try:
                return ImageFont.truetype(p, px)
            except Exception:
                continue
    f = _font_file("bold")
    try:
        return ImageFont.truetype(f[0], px, index=f[1]) if f else ImageFont.load_default()
    except Exception:
        return ImageFont.load_default()


def panel_icon_height(screen_h):
    """상단 표시줄 아이콘을 몇 px 로 그릴지 자동 결정. 화면 배율(HiDPI)·해상도를 보고 맞춘다.
    표시줄 높이는 알 수 없으므로 넉넉히 그려두고 축소를 맡긴다(확대되면 뭉개지므로)."""
    scale = desktop_scale()
    if scale == 1 and screen_h >= 2000:
        return 66                                 # 배율 없이 쓰는 4K: 표시줄도 그만큼 크다
    return 44 * scale


def panel_strip(items, h, spin=None):
    """상단 표시줄 그림 — 맥 메뉴 막대처럼: 'Claude 72% ▭  Codex 100% ▭'.

    흰색 단색에 가는 배터리, 숫자는 배터리 옆. 색은 20% 미만일 때만(빨강) — 애플 상태 막대와 같은 규칙.
    숫자 칸은 '100%' 폭으로 고정해 72%↔100% 로 바뀌어도 아이콘이 들썩이지 않는다.
    글자·곡선이 계단처럼 깨지지 않도록 4배로 그린 뒤 축소(supersampling)한다.
    spin 에 각도를 주면 맨 앞에서 원호가 돈다(갱신 중). 갱신 중이 아닐 때도 그 자리는 비워 둔다.
    """
    from PIL import Image, ImageDraw
    ss = 4
    # 표시줄은 그림을 "높이에 맞춰" 늘리므로, 위아래 여백을 넣어두면 그만큼 작게 그려진다.
    # (여백 없이 꽉 채우면 옆의 시계 글자보다 훨씬 크게 보여서 튄다)
    canvas_h = max(16, int(h)) * ss
    H = canvas_h * 0.70                           # 실제 내용 높이
    fn, fp = ui_font(int(H * 0.62), "regular"), ui_font(int(H * 0.62), "medium")
    probe = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    lead = H * 0.50
    bw, bh, nub = H * 1.08, H * 0.54, H * 0.08   # 배터리 몸통(맥 메뉴 막대 비율 2:1)
    g_name, g_bat, gap = H * 0.22, H * 0.18, H * 0.70
    pct_w = probe.textlength("100%", font=fp)
    segs, x = [], lead
    for name, rem in items:
        nw = probe.textlength(name, font=fn)
        segs.append((name, rem, x, nw))
        x += nw + g_name + pct_w + g_bat + bw + nub + gap
    total = max(1, int(x - gap + nub))

    img = Image.new("RGBA", (total, canvas_h), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    mid = canvas_h / 2
    if spin is not None:                          # 갱신 중: 앞자리에서 가는 원호가 돈다
        r = H * 0.19
        d.arc([lead / 2 - r, mid - r, lead / 2 + r, mid + r], start=spin, end=spin + 270,
              fill=PANEL_TEXT, width=max(2, int(H * 0.075)))
    for name, rem, x0, nw in segs:
        low = rem is not None and rem < 0.2
        d.text((x0, mid), name, font=fn, fill=PANEL_DIM, anchor="lm")
        pr = x0 + nw + g_name + pct_w
        d.text((pr, mid), f"{rem * 100:.0f}%" if rem is not None else "—",
               font=fp, fill=PANEL_LOW if low else PANEL_TEXT, anchor="rm")
        bx, top, bot = pr + g_bat, mid - bh / 2, mid + bh / 2
        sw = max(2, int(H * 0.05))
        d.rounded_rectangle([bx, top, bx + bw, bot], radius=bh * 0.30, outline=PANEL_LINE, width=sw)
        d.rounded_rectangle([bx + bw + sw * 0.8, mid - bh * 0.17, bx + bw + nub, mid + bh * 0.17],
                            radius=nub * 0.5, fill=PANEL_LINE)
        if rem is not None and rem > 0:           # 잔량만큼 안쪽을 흰색(낮으면 빨강)으로 채움
            pad = sw + H * 0.035
            iw = max(bh * 0.14, (bw - 2 * pad) * min(1.0, rem))
            d.rounded_rectangle([bx + pad, top + pad, bx + pad + iw, bot - pad],
                                radius=(bh - 2 * pad) * 0.28, fill=PANEL_LOW if low else PANEL_TEXT)
    return img.resize((max(1, total // ss), max(1, canvas_h // ss)), Image.LANCZOS)


def tray_square(items):
    """윈도우 트레이(정사각 아이콘)용: 가장 낮은 잔량 하나만 배터리에 넣어 보여준다."""
    from PIL import Image, ImageDraw
    vals = [r for _, r in items if r is not None]
    rem = min(vals) if vals else None
    ss, S = 4, 64
    img = Image.new("RGBA", (S * ss, S * ss), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    f = _load_font(int(S * ss * 0.30))
    box = [2 * ss, 18 * ss, 55 * ss, 46 * ss]
    d.rounded_rectangle(box, radius=6 * ss, outline=PANEL_EDGE, width=3 * ss)
    d.rounded_rectangle([57 * ss, 26 * ss, 63 * ss, 38 * ss], radius=2 * ss, fill=PANEL_EDGE)
    if rem is not None:
        w = (box[2] - box[0] - 6 * ss) * max(0.0, min(1.0, rem))
        if w > 1:
            d.rectangle([box[0] + 3 * ss, box[1] + 3 * ss, box[0] + 3 * ss + w, box[3] - 3 * ss],
                        fill=fill_color(rem, PANEL_BG))
    d.text(((box[0] + box[2]) / 2, (box[1] + box[3]) / 2),
           f"{rem * 100:.0f}" if rem is not None else "—",
           font=f, fill=level_color(rem), anchor="mm")
    return img.resize((S, S), Image.LANCZOS)


# ---------------- 상단 표시줄 (트레이) ----------------
class LinuxTray:
    """리눅스: D-Bus StatusNotifierItem 을 같은 프로세스에서 직접(sni_tray). 그림은 PNG 파일로 넘긴다."""

    def __init__(self, post, screen_h):
        from sni_tray import SniTray
        base = os.environ.get("XDG_RUNTIME_DIR") or tempfile.gettempdir()
        for d in os.listdir(base):                    # 강제 종료된 옛 인스턴스가 남긴 폴더 정리
            if d.startswith("tokenwidget-") and d[12:].isdigit() and not os.path.exists(f"/proc/{d[12:]}"):
                shutil.rmtree(os.path.join(base, d), ignore_errors=True)
        self._dir = os.path.join(base, f"tokenwidget-{os.getpid()}")
        self._h = panel_icon_height(screen_h)
        self._post = post
        self._keys = []
        first = self._icon([(s[1], None) for s in active_services()], None)
        self._sni = SniTray("tokenwidget", APP_NAME, icon=first,
                            on_activate=lambda: post("show"),        # 더블클릭 = 창 열기
                            on_secondary=lambda: post("refresh"))    # 가운데클릭 = 새로고침

    def ok(self, timeout=0):
        return self._sni.wait_registered(timeout)

    def _icon(self, items, spin):
        # 표시줄은 아이콘을 파일 이름으로 캐시한다 → 내용이 바뀌면 이름도 바꾼다.
        # 같은 내용·같은 각도면 같은 파일을 다시 써서 회전 애니메이션이 캐시를 탄다.
        key = hashlib.md5(repr((items, self._h)).encode()).hexdigest()[:12]
        if key not in self._keys:
            self._keys = (self._keys + [key])[-2:]      # 직전 것까지는 남겨 둔다(읽는 중일 수 있음)
            for f in (os.listdir(self._dir) if os.path.isdir(self._dir) else []):
                if not any(f.startswith(f"p_{k}_") for k in self._keys):
                    try:
                        os.remove(os.path.join(self._dir, f))
                    except OSError:
                        pass
        path = os.path.join(self._dir, f"p_{key}_{'x' if spin is None else spin}.png")
        if not os.path.exists(path):
            os.makedirs(self._dir, mode=0o700, exist_ok=True)   # tmp 정리 등으로 지워졌어도 다시
            panel_strip(items, self._h, spin).save(path)
        return path

    def show_items(self, items, spin=None):
        self._sni.set_icon(self._icon(items, spin))

    def set_menu(self, lines, shown):
        post = self._post
        self._sni.set_menu([(t, None) for t in lines] + [
            (None, None),
            ("창 숨기기" if shown else "창 열기", lambda: post("toggle")),
            ("지금 새로고침", lambda: post("refresh")),
            ("설정…", lambda: post("settings")),
            (None, None),
            ("종료", lambda: post("quit")),
        ])
        self._sni.set_title(" · ".join(lines) or APP_NAME)

    def close(self):
        self._sni.close()
        shutil.rmtree(self._dir, ignore_errors=True)


class ProcessTray:
    """윈도우/맥: pystray 를 별도 프로세스로 띄우고 stdin/stdout JSON 한 줄로 주고받는다
    (Tk 와 트레이 라이브러리를 한 프로세스에서 돌리면 불안정하다)."""

    def __init__(self, post):
        cmd = ([sys.executable, "--tray"] if FROZEN
               else [sys.executable, os.path.abspath(__file__), "--tray"])
        kwargs = {"creationflags": 0x08000000} if IS_WIN else {}
        self._p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   text=True, **kwargs)
        self._items, self._lines = [], []
        threading.Thread(target=self._listen, args=(post,), daemon=True).start()

    def _listen(self, post):
        for line in self._p.stdout:
            try:
                a = json.loads(line).get("action")
            except ValueError:
                continue
            if a:
                post(a)

    def ok(self, timeout=0):
        if timeout:
            try:
                self._p.wait(timeout)
            except subprocess.TimeoutExpired:
                pass
        return self._p.poll() is None

    def show_items(self, items, spin=None):
        if spin is None and items != self._items:
            self._items = items
            self._send()

    def set_menu(self, lines, shown):
        if lines != self._lines:
            self._lines = lines
            self._send()

    def _send(self):
        if self._p.poll() is not None:
            return
        try:
            self._p.stdin.write(json.dumps({"update": {
                "items": self._items, "lines": self._lines,
                "tip": " · ".join(self._lines) or APP_NAME}}) + "\n")
            self._p.stdin.flush()
        except OSError:
            pass

    def close(self):
        try:
            self._p.stdin.close()
            self._p.terminate()
        except OSError:
            pass


def make_tray(post, screen_h):
    try:
        return LinuxTray(post, screen_h) if IS_LINUX else ProcessTray(post)
    except Exception as e:                                # noqa: BLE001
        print(f"[알림] 상단 표시줄 아이콘을 띄울 수 없습니다: {e}", file=sys.stderr)
        return None


def _emit(action):
    print(json.dumps({"action": action}), flush=True)


def run_tray_process():
    """(윈도우/맥) --tray 로 실행된 하위 프로세스: pystray 아이콘."""
    try:
        import pystray
    except ImportError as e:
        print(f"트레이 사용 불가: {e}", file=sys.stderr)
        return

    state = {"lines": []}

    def build_menu():
        items = [pystray.MenuItem(t, None, enabled=False) for t in state["lines"]]
        items += [pystray.Menu.SEPARATOR,
                  pystray.MenuItem("창 열기/닫기", lambda i, it: _emit("toggle"), default=True),
                  pystray.MenuItem("지금 새로고침", lambda i, it: _emit("refresh")),
                  pystray.MenuItem("설정…", lambda i, it: _emit("settings")),
                  pystray.Menu.SEPARATOR,
                  pystray.MenuItem("종료", lambda i, it: (_emit("quit"), i.stop()))]
        return items

    icon = pystray.Icon("tokenwidget", tray_square([]), APP_NAME,
                        pystray.Menu(lambda: build_menu()))

    def read_stdin():
        for line in sys.stdin:
            try:
                u = json.loads(line).get("update")
                if not u:
                    continue
                state["lines"] = u["lines"]
                icon.icon = tray_square(u["items"])
                icon.title = u["tip"]
                icon.update_menu()
            except Exception:
                continue
        icon.stop()

    threading.Thread(target=read_stdin, daemon=True).start()
    icon.run()


# ---------------- 화면 그리기 (Pillow) ----------------
# Tk 기본 위젯으로는 둥근 모서리·매끈한 막대·굵은 글꼴을 못 쓴다(Tk 8.6.12 는 bold 에서 SIGSEGV).
# 그래서 창 내용 전체를 Pillow 로 한 장 그려 붙이고, 누를 수 있는 곳은 좌표(hit)로 처리한다.
_UI_FAMILIES = ("Pretendard", "Noto Sans CJK KR", "Noto Sans KR", "NanumBarunGothic",
                "NanumGothic", "Source Han Sans K")
_FONT_PATHS = {}
_FONTS = {}


def _font_file(weight):
    """(파일, ttc 번호) — 한글이 되는 UI 글꼴. weight: regular / medium / bold."""
    if weight in _FONT_PATHS:
        return _FONT_PATHS[weight]
    found = None
    if IS_WIN:
        name = {"regular": "malgun.ttf"}.get(weight, "malgunbd.ttf")
        p = os.path.join(os.environ.get("WINDIR", "C:/Windows"), "Fonts", name)
        found = (p, 0) if os.path.exists(p) else None
    elif sys.platform == "darwin":
        p = "/System/Library/Fonts/AppleSDGothicNeo.ttc"
        found = (p, 0) if os.path.exists(p) else None
    elif shutil.which("fc-match"):
        pats = [f"{fam}:weight={weight}" for fam in _UI_FAMILIES] + [f"sans-serif:lang=ko:weight={weight}"]
        for i, pat in enumerate(pats):
            try:
                out = subprocess.run(["fc-match", "-f", "%{family}\t%{file}\t%{index}", pat],
                                     capture_output=True, text=True, timeout=3, env=sys_env()).stdout
            except (OSError, subprocess.SubprocessError):
                break
            fam, _, rest = out.partition("\t")
            path, _, idx = rest.partition("\t")
            generic = i == len(pats) - 1
            if os.path.exists(path) and (generic or _UI_FAMILIES[i].lower() in fam.lower()):
                found = (path, int(idx or 0))
                break
    if not found:
        for p in ("/usr/share/fonts/truetype/nanum/NanumGothic.ttf",
                  "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"):
            if os.path.exists(p):
                found = (p, 0)
                break
    _FONT_PATHS[weight] = found
    return found


def ui_font(px, weight="regular"):
    key = (px, weight)
    if key not in _FONTS:
        from PIL import ImageFont
        f = _font_file(weight)
        try:
            _FONTS[key] = ImageFont.truetype(f[0], px, index=f[1]) if f else ImageFont.load_default()
        except Exception:
            _FONTS[key] = ImageFont.load_default()
    return _FONTS[key]


class Painter:
    """한 화면을 Pillow 로 그린다. 도형은 3배로 그려 줄여서 가장자리를 매끄럽게, 글자는 제 크기로 또렷하게.
    좌표는 논리 단위 — s 배 하면 픽셀. 높이는 다 그린 뒤에 정해지므로 그리기 명령을 모았다가 render 에서 칠한다.
    layer() 뒤에 그린 것(메뉴처럼 떠 있는 것)은 앞 층의 글자까지 모두 덮는다."""

    SS = 3

    def __init__(self, s, width):
        self.s, self.w = s, width
        self._hits, self._layers = [], []
        self.layer()

    def layer(self):
        self._shapes, self._texts = [], []
        self._layers.append((self._shapes, self._texts))

    def font(self, size, weight="regular"):
        return ui_font(max(6, int(round(size * self.s))), weight)

    def measure(self, text, size, weight="regular"):
        return self.font(size, weight).getlength(text) / self.s

    def mark(self):
        return len(self._shapes)

    def rrect(self, x, y, w, h, r, fill, at=None):
        op = ("rrect", (x, y, w, h, r, fill))
        if at is None:
            self._shapes.append(op)
        else:
            self._shapes.insert(at, op)             # 카드 배경처럼 내용보다 먼저 칠해야 하는 것

    def circle(self, cx, cy, r, fill):
        self._shapes.append(("circle", (cx, cy, r, fill)))

    def shadow(self, x, y, w, h, r, blur, alpha):
        """떠 있는 것(메뉴) 아래의 부드러운 그림자."""
        self._shapes.append(("shadow", (x, y, w, h, r, blur, alpha)))

    def arc(self, cx, cy, r, start, end, width, fill):
        self._shapes.append(("arc", (cx, cy, r, start, end, width, fill)))

    def polygon(self, pts, fill):
        self._shapes.append(("poly", (pts, fill)))

    def text(self, x, y, text, size, weight="regular", fill=TEXT, anchor="ls"):
        self._texts.append((x, y, text, size, weight, fill, anchor))

    def hit(self, key, x, y, w, h):
        self._hits.append((key, x, y, x + w, y + h))

    def elide(self, text, max_w, size, weight="regular"):
        if self.measure(text, size, weight) <= max_w:
            return text
        while text and self.measure(text + "…", size, weight) > max_w:
            text = text[:-1]
        return text + "…"

    def wrap(self, x, y, max_w, text, size, weight="regular", fill=TEXT):
        """줄바꿈해서 그린다(가능하면 띄어쓰기에서). y = 첫 줄 기준선, 마지막 줄 기준선을 돌려준다."""
        lines, cur = [], ""
        for ch in text:
            if self.measure(cur + ch, size, weight) <= max_w:
                cur += ch
                continue
            cut = cur.rfind(" ")
            lines.append(cur[:cut] if cut > 0 else cur)
            cur = (cur[cut + 1:] if cut > 0 else "") + ch
        lines.append(cur)
        lh = size * 1.45
        for i, ln in enumerate(lines):
            self.text(x, y + i * lh, ln, size, weight, fill)
        return y + (len(lines) - 1) * lh

    def render(self, height, bg=BG):
        from PIL import Image
        s = self.s
        W, H = max(1, int(round(self.w * s))), max(1, int(round(height * s)))
        img = None
        for i, (shapes, texts) in enumerate(self._layers):
            if i == 0:
                big = Image.new("RGB", (W * self.SS, H * self.SS), bg)
            else:                                      # 위층: 투명한 판에 그려 아래에 겹친다
                big = Image.new("RGBA", (W * self.SS, H * self.SS), (0, 0, 0, 0))
            self._paint(big, shapes)
            small = big.resize((W, H), Image.LANCZOS)
            img = small if i == 0 else Image.alpha_composite(img.convert("RGBA"), small).convert("RGB")
            self._letter(img, texts)
        hits = [(key, x0 * s, y0 * s, x1 * s, y1 * s) for key, x0, y0, x1, y1 in self._hits]
        return img, hits

    def _paint(self, big, shapes):
        from PIL import Image, ImageDraw, ImageFilter
        k = self.s * self.SS
        d = ImageDraw.Draw(big)
        for kind, a in shapes:
            if kind == "rrect":
                x, y, w, h, r, fill = a
                r = min(r, w / 2, h / 2)
                d.rounded_rectangle([x * k, y * k, (x + w) * k - 1, (y + h) * k - 1],
                                    radius=max(0, r * k), fill=fill)
            elif kind == "circle":
                cx, cy, r, fill = a
                d.ellipse([(cx - r) * k, (cy - r) * k, (cx + r) * k, (cy + r) * k], fill=fill)
            elif kind == "arc":
                cx, cy, r, st, en, wd, fill = a
                d.arc([(cx - r) * k, (cy - r) * k, (cx + r) * k, (cy + r) * k], st, en,
                      fill=fill, width=max(1, int(wd * k)))
            elif kind == "poly":
                pts, fill = a
                d.polygon([(px * k, py * k) for px, py in pts], fill=fill)
            elif kind == "shadow":
                x, y, w, h, r, blur, alpha = a
                m = blur * 2
                layer = Image.new("L", (int((w + 2 * m) * k), int((h + 2 * m) * k)), 0)
                ImageDraw.Draw(layer).rounded_rectangle([m * k, m * k, (m + w) * k, (m + h) * k],
                                                        radius=r * k, fill=alpha)
                layer = layer.filter(ImageFilter.GaussianBlur(blur * k / 2))
                x0, y0 = int((x - m) * k), int((y - m) * k)
                black = (0, 0, 0, 255) if big.mode == "RGBA" else (0, 0, 0)
                big.paste(black, (x0, y0, x0 + layer.width, y0 + layer.height), layer)

    def _letter(self, img, texts):
        from PIL import ImageDraw
        d = ImageDraw.Draw(img)
        for x, y, text, size, weight, fill, anchor in texts:
            d.text((x * self.s, y * self.s), text, font=self.font(size, weight), fill=fill, anchor=anchor)


HEADER_H = 42


def _icon_refresh(p, cx, cy, hover, spin):
    if hover:
        p.circle(cx, cy, 14, FILL)
    col = TEXT if hover else TEXT2
    r = 6.5
    if spin is not None:                       # 갱신 중: 화살표 없이 원호만 돈다
        p.arc(cx, cy, r, spin, spin + 270, 1.7, col)
        return
    # 시계 방향 화살표: 원호는 1시 방향에서 시작해 한 바퀴 돌아 12시에서 끝나고, 끝에 화살촉.
    p.arc(cx, cy, r, -30, 270, 1.7, col)
    p.polygon([(cx + 3.4, cy - r), (cx - 1.2, cy - r - 3.3), (cx - 1.2, cy - r + 3.3)], col)


def _icon_more(p, cx, cy, active):
    if active:
        p.circle(cx, cy, 14, FILL)
    col = TEXT if active else TEXT2
    for dx in (-5.5, 0, 5.5):
        p.circle(cx + dx, cy, 1.6, col)


def _bar(p, x, y, w, h, rem):
    p.rrect(x, y, w, h, h / 2, FILL)
    if rem > 0:
        p.rrect(x, y, max(h, w * rem), h, h / 2, level_color(rem))


def _service_card(p, x, y, w, name, accent, rows):
    P = 16
    mark, top = p.mark(), y
    y += P
    p.circle(x + P + 4, y + 8, 4, accent)
    p.text(x + P + 15, y + 8, name, 13.5, "medium", TEXT, "lm")
    y += 16
    for i, r in enumerate(rows):
        y += 14 if i == 0 else 18
        if r.get("loading"):
            p.text(x + P, y + 13, "불러오는 중…", 12.5, "regular", TEXT2)
            y += 17
            continue
        if r.get("err"):
            title, hint = r["err"]
            y = p.wrap(x + P, y + 13, w - 2 * P, title, 12.5, "regular", TEXT)
            if hint:
                y = p.wrap(x + P, y + 18, w - 2 * P, hint, 11, "regular", TEXT2)
            y += 4
            continue
        rem = max(0.0, min(1.0, r["remain"]))
        base = y + 24
        p.text(x + P, base, r["label"], 12.5, "regular", TEXT2)
        pw = p.measure("%", 13, "medium")
        p.text(x + w - P, base, "%", 13, "medium", TEXT2, "rs")
        p.text(x + w - P - pw - 1, base, f"{rem * 100:.0f}", 26, "medium",
               RED if rem < 0.2 else TEXT, "rs")
        y = base + 8
        _bar(p, x + P, y, w - 2 * P, 6, rem)
        y += 6
        rel, ab = fmt_reset(r.get("reset"))
        if rel or ab:
            y += 17
            p.text(x + P, y, rel, 11, "regular", TEXT2)
            p.text(x + w - P, y, ab, 11, "regular", TEXT3, "rs")
        mt = mix_text(r)
        if mt:
            y += 16 if (rel or ab) else 17
            p.text(x + P, y, p.elide(mt, w - 2 * P, 11), 11, "regular", TEXT3)
        y += 4
    y += P - 2
    p.rrect(x, top, w, y - top, 14, CARD, at=mark)
    return y


MENU_ITEMS = (("refresh", "지금 새로고침", "F5"), ("settings", "설정…", "Ctrl+,"),
              None, ("quit", f"{APP_NAME} 종료", "Ctrl+Q"))
MENU_W, MENU_ITEM_H = 204, 26


def _menu_size():
    return MENU_W, 10 + sum(MENU_ITEM_H if it else 11 for it in MENU_ITEMS)


def draw_menu(p, x, y, hover):
    """애플식 풀다운 메뉴: 둥근 모서리·그림자·가는 테두리, 마우스를 올린 항목은 파란 하이라이트,
    단축키는 오른쪽에 흐리게."""
    w, h = _menu_size()
    px = 1 / p.s                                        # 1픽셀
    p.layer()                                           # 카드의 글자까지 덮도록 위층에
    p.shadow(x, y + 4, w, h, 10, 12, 150)
    p.rrect(x, y, w, h, 10, "#505054")                  # 가는 테두리
    p.rrect(x + px, y + px, w - 2 * px, h - 2 * px, 10 - px, "#2a2a2d")
    yy = y + 5
    for it in MENU_ITEMS:
        if it is None:
            p.rrect(x + 12, yy + 5, w - 24, px, 0, "#48484c")
            yy += 11
            continue
        key, label, short = it
        on = hover == "menu:" + key
        if on:
            p.rrect(x + 5, yy, w - 10, MENU_ITEM_H, 6, BLUE)
        p.text(x + 15, yy + MENU_ITEM_H / 2, label, 13, "regular", "#ffffff" if on else TEXT, "lm")
        p.text(x + w - 15, yy + MENU_ITEM_H / 2, short, 12, "regular", "#dbe9ff" if on else TEXT3, "rm")
        p.hit("menu:" + key, x + 5, yy, w - 10, MENU_ITEM_H)
        yy += MENU_ITEM_H


def draw_main(p, st):
    """메인 창: 상태 + (새로고침 · 더보기) / 서비스별 카드 / 열려 있으면 더보기 메뉴."""
    W, M = p.w, 12
    menu = st["menu"]
    hv = None if menu else st["hover"]                 # 메뉴가 떠 있는 동안 뒤쪽은 반응하지 않는다
    cy = HEADER_H / 2 + 1
    p.text(M + 6, cy, st["status"], 11.5, "regular", TEXT2, "lm")
    _icon_more(p, W - M - 16, cy, hv == "more" or menu == "button")
    p.hit("more", W - M - 32, cy - 16, 32, 32)
    _icon_refresh(p, W - M - 50, cy, hv == "refresh", st["spin"])
    p.hit("refresh", W - M - 66, cy - 16, 32, 32)
    y = HEADER_H
    for svc, name, accent, *_ in st["svcs"]:
        rows = [r for r in st["rows"] if r["svc"] == svc] or [{"loading": True}]
        y = _service_card(p, M, y, W - 2 * M, name, accent, rows) + 10
    h = y + 2
    if menu:
        mw, mh = _menu_size()
        if menu == "button":                           # ··· 바로 아래, 오른쪽 끝을 맞춰서
            mx, my = W - M - mw + 4, HEADER_H - 8
        else:                                          # 우클릭한 자리(넘치면 위로 편다)
            mx, my = min(max(8, menu[0]), W - mw - 8), menu[1]
            if my + mh > h - 8:
                my = max(8, my - mh)
        draw_menu(p, mx, my, st["hover"])
        h = max(h, my + mh + 12)
    return h


def _switch(p, x, cy, on, key):
    p.rrect(x, cy - 12, 40, 24, 12, GREEN if on else FILL)
    p.circle(x + (28 if on else 12), cy, 10, "#ffffff")
    p.hit(key, x - 6, cy - 18, 52, 36)


def _stepper(p, xr, cy, key, value, enabled, hover):
    bw, bh = 76, 28
    x = xr - bw
    p.rrect(x, cy - bh / 2, bw, bh, 8, FILL)
    p.rrect(x + bw / 2 - 0.5, cy - 7, 1, 14, 0, FILL_HI)
    for sign, cx in (("-", x + bw / 4), ("+", x + bw * 3 / 4)):
        col = (TEXT if hover == key + sign else TEXT2) if enabled else FILL_HI
        p.rrect(cx - 5.5, cy - 0.9, 11, 1.8, 0.9, col)
        if sign == "+":
            p.rrect(cx - 0.9, cy - 5.5, 1.8, 11, 0.9, col)
        if enabled:
            p.hit(key + sign, cx - bw / 4, cy - bh / 2 - 6, bw / 2, bh + 12)
    p.text(x - 10, cy, value, 13, "regular", TEXT2 if enabled else TEXT3, "rm")


def _segmented(p, x, y, w, h, choices, sel, prefix, hover):
    p.rrect(x, y, w, h, 9, FILL)
    sw = (w - 4) / len(choices)
    for i, (k, label) in enumerate(choices):
        sx = x + 2 + i * sw
        if k == sel:
            p.rrect(sx, y + 2, sw, h - 4, 7, FILL_HI)
        col = TEXT if (k == sel or hover == prefix + k) else TEXT2
        p.text(sx + sw / 2, y + h / 2, label, 12.5, "medium" if k == sel else "regular", col, "mm")
        p.hit(prefix + k, sx, y, sw, h)


def _section(p, x, y, title):
    p.text(x + 16, y + 13, title, 11, "regular", TEXT2)
    return y + 21


def _footnote(p, x, y, lines):
    for line in lines:
        y += 16
        p.text(x + 16, y, line, 10.5, "regular", TEXT3)
    return y + 18


def _group(p, x, y, w, rows, hover):
    """설정 묶음 카드. rows: [(제목, ('switch', 키, 켜짐) | ('stepper', 키, 값글자, 사용가능))]"""
    ROW_H = 46
    mark, top = p.mark(), y
    for i, (title, ctl) in enumerate(rows):
        cy = y + ROW_H / 2
        if i:
            p.rrect(x + 16, y, w - 16, 1 / p.s, 0, SEP)          # 가는 구분선(1px)
        enabled = ctl[0] == "switch" or ctl[3]
        p.text(x + 16, cy, title, 13, "regular", TEXT if enabled else TEXT3, "lm")
        if ctl[0] == "switch":
            _switch(p, x + w - 16 - 40, cy, ctl[2], ctl[1])
        else:
            _stepper(p, x + w - 16, cy, ctl[1], ctl[2], ctl[3], hover)
        y += ROW_H
    p.rrect(x, top, w, y - top, 12, CARD, at=mark)
    return y


def draw_settings(p, st):
    """설정 창 — 맥 '설정' 창처럼 바꾸는 즉시 적용되고, 창의 X · Esc 로 닫는다."""
    W, M = p.w, 14
    cfg, hv = st["cfg"], st["hover"]
    y = _section(p, M, 8, "상단 표시줄")
    mark, top = p.mark(), y
    _segmented(p, M + 8, y + 8, W - 2 * M - 16, 30, PANEL_CHOICES, cfg["panel_window"], "panel:", hv)
    y += 46
    p.rrect(M, top, W - 2 * M, y - top, 12, CARD, at=mark)
    y = _footnote(p, M, y, ("상단 표시줄 배터리에 이 한도의 잔량이 보입니다",))

    y = _section(p, M, y, "새로고침")
    y = _group(p, M, y, W - 2 * M, [
        ("자동 새로고침", ("switch", "auto", cfg["auto"])),
        ("간격", ("stepper", "interval", fmt_interval(cfg["interval_min"]), cfg["auto"])),
        ("질문하면 바로 반영", ("switch", "on_activity", cfg["on_activity"])),
    ], hv)
    y = _footnote(p, M, y, ("Claude 확인 1회 ≈ 23토큰 · Codex 는 무료",
                            "창에서 가운데 클릭 또는 F5 로 언제든 새로고침"))

    y = _section(p, M, y, "일반")
    rows = [("로그인 시 자동 실행", ("switch", "autostart", cfg["autostart"]))] if st["autostart_ok"] else []
    rows.append(("크기", ("stepper", "size", f"{round(cfg['size'] * 100)}%", True)))
    y = _group(p, M, y, W - 2 * M, rows, hv) + 22
    p.text(W / 2, y, f"{APP_NAME} {VERSION}", 10.5, "regular", TEXT3, "mm")
    return y + 18


def to_photo(img):
    buf = io.BytesIO()
    img.save(buf, "PNG", compress_level=1)
    return tk.PhotoImage(data=base64.b64encode(buf.getvalue()).decode("ascii"))


def _snap(v, steps):
    try:
        return min(steps, key=lambda s: abs(s - float(v)))
    except (TypeError, ValueError):
        return steps[0]


def _geom_pos(win):
    """창 위치. winfo_x/y 는 창 장식(제목표시줄) 높이만큼 값이 커져서, 그대로 다시
    geometry() 에 넣으면 갱신할 때마다 창이 아래로 밀린다 → 요청값을 문자열에서 읽는다."""
    m = re.search(r"([+-]\d+)([+-]\d+)$", win.geometry())
    return (int(m.group(1)), int(m.group(2))) if m else (0, 0)


# ---------------- 창 ----------------
class Surface:
    """Pillow 로 그린 한 장을 띄우는 창 — 메인 창과 설정 창이 같이 쓴다.
    누를 수 있는 곳은 좌표(hit)로 찾고, 마우스를 올리면 hover 상태로 다시 그린다."""

    def __init__(self, win, width, scale, draw, on_click, drag=False):
        self.win, self.width, self._scale = win, width, scale
        self._draw, self._on_click, self._drag = draw, on_click, drag
        self.hover, self.hits, self.size, self._press = None, [], (0, 0), None
        self.label = tk.Label(win, bd=0, highlightthickness=0, bg=BG)
        self.label.pack()
        self.label.bind("<Motion>", self._motion)
        self.label.bind("<Leave>", lambda e: self.set_hover(None))
        self.label.bind("<ButtonPress-1>", self._down)
        self.label.bind("<B1-Motion>", self._move)
        self.label.bind("<ButtonRelease-1>", self._up)

    def visible(self):
        return self.win.winfo_exists() and self.win.state() == "normal"

    def render(self, force=False):
        if not force and not self.visible():
            return
        p = Painter(self._scale(), self.width)
        img, self.hits = p.render(self._draw(p, self.hover))
        self._photo = to_photo(img)
        self.label.config(image=self._photo)
        if img.size != self.size:
            self.size = img.size
            # 감춰졌거나(withdrawn) 최소화된(iconic) 창에 geometry() 를 걸면 창관리자가
            # 창을 도로 띄워버린다 → 보이는 상태일 때만 크기를 맞춘다(위치는 그대로).
            if self.visible():
                self.win.geometry(f"{img.size[0]}x{img.size[1]}")

    def hit(self, x, y):
        for key, x0, y0, x1, y1 in reversed(self.hits):   # 나중에 그린 것(메뉴)이 위에 있다
            if x0 <= x < x1 and y0 <= y < y1:
                return key
        return None

    def set_hover(self, key):
        if key != self.hover:
            self.hover = key
            self.render()

    def _motion(self, e):
        if not self._press:
            self.set_hover(self.hit(e.x, e.y))

    def _down(self, e):
        x, y = _geom_pos(self.win)
        self._press = [e.x_root, e.y_root, e.x_root - x, e.y_root - y, False]

    def _move(self, e):
        p = self._press
        if not p or not self._drag:
            return
        if not p[4] and abs(e.x_root - p[0]) + abs(e.y_root - p[1]) < 5:
            return                                    # 살짝 흔들린 클릭은 이동으로 치지 않는다
        p[4] = True
        self.win.geometry(f"+{e.x_root - p[2]}+{e.y_root - p[3]}")

    def _up(self, e):
        p, self._press = self._press, None
        self._on_click(self.hit(e.x, e.y), bool(p and p[4]))


class App:
    WIDTH, SETTINGS_WIDTH = 272, 312

    def __init__(self, background=False):
        self.root = tk.Tk(className="TokenWidget")
        self.root.report_callback_exception = lambda *a: traceback.print_exception(*a)
        self.root.title(APP_NAME)
        self.root.withdraw()
        icon = app_icon_path()          # 독·창 아이콘 (.desktop 의 Icon=tokenwidget 과 같은 그림)
        if icon:
            try:
                self._icon_img = tk.PhotoImage(file=icon)
                self.root.iconphoto(True, self._icon_img)
            except tk.TclError:
                pass
        self.root.configure(bg=BG)
        self.root.resizable(False, False)
        self.root.attributes("-topmost", True)

        cfg = load_config()
        self.cfg = {
            "size": _snap(cfg.get("size", 1.0), SIZE_STEPS),
            "interval_min": _snap(cfg.get("interval_min", DEFAULT_INTERVAL_MIN), INTERVAL_STEPS),
            "auto": bool(cfg.get("auto", True)),                # 주기 갱신 사용 여부
            "on_activity": bool(cfg.get("on_activity", True)),  # 질문하면 그때 갱신
            "panel_window": cfg.get("panel_window") if cfg.get("panel_window") in dict(PANEL_CHOICES) else "5h",
            "autostart": bool(cfg.get("autostart", FROZEN)),    # 실행파일은 기본 켜짐
            "proxy": str(cfg.get("proxy") or ""),               # 비우면 자동(환경변수·Claude 설정·GNOME)
        }
        self._pos = cfg.get("pos")
        _net["proxy"] = self.cfg["proxy"] or None
        self._dpi = max(0.5, self.root.winfo_fpixels("1i") / 96.0) * desktop_scale() * text_scale()

        self._rows, self._updated = [], None
        self._busy, self._spin, self._spin_ticks = False, None, 0
        self._menu = None                 # 창 안의 더보기 메뉴: None / "button"(··· 아래) / (x, y)(우클릭)
        self._settings = None             # 설정 창(Surface) — 따로 뜨는 창
        self._inbox = queue.Queue()
        self._want_show = self._want_quit = self._quitting = False
        self._after_id = self._act_id = None
        self._last_fetch = 0.0

        self.main = Surface(self.root, self.WIDTH, self._scale, self._draw_main, self._main_click,
                            drag=True)
        self.main.label.bind("<Button-2>", lambda e: self.refresh())    # 휠클릭 = 즉시 갱신
        self.main.label.bind("<Button-3>", self._context_menu)          # 우클릭 = 그 자리에 메뉴
        self._bind_keys(self.root)
        self.root.bind("<Escape>", lambda e: self._close_menu() if self._menu else self._close_window())
        self.root.bind("<FocusOut>", lambda e: self._close_menu())      # 다른 곳을 누르면 메뉴는 닫힌다
        self.root.protocol("WM_DELETE_WINDOW", self._close_window)     # 창의 X = 감추기(표시줄 있으면)

        # 다른 스레드·시그널에서 온 요청은 전부 _inbox/플래그로 받아 Tk 스레드에서 처리한다.
        if hasattr(signal, "SIGUSR1"):                   # 앱 아이콘을 다시 누름 → 창 띄우기
            signal.signal(signal.SIGUSR1, lambda *_: setattr(self, "_want_show", True))
        signal.signal(signal.SIGTERM, lambda *_: setattr(self, "_want_quit", True))
        self._want_show = bool(_early_show)

        self.tray = make_tray(self._inbox.put, self.root.winfo_screenheight())
        tray_ok = self.tray is not None and self.tray.ok(1.5)
        if FROZEN and autostart_supported():
            set_autostart(self.cfg["autostart"])       # 설치본이 옮겨졌어도 자동 실행 경로를 맞춘다

        self.main.render(force=True)
        self._place()
        # 자동 실행(--background)이면 표시줄에만 뜬다. 표시줄이 끝내 안 뜨면 창을 보여서
        # 앱이 "안 보이는 채로" 도는 일이 없게 한다.
        if background and self.tray is not None:
            if not tray_ok:
                self.root.after(15000, lambda: None if self.tray.ok() else self._show())
        else:
            self._show(refresh=False)
        self._start_activity_watch()
        self._pump()
        self._tick()
        self.refresh()

    def _bind_keys(self, win):
        """맥 앱처럼 어느 창에서든 같은 단축키."""
        for k in ("<F5>", "<Control-r>"):
            win.bind(k, lambda e: self.refresh())
        win.bind("<Control-comma>", lambda e: self._open_settings())
        win.bind("<Control-q>", lambda e: self._quit())
        for k in ("<plus>", "<equal>", "<KP_Add>"):
            win.bind(k, lambda e: self._step("size", +1))
        for k in ("<minus>", "<KP_Subtract>"):
            win.bind(k, lambda e: self._step("size", -1))

    # ---- 치수 ----
    def _scale(self):
        return self._dpi * self.cfg["size"]

    def _place(self):
        w, h = self.main.size
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        try:
            x, y = int(self._pos[0]), int(self._pos[1])
        except (TypeError, ValueError, IndexError):
            x, y = sw - w - 24, 48                       # 처음: 오른쪽 위, 상단 표시줄 아래
        # 모니터 구성이 바뀌어 화면 밖에 저장된 위치면 화면 안으로 당긴다.
        x, y = min(max(0, x), max(0, sw - 80)), min(max(0, y), max(0, sh - 80))
        self.root.geometry(f"{w}x{h}+{x}+{y}")

    # ---- 그리기 ----
    def _draw_main(self, p, hover):
        return draw_main(p, {"rows": self._rows, "svcs": active_services(), "hover": hover,
                             "spin": self._spin if self._busy else None,
                             "status": self._status(), "menu": self._menu})

    def _draw_settings(self, p, hover):
        return draw_settings(p, {"cfg": self.cfg, "hover": hover,
                                 "autostart_ok": autostart_supported()})

    def _render_all(self):
        self.main.render()
        if self._settings is not None:
            self._settings.render()

    def _status(self):
        if self._busy:
            return "업데이트 중…"
        if not self._updated:
            return ""
        m = int((datetime.now() - self._updated).total_seconds() // 60)
        if m < 1:
            return "방금 업데이트됨"
        return f"{m}분 전 업데이트됨" if m < 60 else f"{self._updated:%H:%M} 업데이트됨"

    def _tick(self):
        """'N분 전' 글자와 리셋까지 남은 시간이 흐르도록 30초마다 다시 그린다."""
        if not self._busy:
            self.main.render()
        self.root.after(30000, self._tick)

    # ---- 메인 창: 누르기 · 메뉴 ----
    def _main_click(self, key, dragged):
        if dragged:
            self._persist()
            return
        if self._menu is not None:               # 메뉴가 떠 있으면: 항목이면 실행, 아니면 닫기만
            was, self._menu = self._menu, None
            self.main.render()
            if key and key.startswith("menu:"):
                self._command(key[5:])
            elif key == "more" and was != "button":
                self._open_menu("button")
            return
        if key == "refresh":
            self.refresh()
        elif key == "more":
            self._open_menu("button")

    def _open_menu(self, where):
        self._menu = where
        self.main.render()

    def _context_menu(self, e):
        s = self._scale()
        self._open_menu((e.x / s, e.y / s))

    def _close_menu(self):
        if self._menu is not None:
            self._menu = None
            self.main.render()

    def _command(self, name):
        if name == "refresh":
            self.refresh()
        elif name == "settings":
            self._open_settings()
        elif name == "quit":
            self._quit()

    # ---- 설정 창 ----
    def _open_settings(self):
        """설정은 따로 뜨는 창(맥의 '설정…' 처럼). 이미 열려 있으면 앞으로 가져온다."""
        s = self._settings
        if s is not None and s.win.winfo_exists():
            s.win.deiconify()
            s.win.lift()
            s.win.focus_force()
            return
        # 클래스를 메인 창과 같게 해야 독에서 같은 앱으로 묶인다.
        win = tk.Toplevel(self.root, class_="Tokenwidget", name="settings")
        win.withdraw()
        win.title("설정")
        win.configure(bg=BG)
        win.resizable(False, False)
        win.attributes("-topmost", True)
        self._settings = s = Surface(win, self.SETTINGS_WIDTH, self._scale, self._draw_settings,
                                     lambda key, _dragged: key and self._apply_setting(key))
        self._bind_keys(win)
        for k in ("<Escape>", "<Control-w>"):
            win.bind(k, lambda e: self._close_settings())
        win.protocol("WM_DELETE_WINDOW", self._close_settings)
        s.render(force=True)
        w, h = s.size
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        if self.main.visible():                          # 메인 창 옆에(자리가 없으면 왼쪽에)
            mx, my = _geom_pos(self.root)
            mw = self.main.size[0]
            x = mx + mw + 16 if mx + mw + 16 + w <= sw else max(0, mx - w - 16)
            y = min(my, max(0, sh - h - 60))
        else:                                            # 표시줄에서 열었으면 화면 가운데
            x, y = (sw - w) // 2, max(40, (sh - h) // 3)
        win.geometry(f"{w}x{h}+{x}+{y}")
        win.deiconify()
        win.lift()
        win.focus_force()

    def _close_settings(self):
        if self._settings is not None and self._settings.win.winfo_exists():
            self._settings.win.destroy()
        self._settings = None

    def _apply_setting(self, key):
        """설정 창에서 누른 것 — 바로 적용하고 저장한다(적용 버튼 없음)."""
        if key[:-1] in ("interval", "size"):
            self._step(key[:-1], +1 if key[-1] == "+" else -1)
            return
        if key.startswith("panel:"):
            self.cfg["panel_window"] = key[6:]
            self._push_tray()
        elif key in ("auto", "on_activity", "autostart"):
            self.cfg[key] = not self.cfg[key]
            if key == "auto":
                self._schedule_next()          # 켜면 지금부터 다시 예약, 끄면 예약 해제
            elif key == "autostart":
                set_autostart(self.cfg[key])
        self._persist()
        self._render_all()

    def _step(self, name, d):
        cfgkey, steps = (("interval_min", INTERVAL_STEPS) if name == "interval"
                         else ("size", SIZE_STEPS))
        if name == "interval" and not self.cfg["auto"]:
            return
        i = steps.index(self.cfg[cfgkey])
        self.cfg[cfgkey] = steps[min(max(i + d, 0), len(steps) - 1)]
        if name == "interval":
            self._schedule_next()
        self._persist()
        self._render_all()

    def _persist(self):
        if self.main.visible():
            self._pos = list(_geom_pos(self.root))
        save_config({**self.cfg, "pos": self._pos})

    # ---- 갱신 ----
    def refresh(self):
        if self._busy:
            return
        if self._after_id is not None:
            self.root.after_cancel(self._after_id)
            self._after_id = None
        self._last_fetch = time.time()
        self._busy, self._spin, self._spin_ticks = True, 0, 0
        threading.Thread(target=self._work, daemon=True).start()
        self._spin_tick()

    def _work(self):
        try:
            rows = fetch_all()
        except Exception as e:                       # noqa: BLE001
            rows = [{"svc": s[0], "name": s[1], "accent": s[2],
                     "err": ("확인하지 못했습니다", str(e)[:80])} for s in active_services()]
        self._inbox.put(("rows", rows))

    def _spin_tick(self):
        """갱신 중 표시: 창의 ↻ 와 상단 표시줄 앞자리 원호가 돈다. 응답이 안 와도 30초 뒤엔 멈춘다."""
        if not self._busy:
            return
        self._spin_ticks += 1
        if self._spin_ticks > 250:
            return
        self._spin = (self._spin + 45) % 360
        if self.tray:
            self.tray.show_items(panel_items(self._rows, self.cfg["panel_window"]), self._spin)
        self.main.render()
        self.root.after(120, self._spin_tick)

    def _on_rows(self, rows):
        self._busy, self._spin = False, None
        self._rows, self._updated = rows, datetime.now()
        self.main.render()
        self._push_tray()
        self._schedule_next()

    def _schedule_next(self):
        """주기 갱신 예약. 꺼져 있으면(자동 갱신 OFF) 예약하지 않는다."""
        if self._after_id is not None:
            self.root.after_cancel(self._after_id)
            self._after_id = None
        if self.cfg["auto"]:
            self._after_id = self.root.after(self.cfg["interval_min"] * 60 * 1000, self.refresh)

    def _pump(self):
        """다른 스레드(조회·표시줄·로그 감시)와 시그널에서 온 요청을 Tk 스레드에서 처리한다."""
        try:
            if self._want_quit:
                self._quit()
                return
            if self._want_show:
                self._want_show = False
                self._show()
            while True:
                try:
                    msg = self._inbox.get_nowait()
                except queue.Empty:
                    break
                if isinstance(msg, tuple):
                    self._on_rows(msg[1])
                elif msg == "show":
                    self._show()
                elif msg == "toggle":
                    self._toggle()
                elif msg == "refresh":
                    self.refresh()
                elif msg == "settings":
                    self._open_settings()
                elif msg == "activity":
                    self._activity_seen()
                elif msg == "quit":
                    self._quit()
                    return
        finally:
            if not self._quitting:
                self.root.after(100, self._pump)

    # ---- 질문하면 그때 갱신 ----
    # Claude Code / Codex 는 대화 내용을 세션 로그(JSONL)에 계속 덧붙인다. 그 파일들의
    # 수정시각만 훑어보면(=토큰 0) "방금 뭔가 주고받았다"를 알 수 있다. 답변 도중에도
    # 계속 쓰이므로, 잠잠해진 뒤에 한 번만 갱신한다.
    def _start_activity_watch(self):
        roots = [claude_home() / "projects", codex_home() / "sessions"]

        def latest():
            m = 0.0
            for r in roots:
                for dirpath, _dirs, files in os.walk(r):
                    for f in files:
                        if f.endswith(".jsonl"):
                            try:
                                m = max(m, os.stat(os.path.join(dirpath, f)).st_mtime)
                            except OSError:
                                continue
            return m

        def loop():
            seen = latest()
            while True:
                time.sleep(ACTIVITY_POLL_SEC)
                if not self.cfg["on_activity"]:
                    seen = latest()          # 꺼진 동안의 변화는 무시(켜자마자 몰아치지 않게)
                    continue
                try:
                    now = latest()
                except Exception:            # noqa: BLE001
                    continue
                if now > seen:
                    seen = now
                    self._inbox.put("activity")

        threading.Thread(target=loop, daemon=True).start()

    def _activity_seen(self):
        """변화가 감지될 때마다 호출 → 조용해질 때까지 갱신을 미룬다(디바운스)."""
        if self._act_id is not None:
            self.root.after_cancel(self._act_id)
        self._act_id = self.root.after(ACTIVITY_QUIET_SEC * 1000, self._activity_refresh)

    def _activity_refresh(self):
        self._act_id = None
        if time.time() - self._last_fetch < ACTIVITY_COOLDOWN_SEC:
            return                           # 방금 확인했으면 건너뜀
        self.refresh()

    # ---- 상단 표시줄 ----
    def _push_tray(self):
        if not self.tray:
            return
        self.tray.show_items(panel_items(self._rows, self.cfg["panel_window"]),
                             self._spin if self._busy else None)
        self.tray.set_menu(tray_lines(self._rows), self.main.visible())

    # ---- 창 ----
    # 창 상태는 세 가지 — 보임 / 최소화(독에 남음) / 감춤(상단 표시줄에만 남음).
    def _show(self, refresh=True):
        self.root.deiconify()
        self.root.lift()
        try:
            self.root.focus_force()
        except tk.TclError:
            pass
        self.main.render()
        if self.main.size != (0, 0):
            self.root.geometry(f"{self.main.size[0]}x{self.main.size[1]}")
        self._push_tray()
        if refresh and time.time() - self._last_fetch > 60:
            self.refresh()

    def _hide(self):
        self._persist()
        self.root.withdraw()
        self._menu, self.main.hover = None, None
        self._push_tray()

    def _toggle(self):
        # 최소화된 상태에서 누르면 감추지 말고 다시 띄워준다.
        if self.main.visible():
            self._hide()
        else:
            self._show()

    def _close_window(self):
        # 표시줄 아이콘이 있으면 창만 감춘다. 없으면 감추면 다시 열 방법이 없으니 종료한다.
        if self.tray is not None and self.tray.ok():
            self._hide()
        else:
            self._quit()

    def _quit(self):
        self._quitting = True
        self._persist()
        if self.tray:
            self.tray.close()
            self.tray = None
        release_single_instance()
        self.root.destroy()

    def run(self):
        self.root.mainloop()


def main():
    args = sys.argv[1:]
    _add_pylib()
    if "--tray" in args:
        run_tray_process()
        return
    if "--uninstall" in args:
        uninstall()
        return
    if "--version" in args:
        print(f"{APP_NAME} {VERSION}")
        return
    background = "--background" in args
    if self_install(args):          # 다운로드한 파일을 실행 → 설치본으로 넘기고 끝
        return
    take_single_instance(background)
    ensure_deps()
    App(background).run()


if __name__ == "__main__":
    main()
