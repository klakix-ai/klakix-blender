# SPDX-License-Identifier: GPL-3.0-or-later
"""Main-thread executor: runs commands from the bridge, handles undo/rollback and pictures."""

import base64
import os
import tempfile
import time
import traceback

import bpy

from . import ops

# ---------------------------------------------------------------------------- pictures


def _shrink_to_b64(path, max_size):
    """Loads an image file, scales it to fit max_size, returns (base64, mime)."""
    img = bpy.data.images.load(path, check_existing=False)
    try:
        w, h = img.size[:]
        if not w or not h:
            raise RuntimeError("Empty image")
        f = min(1.0, float(max_size) / max(w, h))
        if f < 1.0:
            img.scale(max(1, int(w * f)), max(1, int(h * f)))
        out = os.path.join(tempfile.gettempdir(), "klakix_bridge", "snap_%d.jpg" % int(time.time() * 1000))
        os.makedirs(os.path.dirname(out), exist_ok=True)
        img.filepath_raw = out
        img.file_format = "JPEG"
        img.save()
        with open(out, "rb") as fh:
            data = fh.read()
        try:
            os.remove(out)
        except OSError:
            pass
        return base64.b64encode(data).decode("ascii"), "image/jpeg"
    finally:
        bpy.data.images.remove(img)


def _tmp_png():
    d = os.path.join(tempfile.gettempdir(), "klakix_bridge")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, "cap_%d.png" % int(time.time() * 1000))


def capture(kind="viewport", max_size=960):
    """Returns ({veri, tur}, info). kind: viewport | camera | render."""
    max_size = max(256, min(int(max_size or 960), 1600))
    path = _tmp_png()
    sc = bpy.context.scene
    info = {"kind": kind}
    try:
        if kind == "viewport":
            ctx = ops.window_context("VIEW_3D")
            if bpy.app.background or not ctx or "area" not in ctx:
                if sc.camera:
                    # No window (e.g. blender -b): the camera view is the closest picture we can give
                    img, cinfo = capture("camera", max_size)
                    cinfo["requested"] = "viewport"
                    cinfo["note"] = "No 3D viewport window, showing the camera view instead."
                    return img, cinfo
                raise ops.UserError("No 3D viewport is open (Blender may be running without a window) and the scene has no camera. Add one with add_camera, then use kind=camera.")
            # Draw the viewport's own view off-screen: no menus, dialogs, splash or other UI on top
            r = sc.render
            old_pct = r.resolution_percentage
            r.resolution_percentage = max(1, min(100, int(100 * max_size / max(r.resolution_x, r.resolution_y))))
            try:
                with bpy.context.temp_override(**ctx):
                    bpy.ops.render.opengl(write_still=False, view_context=True)
                bpy.data.images["Render Result"].save_render(filepath=path)
                info["method"] = "viewport-render"
            except Exception:
                # Fallback: screenshot of the viewport area (may include UI on top of it)
                with bpy.context.temp_override(**ctx):
                    if not bpy.ops.screen.screenshot_area.poll():
                        raise ops.UserError("The 3D viewport can't be captured right now (a menu or dialog may be open). Use kind=camera or render.")
                    bpy.ops.screen.screenshot_area(filepath=path)
                info["method"] = "screenshot"
            finally:
                r.resolution_percentage = old_pct
        elif kind == "camera":
            if not sc.camera:
                raise ops.UserError("The scene has no active camera. Add one with add_camera.")
            ctx = ops.window_context("VIEW_3D")
            r = sc.render
            old = (r.resolution_percentage, r.filepath)
            r.resolution_percentage = max(1, min(100, int(100 * max_size / max(r.resolution_x, r.resolution_y))))
            try:
                if not bpy.app.background and ctx and "area" in ctx:
                    with bpy.context.temp_override(**ctx):
                        bpy.ops.render.opengl(write_still=False, view_context=False)
                    info["method"] = "viewport"
                else:
                    # No OpenGL context (no window): a Workbench render gives the same framing quickly
                    engine = r.engine
                    r.engine = "BLENDER_WORKBENCH"
                    try:
                        bpy.ops.render.render(write_still=False)
                    finally:
                        r.engine = engine
                    info["method"] = "workbench"
                bpy.data.images["Render Result"].save_render(filepath=path)
            finally:
                r.resolution_percentage, r.filepath = old
            info["camera"] = sc.camera.name
        elif kind == "render":
            if not sc.camera:
                raise ops.UserError("The scene has no active camera. Add one with add_camera.")
            r = sc.render
            saved = {"pct": r.resolution_percentage}
            r.resolution_percentage = max(1, min(100, int(100 * max_size / max(r.resolution_x, r.resolution_y))))
            if r.engine == "CYCLES":
                saved["cycles"] = sc.cycles.samples
                sc.cycles.samples = min(sc.cycles.samples, 32)
            elif hasattr(sc, "eevee"):
                saved["eevee"] = sc.eevee.taa_render_samples
                sc.eevee.taa_render_samples = min(sc.eevee.taa_render_samples, 16)
            t0 = time.time()
            try:
                bpy.ops.render.render(write_still=False)
                bpy.data.images["Render Result"].save_render(filepath=path)
            finally:
                r.resolution_percentage = saved["pct"]
                if "cycles" in saved:
                    sc.cycles.samples = saved["cycles"]
                if "eevee" in saved:
                    sc.eevee.taa_render_samples = saved["eevee"]
            info.update({"engine": r.engine, "seconds": round(time.time() - t0, 2), "camera": sc.camera.name})
        else:
            raise ops.UserError("kind must be viewport, camera or render.")
        veri, tur = _shrink_to_b64(path, max_size)
        return {"veri": veri, "tur": tur}, info
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


