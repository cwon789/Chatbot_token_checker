"""상단 표시줄(트레이) 아이콘 — D-Bus StatusNotifierItem 을 순수 파이썬(jeepney)으로 구현.

gi/AppIndicator 같은 시스템 패키지에 기대지 않으므로 PyInstaller 실행파일 하나에 다 담긴다.
GNOME(AppIndicator 확장)·KDE·XFCE 등 SNI 를 지원하는 표시줄이면 어디서든 뜬다.

- 아이콘: IconName 에 PNG 절대경로를 넣는다(libappindicator 와 같은 방식 — 가로로 긴 그림도 그대로 보인다).
- 메뉴: com.canonical.dbusmenu 로 노출한다.
- 표시줄(GNOME Shell)이 재시작되면 감시자(watcher)가 새로 뜨므로 그때 다시 등록한다.

콜백(on_activate/on_secondary/메뉴 클릭)은 D-Bus 수신 스레드에서 불린다. UI 스레드로 넘기는 건 호출자 몫.
"""
import os
import threading

from jeepney import (DBusAddress, HeaderFields, MatchRule, MessageFlag, MessageType,
                     message_bus, new_error, new_method_call, new_method_return, new_signal)
from jeepney.io.blocking import open_dbus_connection

ITEM_PATH = "/StatusNotifierItem"
MENU_PATH = "/MenuBar"
SNI_IFACE = "org.kde.StatusNotifierItem"
MENU_IFACE = "com.canonical.dbusmenu"
PROPS_IFACE = "org.freedesktop.DBus.Properties"
WATCHER = "org.kde.StatusNotifierWatcher"

_INTROSPECT = {
    ITEM_PATH: f"""<node><interface name="{SNI_IFACE}">
 <property name="Category" type="s" access="read"/><property name="Id" type="s" access="read"/>
 <property name="Title" type="s" access="read"/><property name="Status" type="s" access="read"/>
 <property name="WindowId" type="i" access="read"/><property name="IconName" type="s" access="read"/>
 <property name="IconThemePath" type="s" access="read"/><property name="Menu" type="o" access="read"/>
 <property name="ItemIsMenu" type="b" access="read"/>
 <property name="IconPixmap" type="a(iiay)" access="read"/>
 <property name="ToolTip" type="(sa(iiay)ss)" access="read"/>
 <method name="Activate"><arg name="x" type="i" direction="in"/><arg name="y" type="i" direction="in"/></method>
 <method name="SecondaryActivate"><arg name="x" type="i" direction="in"/><arg name="y" type="i" direction="in"/></method>
 <method name="ContextMenu"><arg name="x" type="i" direction="in"/><arg name="y" type="i" direction="in"/></method>
 <method name="Scroll"><arg name="delta" type="i" direction="in"/><arg name="orientation" type="s" direction="in"/></method>
 <signal name="NewIcon"/><signal name="NewTitle"/><signal name="NewToolTip"/>
 <signal name="NewStatus"><arg name="status" type="s"/></signal>
</interface></node>""",
    MENU_PATH: f"""<node><interface name="{MENU_IFACE}">
 <property name="Version" type="u" access="read"/><property name="Status" type="s" access="read"/>
 <property name="TextDirection" type="s" access="read"/>
 <method name="GetLayout"><arg type="i" direction="in"/><arg type="i" direction="in"/><arg type="as" direction="in"/>
  <arg type="u" direction="out"/><arg type="(ia{{sv}}av)" direction="out"/></method>
 <method name="GetGroupProperties"><arg type="ai" direction="in"/><arg type="as" direction="in"/>
  <arg type="a(ia{{sv}})" direction="out"/></method>
 <method name="GetProperty"><arg type="i" direction="in"/><arg type="s" direction="in"/><arg type="v" direction="out"/></method>
 <method name="Event"><arg type="i" direction="in"/><arg type="s" direction="in"/><arg type="v" direction="in"/><arg type="u" direction="in"/></method>
 <method name="EventGroup"><arg type="a(isvu)" direction="in"/><arg type="ai" direction="out"/></method>
 <method name="AboutToShow"><arg type="i" direction="in"/><arg type="b" direction="out"/></method>
 <method name="AboutToShowGroup"><arg type="ai" direction="in"/><arg type="ai" direction="out"/><arg type="ai" direction="out"/></method>
 <signal name="ItemsPropertiesUpdated"><arg type="a(ia{{sv}})"/><arg type="a(ias)"/></signal>
 <signal name="LayoutUpdated"><arg type="u"/><arg type="i"/></signal>
</interface></node>""",
}


