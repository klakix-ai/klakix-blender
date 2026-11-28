# SPDX-License-Identifier: GPL-3.0-or-later
"""Klakix Bridge: lets AI agents (Claude, ChatGPT, Cursor…) connected to Klakix MCP work in the open Blender scene.

How it works: the add-on links this Blender to your Klakix account (approve a code on the website once),
then keeps a long-poll connection to klakix.com. When your agent calls run_in_app / capture_app_view,
the command arrives here and runs on Blender's main thread. Nothing listens on a local port.

Docs: https://klakix.com/blender-mcp
"""

bl_info = {
    "name": "Klakix Bridge",
    "author": "Klakix",
    "version": (1, 0, 2),
    "blender": (4, 2, 0),
    "location": "3D Viewport > Sidebar (N) > Klakix",
    "description": "Let Claude, ChatGPT and other AI agents work in your open Blender scene through Klakix MCP",
    "doc_url": "https://klakix.com/blender-mcp",
    "category": "Interface",
}

import json
import os
import platform
import socket
import threading
import time
import traceback

import bpy

from . import net, ops, runner

ADDON_VERSION = "1.0.2"
_bridge = None
_log = []
_info_cache = {}
_catalog_cache = {}


# ---------------------------------------------------------------------------- config (token lives outside .blend files)


class Config:
    def __init__(self):
        try:
            base = bpy.utils.extension_path_user(__package__, create=True)
        except Exception:
            base = bpy.utils.user_resource("CONFIG", path="klakix_bridge", create=True)
        self.path = os.path.join(base, "config.json")
        self._lock = threading.Lock()
        self.data = {}
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                self.data = json.load(f)
        except Exception:
            self.data = {}
        env = os.environ.get("KLAKIX_BRIDGE_URL")
        if env:
            self.data["server_url"] = env.rstrip("/")

    def get(self, k, default=None):
        return self.data.get(k, default)

    def update(self, values):
        with self._lock:
            self.data.update(values)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.data, f)
            os.replace(tmp, self.path)


def log(msg):
    _log.insert(0, time.strftime("%H:%M:%S ") + msg)
    del _log[8:]


# ---------------------------------------------------------------------------- main-thread caches read by the network thread


def _refresh_info():
    try:
        _info_cache.update(
            {
                "device_name": "Blender %s · %s" % (bpy.app.version_string.split(" ")[0], socket.gethostname()[:30]),
                "app_version": bpy.app.version_string,
                "addon_version": ADDON_VERSION,
                "os": "%s %s" % (platform.system(), platform.release()),
                "document": {
                    "file": os.path.basename(bpy.data.filepath) or "(unsaved)",
                    "saved": not bpy.data.is_dirty,
                    "scene": bpy.context.scene.name if bpy.context.scene else None,
                    "objects": len(bpy.context.scene.objects) if bpy.context.scene else 0,
                },
            }
        )
    except Exception:
        pass


def _allow_python():
    return bool(_bridge and _bridge.config.get("allow_python"))


def _refresh_catalog():
    _catalog_cache.clear()
    _catalog_cache.update(ops.catalog(allow_python=_allow_python()))


def _prepare(cmd):
    """Worker thread: downloads for operations that declare `prepare`."""
    body = cmd.get("govde") or {}
    if body.get("tur") != "calistir":
        return cmd
    steps = []
    for step in body.get("islemler") or []:
        spec = ops.REGISTRY.get(step.get("islem"))
        if spec and spec.get("prepare"):
            step = dict(step, args=spec["prepare"](dict(step.get("args") or {}), None))
        steps.append(step)
    return dict(cmd, govde=dict(body, islemler=steps))


# ---------------------------------------------------------------------------- timer: run commands on the main thread


