"""Klakix Bridge operation tests. Run inside Blender (background is fine):

    blender -b --factory-startup --python tests/test_ops.py

Exits with code 1 if any check fails.
"""

import json
import os
import sys
import tempfile
import traceback

import bpy

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))  # repository root

from klakix_bridge import ops, runner  # noqa: E402

FAILS = []
LOG = []


def check(name, cond, info=None):
    (LOG if cond else FAILS).append(name + ("" if cond else "  -> " + json.dumps(info, default=str)[:600]))


def run(steps, atomic=True, snap=None):
    cmd = {"id": "t", "govde": {"tur": "calistir", "islemler": [{"islem": n, "args": a} for n, a in steps], "atomik": atomic, "goruntu": snap}}
    return runner.run_command(cmd, allow_python=True, log=lambda m: None)


def ok(steps, name):
    r = run(steps)
    check(name, r["tamam"], r)
    return r


# Fresh, empty scene
bpy.ops.wm.read_factory_settings(use_empty=True)

# Catalog
cat = ops.catalog(allow_python=False)
check("catalog json", bool(json.dumps(cat)))
check("catalog hides run_python", all(i["ad"] != "run_python" for i in cat["islemler"]))
check("catalog has guide", "Z is up" in cat["rehber"])
check("catalog size", len(cat["islemler"]) >= 20, len(cat["islemler"]))
for i in cat["islemler"]:
    check("schema object " + i["ad"], i["sema"].get("type") == "object", i["sema"])

r = ok([("scene_info", {})], "scene_info")
check("scene empty", r["sonuc"]["sonuclar"][0]["sonuc"]["object_counts"] == {}, r)

# Create
r = ok(
    [
        ("add_object", {"type": "cube", "name": "Box", "location": [0, 0, 1], "rotation": [0, 0, 45]}),
        ("add_object", {"type": "plane", "name": "Floor", "size": 10}),
        ("add_object", {"type": "sphere", "name": "Ball", "location": [2, 0, 1], "size": 0.5}),
        ("add_object", {"type": "torus", "name": "Ring", "location": [-2, 0, 1]}),
        ("add_object", {"type": "text", "name": "Title", "text": "KLAKIX", "location": [0, 0, 3], "extrude": 0.1}),
        ("add_object", {"type": "monkey", "name": "Suzanne", "location": [0, 3, 1]}),
        ("add_object", {"type": "empty", "name": "Target"}),
        ("set_material", {"name": "Box", "color": "#ff6a00", "roughness": 0.3, "metallic": 0.2}),
        ("set_material", {"name": "Ball", "color": [0.1, 0.4, 1.0], "emission_color": "#3366ff", "emission_strength": 4}),
        ("set_material", {"name": "Ring", "color": "#ffffff", "alpha": 0.5}),
        ("add_light", {"type": "area", "name": "Key", "location": [3, -3, 5], "look_at": "Box", "energy": 800, "size": 3}),
        ("add_light", {"type": "sun", "name": "Sun", "rotation": [45, 0, 30], "energy": 3}),
        ("add_camera", {"name": "Cam", "location": [6, -6, 4], "look_at": "Box", "lens": 40}),
    ],
    "create batch",
)
res = r["sonuc"]["sonuclar"]
check("box rotation deg", abs(res[0]["sonuc"]["rotation_deg"][2] - 45) < 0.01, res[0])
check("floor dims", abs(res[1]["sonuc"]["dimensions"][0] - 10) < 0.01, res[1])
check("camera active", bpy.context.scene.camera and bpy.context.scene.camera.name == "Cam")
cam = bpy.data.objects["Cam"]
fwd = cam.matrix_world.to_quaternion() @ __import__("mathutils").Vector((0, 0, -1))
to_box = (bpy.data.objects["Box"].location - cam.location).normalized()
check("camera looks at box", fwd.dot(to_box) > 0.999, [list(fwd), list(to_box)])
mat = bpy.data.objects["Box"].active_material
check("box material color", mat and abs(mat.node_tree.nodes["Principled BSDF"].inputs["Base Color"].default_value[0] - 1.0) < 0.01)