class SniTray:
    """표시줄 아이콘 하나. set_icon / set_menu 는 어느 스레드에서 불러도 된다."""

    def __init__(self, app_id, title, icon="", on_activate=None, on_secondary=None):
        self._conn = open_dbus_connection("SESSION")       # 세션 버스가 없으면 여기서 예외
        self._send_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._id, self._title = app_id, title
        self._icon = icon                 # 등록 전에 넣어 둬야 표시줄이 빈 아이콘을 잠깐 보이지 않는다
        self._menu = []                   # [(id, 속성dict, 콜백)]
        self._rev = 1
        self._on_activate, self._on_secondary = on_activate, on_secondary
        self._pending = {}                # 응답 기다리는 호출: serial → 이름
        self._registered = threading.Event()
        self._failed = threading.Event()
        self._closed = False

        rule = MatchRule(type="signal", sender="org.freedesktop.DBus",
                         interface="org.freedesktop.DBus", member="NameOwnerChanged")
        rule.add_arg_condition(0, WATCHER)
        self._send(message_bus.AddMatch(rule))
        threading.Thread(target=self._loop, name="sni-tray", daemon=True).start()
        self._register()

    # ---- 공개 API ----
    def wait_registered(self, timeout):
        """표시줄에 등록됐으면 True. 표시줄(watcher)이 없으면 곧바로 False."""
        for _ in range(int(timeout * 20)):
            if self._registered.is_set():
                return True
            if self._failed.is_set():
                return False
            self._registered.wait(0.05)
        return self._registered.is_set()

    def set_icon(self, path):
        with self._state_lock:
            if path == self._icon:
                return
            self._icon = path
        self._signal(ITEM_PATH, SNI_IFACE, "NewIcon")

    def set_title(self, title):
        with self._state_lock:
            if title == self._title:
                return
            self._title = title
        self._signal(ITEM_PATH, SNI_IFACE, "NewTitle")
        self._signal(ITEM_PATH, SNI_IFACE, "NewToolTip")

    def set_menu(self, entries):
        """entries: [(라벨, 콜백 또는 None)] · 라벨이 None 이면 구분선 · 콜백이 None 이면 회색(누를 수 없음)."""
        items = []
        for i, (label, cb) in enumerate(entries, start=1):
            if label is None:
                props = {"type": ("s", "separator"), "visible": ("b", True)}
            else:
                props = {"label": ("s", label.replace("_", "__")),   # '_' 는 단축키 표시라 이스케이프
                         "enabled": ("b", cb is not None), "visible": ("b", True)}
            items.append((i, props, cb))
        with self._state_lock:
            if [(i, p) for i, p, _ in items] == [(i, p) for i, p, _ in self._menu]:
                self._menu = items            # 콜백만 바뀐 경우: 신호 없이 교체
                return
            self._menu = items
            self._rev += 1
            rev = self._rev
        self._signal(MENU_PATH, MENU_IFACE, "LayoutUpdated", "ui", (rev, 0))

    def close(self):
        self._closed = True
        try:
            self._conn.close()                # 연결이 끊기면 표시줄이 알아서 아이콘을 치운다
        except OSError:
            pass

    # ---- 내부 ----
    def _send(self, msg):
        with self._send_lock:
            serial = next(self._conn.outgoing_serial)
            self._conn.send(msg, serial=serial)
        return serial

    def _signal(self, path, iface, name, sig=None, body=()):
        try:
            self._send(new_signal(DBusAddress(path, interface=iface), name, sig, body))
        except OSError:
            pass

    def _register(self):
        self._registered.clear()
        self._failed.clear()
        call = new_method_call(DBusAddress("/StatusNotifierWatcher", bus_name=WATCHER,
                                           interface=WATCHER),
                               "RegisterStatusNotifierItem", "s", (ITEM_PATH,))
        self._pending[self._send(call)] = "register"

    def _loop(self):
        while not self._closed:
            try:
                msg = self._conn.receive()
            except Exception:                               # noqa: BLE001  연결 끊김
                self._failed.set()
                return
            try:
                self._dispatch(msg)
            except Exception as e:                          # noqa: BLE001
                if msg.header.message_type == MessageType.method_call:
                    self._reply_error(msg, "org.freedesktop.DBus.Error.Failed", str(e))

    def _dispatch(self, msg):
        h = msg.header
        t = h.message_type
        if t in (MessageType.method_return, MessageType.error):
            what = self._pending.pop(h.fields.get(HeaderFields.reply_serial), None)
            if what == "register":
                (self._registered if t == MessageType.method_return else self._failed).set()
            return
        if t == MessageType.signal:
            if h.fields.get(HeaderFields.member) == "NameOwnerChanged":
                name, _old, new = msg.body
                if name == WATCHER and new:               # 표시줄이 다시 떴다 → 재등록
                    self._register()
            return
        if t != MessageType.method_call:
            return

        path = h.fields.get(HeaderFields.path)
        iface = h.fields.get(HeaderFields.interface)
        member = h.fields.get(HeaderFields.member)
        args = msg.body

        if iface == "org.freedesktop.DBus.Introspectable" or member == "Introspect":
            xml = _INTROSPECT.get(path, "<node/>")
            return self._reply(msg, "s", (xml,))
        if iface == "org.freedesktop.DBus.Peer":
            return self._reply(msg)
        if iface == PROPS_IFACE:
            props = self._props(path)
            if member == "GetAll":
                return self._reply(msg, "a{sv}", (props,))
            if member == "Get" and args[1] in props:
                return self._reply(msg, "v", (props[args[1]],))
            return self._reply_error(msg, "org.freedesktop.DBus.Error.UnknownProperty", str(args))

        if path == ITEM_PATH:
            if member == "Activate" and self._on_activate:
                self._on_activate()
            elif member == "SecondaryActivate" and self._on_secondary:
                self._on_secondary()
            return self._reply(msg)

        if path == MENU_PATH:
            with self._state_lock:
                menu, rev = list(self._menu), self._rev
            if member == "GetLayout":
                children = [("(ia{sv}av)", (i, p, [])) for i, p, _ in menu]
                root = (0, {"children-display": ("s", "submenu")}, children)
                if args[0] != 0:
                    root = next(((i, p, []) for i, p, _ in menu if i == args[0]), (args[0], {}, []))
                return self._reply(msg, "u(ia{sv}av)", (rev, root))
            if member == "GetGroupProperties":
                ids = set(args[0])
                return self._reply(msg, "a(ia{sv})",
                                   ([(i, p) for i, p, _ in menu if not ids or i in ids],))
            if member == "GetProperty":
                p = next((p for i, p, _ in menu if i == args[0]), {})
                if args[1] in p:
                    return self._reply(msg, "v", (p[args[1]],))
                return self._reply_error(msg, "org.freedesktop.DBus.Error.InvalidArgs", "no property")
            if member == "Event":
                if args[1] == "clicked":
                    self._click(menu, args[0])
                return self._reply(msg)
            if member == "EventGroup":
                for i, ev, _data, _ts in args[0]:
                    if ev == "clicked":
                        self._click(menu, i)
                return self._reply(msg, "ai", ([],))
            if member == "AboutToShow":
                return self._reply(msg, "b", (False,))
            if member == "AboutToShowGroup":
                return self._reply(msg, "aiai", ([], []))

        self._reply_error(msg, "org.freedesktop.DBus.Error.UnknownMethod", f"{iface}.{member}")

    @staticmethod
    def _click(menu, item_id):
        for i, _p, cb in menu:
            if i == item_id and cb:
                cb()

    def _props(self, path):
        with self._state_lock:
            icon, title = self._icon, self._title
        if path == ITEM_PATH:
            return {
                "Category": ("s", "ApplicationStatus"), "Id": ("s", self._id),
                "Title": ("s", title), "Status": ("s", "Active"), "WindowId": ("i", 0),
                "IconName": ("s", icon), "IconThemePath": ("s", os.path.dirname(icon)),
                "IconPixmap": ("a(iiay)", []), "OverlayIconName": ("s", ""),
                "OverlayIconPixmap": ("a(iiay)", []), "AttentionIconName": ("s", ""),
                "AttentionIconPixmap": ("a(iiay)", []), "AttentionMovieName": ("s", ""),
                "ToolTip": ("(sa(iiay)ss)", ("", [], title, "")),
                "ItemIsMenu": ("b", True), "Menu": ("o", MENU_PATH),
            }
        if path == MENU_PATH:
            return {"Version": ("u", 3), "Status": ("s", "normal"),
                    "TextDirection": ("s", "ltr"), "IconThemePath": ("as", [])}
        return {}

    def _reply(self, call, sig=None, body=()):
        if call.header.flags & MessageFlag.no_reply_expected:
            return
        try:
            self._send(new_method_return(call, sig, body))
        except OSError:
            pass

    def _reply_error(self, call, name, text):
        if call.header.flags & MessageFlag.no_reply_expected:
            return
        try:
            self._send(new_error(call, name, "s", (text,)))
        except OSError:
            pass