def _tick():
    if _bridge is None:
        return None
    try:
        if not _bridge._threads and (not hasattr(bpy.app, "online_access") or bpy.app.online_access):
            _bridge.start()  # online access was turned on after Blender started
        if not _info_cache or int(time.time()) % 5 == 0:
            _refresh_info()
        for cmd in _bridge.take_commands():
            try:
                payload = runner.run_command(cmd, _allow_python(), log)
            except Exception as e:  # runner itself failed: report as a bug
                payload = {"tamam": False, "hata": {"kod": "runner_failed", "mesaj": str(e), "yigin": traceback.format_exc(), "beklenmeyen": True}}
            _bridge.send_result(cmd["id"], payload)
        _redraw()
    except Exception as e:
        _bridge.report_error("tick", str(e), traceback.format_exc())
    return 0.1


_last_state = None


def _redraw():
    global _last_state
    st = (_bridge.state, _bridge.user_code, _bridge.status_text, tuple(_log[:1]))
    if st == _last_state:
        return
    _last_state = st
    for win in bpy.context.window_manager.windows:
        for area in win.screen.areas:
            if area.type == "VIEW_3D":
                area.tag_redraw()


# ---------------------------------------------------------------------------- operators


def _online_ok(self_op):
    if hasattr(bpy.app, "online_access") and not bpy.app.online_access:
        self_op.report({"ERROR"}, "Enable 'Allow Online Access' in Preferences > System > Network first.")
        return False
    return True


class KLAKIX_OT_connect(bpy.types.Operator):
    bl_idname = "klakix.connect"
    bl_label = "Connect to Klakix"
    bl_description = "Link this Blender to your Klakix account (you approve a code in the browser)"

    def execute(self, context):
        if not _online_ok(self):
            return {"CANCELLED"}
        _refresh_info()
        _bridge.begin_pairing(_info_cache.get("device_name"), _info_cache.get("app_version"), ADDON_VERSION, _info_cache.get("os"))
        return {"FINISHED"}


class KLAKIX_OT_open_browser(bpy.types.Operator):
    bl_idname = "klakix.open_browser"
    bl_label = "Open approval page"

    def execute(self, context):
        if _bridge and _bridge.verify_url:
            import webbrowser

            webbrowser.open(_bridge.verify_url)
        return {"FINISHED"}


class KLAKIX_OT_cancel(bpy.types.Operator):
    bl_idname = "klakix.cancel"
    bl_label = "Cancel"

    def execute(self, context):
        _bridge.cancel_pairing()
        return {"FINISHED"}


class KLAKIX_OT_disconnect(bpy.types.Operator):
    bl_idname = "klakix.disconnect"
    bl_label = "Disconnect"
    bl_description = "Unlink this Blender from your Klakix account"

    def invoke(self, context, event):
        return context.window_manager.invoke_confirm(self, event)

    def execute(self, context):
        _bridge.disconnect()
        log("Disconnected")
        return {"FINISHED"}


class KLAKIX_OT_pause(bpy.types.Operator):
    bl_idname = "klakix.pause"
    bl_label = "Pause / resume"
    bl_description = "Stop receiving agent commands until you resume"

    def execute(self, context):
        paused = not _bridge.config.get("paused")
        _bridge.config.update({"paused": paused})
        if not paused:
            _bridge.request_hello()
        log("Paused" if paused else "Resumed")
        return {"FINISHED"}


class KLAKIX_OT_toggle_python(bpy.types.Operator):
    bl_idname = "klakix.toggle_python"
    bl_label = "Allow Python"
    bl_description = "Let the agent run arbitrary Python in Blender (off by default). Only enable for agents you trust"

    def invoke(self, context, event):
        if not _bridge.config.get("allow_python"):
            return context.window_manager.invoke_confirm(self, event)
        return self.execute(context)

    def execute(self, context):
        on = not _bridge.config.get("allow_python")
        _bridge.config.update({"allow_python": on})
        _refresh_catalog()
        _bridge.request_hello()
        log("Python allowed" if on else "Python blocked")
        return {"FINISHED"}


# ---------------------------------------------------------------------------- panel