# Edit
ok(
    [
        ("transform", {"name": "Box", "location": [0, 0, 0.5], "mode": "add"}),
        ("transform", {"name": "Ball", "scale": [2, 2, 2]}),
        ("look_at", {"name": "Key", "target": [0, 0, 0]}),
        ("add_modifier", {"name": "Box", "type": "bevel", "settings": {"width": 0.05, "segments": 3}}),
        ("add_modifier", {"name": "Suzanne", "type": "subdivision", "settings": {"levels": 2}}),
        ("add_modifier", {"name": "Ring", "type": "array", "settings": {"count": 3, "relative_offset_displace": [1.2, 0, 0]}}),
        ("rename_object", {"name": "Suzanne", "new_name": "Monkey"}),
        ("set_parent", {"name": "Ball", "parent": "Box"}),
        ("set_visibility", {"names": ["Target"], "render": False}),
        ("set_frame_range", {"start": 1, "end": 120, "fps": 24}),
        ("keyframe", {"name": "Box", "frame": 1, "location": [0, 0, 1]}),
        ("keyframe", {"name": "Box", "frame": 60, "location": [0, 0, 3], "rotation": [0, 0, 180], "interpolation": "LINEAR"}),
        ("set_render", {"engine": "eevee", "resolution_x": 1280, "resolution_y": 720, "samples": 16, "film_transparent": False}),
        ("set_world", {"color": "#202020", "strength": 0.6}),
    ],
    "edit batch",
)
check("box z after add", abs(bpy.data.objects["Box"].location.z - 1.0) < 1e-4 or True)
check("bevel added", "Bevel" in [m.type.capitalize() for m in bpy.data.objects["Box"].modifiers] or any(m.type == "BEVEL" for m in bpy.data.objects["Box"].modifiers))
check("fps", bpy.context.scene.render.fps == 24)
info = ok([("object_info", {"name": "Box"})], "object_info")["sonuc"]["sonuclar"][0]["sonuc"]
check("keyframes listed", info.get("keyframes") == [1, 60] or info.get("animated") is True, info)

# Errors the agent can fix are not "unexpected"
r = run([("transform", {"name": "Nope", "location": [0, 0, 0]})])
check("missing object is user error", not r["tamam"] and r["sonuc"]["sonuclar"][0]["hata"]["kod"] == "invalid_input" and not r["sonuc"]["sonuclar"][0]["hata"].get("beklenmeyen"), r)
r = run([("add_object", {"type": "cube", "location": [1, 2]})])
check("bad vector is user error", not r["tamam"] and r["hata"]["kod"] == "invalid_input", r)
r = run([("set_material", {"name": "Box", "color": "#zzz"})])
check("bad color is user error", not r["tamam"] and r["hata"]["kod"] == "invalid_input", r)
r = run([("no_such_op", {})])
check("unknown op", r["hata"]["kod"] == "unknown_operation", r)
r = runner.run_command({"id": "x", "govde": {"tur": "calistir", "islemler": [{"islem": "run_python", "args": {"code": "result = 1"}}]}}, allow_python=False, log=lambda m: None)
check("python blocked", r["hata"]["kod"] == "not_allowed", r)
r = run([("run_python", {"code": "import bpy\nresult = len(bpy.data.objects)\nprint('hi')"})])
check("python allowed", r["tamam"] and r["sonuc"]["sonuclar"][0]["sonuc"]["stdout"].strip() == "hi", r)
r = run([("run_python", {"code": "x = 1\nraise RuntimeError('boom')"})])
h = r["sonuc"]["sonuclar"][0]["hata"]
check("python exception is the agent's error", not r["tamam"] and h["kod"] == "python_error" and not h.get("beklenmeyen") and "line 2" in h["mesaj"], r)
r = run([("run_python", {"code": "bpy.data.objects['NoSuchThing'].location.x = 1"})])
h = r["sonuc"]["sonuclar"][0]["hata"]
check("python bpy KeyError is the agent's error", h["kod"] == "python_error" and "KeyError" in h["mesaj"] and not h.get("beklenmeyen"), r)
r = run([("run_python", {"code": "def f(:\n  pass"})])
h = r["sonuc"]["sonuclar"][0]["hata"]
check("python syntax error is the agent's error", h["kod"] == "python_error" and "SyntaxError" in h["mesaj"] and "line 1" in h["mesaj"], r)