# ---------------------------------------------------------------------------- undo


def _undo_push(message):
    try:
        ctx = ops.window_context()
        if ctx:
            with bpy.context.temp_override(**ctx):
                bpy.ops.ed.undo_push(message=message)
            return True
        bpy.ops.ed.undo_push(message=message)
        return True
    except Exception:
        return False


def _undo():
    ctx = ops.window_context()
    if not ctx:
        return False
    try:
        with bpy.context.temp_override(**ctx):
            if bpy.ops.ed.undo.poll():
                bpy.ops.ed.undo()
                return True
    except Exception:
        pass
    return False


# ---------------------------------------------------------------------------- execute


def _error(e, op_name=None):
    if isinstance(e, ops.UserError):
        return {"kod": "invalid_input", "mesaj": str(e)}
    if isinstance(e, (TypeError, ValueError, KeyError)) and _from_args(e):
        return {"kod": "invalid_input", "mesaj": "%s: %s" % (e.__class__.__name__, e)}
    return {"kod": "op_failed", "mesaj": "%s: %s" % (e.__class__.__name__, e), "yigin": traceback.format_exc()[-8000:], "beklenmeyen": True}


def _from_args(e):
    # float("abc"), int(None)… raised directly inside an operation body: the agent sent a bad value
    tb = traceback.extract_tb(e.__traceback__)
    return bool(tb) and tb[-1].filename.endswith("ops.py") and ("float" in str(e) or "int" in str(e) or "invalid literal" in str(e))


def run_command(cmd, allow_python, log):
    """Executes one command dict from the server, returns the result payload."""
    body = cmd.get("govde") or {}
    kind = body.get("tur")
    if kind == "goruntu":
        g = body.get("goruntu") or {}
        try:
            img, info = capture(g.get("tur") or "viewport", g.get("boyut") or 960)
            log("Captured %s" % (g.get("tur") or "viewport"))
            return {"tamam": True, "sonuc": info, "goruntu": img}
        except Exception as e:
            return {"tamam": False, "hata": _error(e)}
    if kind != "calistir":
        return {"tamam": False, "hata": {"kod": "unknown_command", "mesaj": "Unknown command type: %s" % kind}}

    steps = body.get("islemler") or []
    atomic = body.get("atomik", True)
    results = []
    failed = None
    names = [s.get("islem") for s in steps]
    has_writes = any(not ops.REGISTRY.get(n, {}).get("read_only", False) for n in names)
    pushed = _undo_push("Klakix: before AI edit") if has_writes else False

    for step in steps:
        name = step.get("islem")
        args = step.get("args") or {}
        spec = ops.REGISTRY.get(name)
        if spec is None:
            failed = {"kod": "unknown_operation", "mesaj": "Unknown operation: %s" % name}
        elif spec["requires"] == "allow_python" and not allow_python:
            failed = {"kod": "not_allowed", "mesaj": "run_python is turned off. The user can enable 'Allow Python' in the Klakix panel."}
        if failed:
            results.append({"islem": name, "tamam": False, "hata": failed})
            break
        try:
            res = spec["fn"](args)
            results.append({"islem": name, "tamam": True, "sonuc": res})
        except Exception as e:
            failed = _error(e, name)
            results.append({"islem": name, "tamam": False, "hata": failed})
            break

    rolled_back = False
    if failed and atomic and has_writes and pushed:
        # Back to the state saved before the batch (partial changes of the failing step included)
        _undo_push("Klakix: failed step")
        rolled_back = _undo()
    elif has_writes and not failed:
        label = ", ".join(n for n in names if n)[:60]
        _undo_push("Klakix: " + label)
    if has_writes:
        try:
            bpy.context.view_layer.update()
        except Exception:
            pass

    payload = {"tamam": failed is None, "sonuc": {"sonuclar": results, "geri_alindi": rolled_back}}
    if failed:
        payload["hata"] = {"kod": failed["kod"], "mesaj": failed["mesaj"]}

    g = body.get("goruntu")
    if g:
        try:
            img, info = capture(g.get("tur") or "viewport", g.get("boyut") or 960)
            payload["goruntu"] = img
        except Exception as e:
            payload["sonuc"]["goruntu_hatasi"] = str(e)[:300]
    log(("Ran " if not failed else "Failed: ") + ", ".join(n for n in names if n)[:80])
    return payload