STATE_LABEL = {
    "idle": ("Not connected", "UNLINKED"),
    "pairing": ("Waiting for approval", "TIME"),
    "connecting": ("Connecting…", "SORTTIME"),
    "online": ("Connected", "CHECKMARK"),
    "paused": ("Paused", "PAUSE"),
    "error": ("Problem", "ERROR"),
}


class KLAKIX_PT_panel(bpy.types.Panel):
    bl_label = "Klakix Bridge"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Klakix"

    def draw(self, context):
        lay = self.layout
        b = _bridge
        if b is None:
            lay.label(text="Starting…")
            return
        label, icon = STATE_LABEL.get(b.state, (b.state, "INFO"))
        box = lay.box()
        box.label(text=label, icon=icon)
        if b.status_text and b.status_text != label:
            for line in _wrap(b.status_text, 38):
                box.label(text=line)
        if hasattr(bpy.app, "online_access") and not bpy.app.online_access:
            box.label(text="Online access is off.", icon="ERROR")
            box.label(text="Preferences > System > Network")
        if b.state == "pairing" and b.user_code:
            col = lay.column(align=True)
            col.label(text="Your code:")
            row = col.row()
            row.scale_y = 1.6
            row.label(text=b.user_code)
            col.operator("klakix.open_browser", icon="URL")
            col.operator("klakix.cancel", icon="X")
        elif not b.token:
            row = lay.row()
            row.scale_y = 1.4
            row.operator("klakix.connect", icon="LINKED")
        else:
            acct = b.account or b.config.get("account")
            if acct:
                lay.label(text=acct, icon="USER")
            row = lay.row(align=True)
            row.operator("klakix.pause", text="Resume" if b.config.get("paused") else "Pause", icon="PLAY" if b.config.get("paused") else "PAUSE")
            row.operator("klakix.disconnect", icon="UNLINKED")
            lay.operator("klakix.toggle_python", text="Python: allowed" if b.config.get("allow_python") else "Python: blocked", icon="CONSOLE", depress=bool(b.config.get("allow_python")))
            if b.latest_version and _newer(b.latest_version, ADDON_VERSION):
                lay.label(text="Update available: " + b.latest_version, icon="IMPORT")
            lay.label(text="In your AI agent, ask:", icon="QUESTION")
            lay.label(text='"Use Klakix to build a scene in Blender"')
        if _log:
            col = lay.column(align=True)
            col.label(text="Activity", icon="TEXT")
            for line in _log[:5]:
                col.label(text=line[:46])
        lay.operator("wm.url_open", text="Help", icon="HELP").url = "https://klakix.com/blender-mcp"


def _wrap(text, n):
    words, line, out = text.split(), "", []
    for w in words:
        if len(line) + len(w) + 1 > n:
            out.append(line)
            line = w
        else:
            line = (line + " " + w).strip()
    if line:
        out.append(line)
    return out[:4]


def _newer(a, b):
    try:
        return tuple(int(x) for x in a.split(".")) > tuple(int(x) for x in b.split("."))
    except Exception:
        return False


# ---------------------------------------------------------------------------- register

CLASSES = (KLAKIX_OT_connect, KLAKIX_OT_open_browser, KLAKIX_OT_cancel, KLAKIX_OT_disconnect, KLAKIX_OT_pause, KLAKIX_OT_toggle_python, KLAKIX_PT_panel)


def register():
    global _bridge
    for c in CLASSES:
        bpy.utils.register_class(c)
    cfg = Config()
    _bridge = net.Bridge(cfg, lambda: dict(_catalog_cache), _prepare, lambda: dict(_info_cache), log)
    _refresh_catalog()
    _refresh_info()
    _bridge.account = cfg.get("account")
    if not hasattr(bpy.app, "online_access") or bpy.app.online_access:
        _bridge.start()
    bpy.app.timers.register(_tick, first_interval=0.5, persistent=True)


def unregister():
    global _bridge
    if bpy.app.timers.is_registered(_tick):
        bpy.app.timers.unregister(_tick)
    if _bridge:
        _bridge.stop()
        _bridge = None
    for c in reversed(CLASSES):
        bpy.utils.unregister_class(c)