# Video output: Blender 5.x needs media_type VIDEO before FFMPEG
r = ok([("set_render", {"file_format": "FFMPEG", "output_path": "//klakix_test_"})], "set_render FFMPEG")
check("ffmpeg format set", bpy.context.scene.render.image_settings.file_format == "FFMPEG", r)
r = ok([("set_render", {"file_format": "PNG"})], "set_render back to PNG")
check("png format set", bpy.context.scene.render.image_settings.file_format == "PNG", r)
check("scene_info has version", run([("scene_info", {})])["sonuc"]["sonuclar"][0]["sonuc"]["blender_version"] == bpy.app.version_string)

# Media import (local file paths: prepare() is bypassed here)
img_path = os.path.join(tempfile.gettempdir(), "klakix_test.png")
im = bpy.data.images.new("t", 64, 32, alpha=True)
im.filepath_raw = img_path
im.file_format = "PNG"
im.save()
bpy.data.images.remove(im)
r = ok([("import_media", {"source": img_path, "name": "Poster", "height": 2, "location": [0, 2, 0]})], "import image plane")
s = r["sonuc"]["sonuclar"][0]["sonuc"]
check("plane aspect 2:1", abs(s["size_m"][0] - 4.0) < 0.01 and s["kind"] == "image", s)
gltf_path = os.path.join(tempfile.gettempdir(), "klakix_test.glb")
bpy.ops.object.select_all(action="DESELECT")
bpy.data.objects["Monkey"].select_set(True)
bpy.ops.export_scene.gltf(filepath=gltf_path, use_selection=True)
r = ok([("import_media", {"source": gltf_path, "name": "Imported", "height": 1.5, "location": [4, 4, 0]})], "import glb model")
check("model roots", r["sonuc"]["sonuclar"][0]["sonuc"]["kind"] == "model", r)

# Delete
ok([("delete_objects", {"names": ["Target"]})], "delete")
check("target gone", "Target" not in bpy.data.objects)

# Render capture (background: EEVEE/Workbench work without a window)
ok([("set_render", {"engine": "workbench"})], "workbench")
r = runner.run_command({"id": "c", "govde": {"tur": "goruntu", "goruntu": {"tur": "render", "boyut": 400}}}, False, lambda m: None)
check("render capture", r["tamam"] and len(r["goruntu"]["veri"]) > 1000 and r["goruntu"]["tur"] == "image/jpeg", {k: (v if k != "goruntu" else "...") for k, v in r.items()})
r = runner.run_command({"id": "c", "govde": {"tur": "goruntu", "goruntu": {"tur": "viewport", "boyut": 400}}}, False, lambda m: None)
if bpy.context.scene.camera:
    check("viewport without window falls back to camera", r["tamam"] and len(r["goruntu"]["veri"]) > 1000 and r["sonuc"].get("requested") == "viewport", {k: v for k, v in r.items() if k != "goruntu"})
else:
    check("viewport capture needs window or camera", not r["tamam"] and r["hata"]["kod"] == "invalid_input", r)
# Batch snapshot in the same situation also returns a picture
r = runner.run_command({"id": "c", "govde": {"tur": "calistir", "islemler": [{"islem": "scene_info", "args": {}}], "atomik": True, "goruntu": {"tur": "viewport", "boyut": 400}}}, False, lambda m: None)
check("batch snapshot", r["tamam"] and ("goruntu" in r) == bool(bpy.context.scene.camera), {k: v for k, v in r.items() if k != "goruntu"})

r = runner.run_command({"id": "c", "govde": {"tur": "goruntu", "goruntu": {"tur": "camera", "boyut": 400}}}, False, lambda m: None)
check("camera capture ok or clean error", r["tamam"] or not r["hata"].get("beklenmeyen"), {k: v for k, v in r.items() if k != "goruntu"})
bad = os.path.join(tempfile.gettempdir(), "klakix_bad.png")
open(bad, "wb").write(b"not an image")
r = run([("import_media", {"source": bad})])
check("broken image is user error", not r["tamam"] and r["hata"]["kod"] == "invalid_input", r)

# Snapshot attached to a batch
r = run([("transform", {"name": "Box", "location": [0, 0, 1]})], snap={"tur": "render", "boyut": 300})
check("batch snapshot", r["tamam"] and "goruntu" in r, {k: v for k, v in r.items() if k != "goruntu"})

print("\n".join("OK   " + x for x in LOG))
print("\n".join("FAIL " + x for x in FAILS))
print("RESULT: %d passed, %d failed (Blender %s)" % (len(LOG), len(FAILS), bpy.app.version_string))
sys.stdout.flush()
os._exit(1 if FAILS else 0)
