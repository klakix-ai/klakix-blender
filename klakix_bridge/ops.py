# SPDX-License-Identifier: GPL-3.0-or-later
"""Operation catalog: what the AI agent can do in the open Blender scene.

Every operation is a function `fn(args) -> result` that runs on Blender's main thread.
Raise `UserError` for problems the agent can fix (bad name, bad value); any other exception
is treated as a bug in the add-on and reported automatically.

Operations that need files from the internet declare `prepare(args, ctx) -> args`; it runs in
a worker thread before the command reaches the main thread (downloads never freeze Blender).
"""

import math
import os
import tempfile

import bpy
from mathutils import Euler, Vector

from . import net

CATALOG_VERSION = "1"
REGISTRY = {}


class UserError(Exception):
    """A problem the agent can fix by changing its arguments."""


def op(name, description, schema=None, read_only=False, destructive=False, prepare=None, requires=None):
    def wrap(fn):
        REGISTRY[name] = {
            "fn": fn,
            "description": description,
            "schema": schema or {"type": "object", "properties": {}},
            "read_only": read_only,
            "destructive": destructive,
            "prepare": prepare,
            "requires": requires,
        }
        return fn

    return wrap


GUIDE = """Klakix Bridge for Blender: working guide
- Units are meters, Z is up, the default camera looks along -Y. Rotations in arguments are in DEGREES (XYZ Euler).
- Start with scene_info (and list_objects) so you know what is already there; never assume an empty scene.
- Work in small batches (3-10 operations) with snapshot=true and look at the picture before continuing.
- A batch is ONE undo step for the user, and with atomic=true a failing step rolls the batch back.
- Object names are unique; operations return the final name (Blender may add .001). Use returned names.
- To build a shot: add_camera with look_at, add_light (an area light ~ 500-1000 W or a sun 3-5), set_render, then capture_app_view kind=camera or render.
- To use media generated with Klakix tools (generate_image / generate_video): import_media with source="gen:<generation id>". Images/videos become textured planes sized by `height`; .glb/.gltf/.obj/.fbx/.usd become models; .hdr/.exr become the world light.
- Free CC0 assets: polyhaven_search then polyhaven_apply (HDRIs light the scene, models are imported, textures become PBR materials on an object).
- add_object type=text lies flat on the floor (XY plane) like in Blender; to stand it up facing the camera use rotation [90, 0, angle toward the camera]. Its location is the bottom center of the text, so z=0 stands on the floor.
- A floor plane should be large (size 60+) so its edge doesn't show in wide shots; emission strength 2-4 already glows, 10+ blows out to white.
- Materials: set_material with a hex color (#RRGGBB) or [r,g,b] 0-1, metallic/roughness 0-1, emission for glowing parts.
- Animation: set_frame_range, then keyframe at several frames (location/rotation/scale); interpolation can be BEZIER, LINEAR or CONSTANT.
- save_file only when the user asks. Ask before delete_objects on things you did not create.
- run_python exists only if the user enabled it in the add-on panel; prefer the operations above.
"""

# ---------------------------------------------------------------------------- helpers

NUM3 = {"type": "array", "items": {"type": "number"}, "minItems": 3, "maxItems": 3}
COLOR = {
    "description": "Hex color like #ff8800 or [r, g, b] (0-1).",
    "oneOf": [{"type": "string"}, {"type": "array", "items": {"type": "number"}, "minItems": 3, "maxItems": 4}],
}


def vec3(v, name, default=None):
    if v is None:
        return default
    if isinstance(v, (int, float)):
        return Vector((float(v), float(v), float(v)))
    try:
        vals = [float(x) for x in v]
    except Exception:
        raise UserError("%s must be a list of 3 numbers." % name)
    if len(vals) != 3:
        raise UserError("%s must be a list of 3 numbers." % name)
    return Vector(vals)


def euler_deg(v, name="rotation"):
    r = vec3(v, name)
    return None if r is None else Euler([math.radians(x) for x in r], "XYZ")


def color4(v, alpha=1.0):
    if v is None:
        return None
    if isinstance(v, str):
        h = v.strip().lstrip("#")
        if len(h) == 3:
            h = "".join(c * 2 for c in h)
        if len(h) not in (6, 8):
            raise UserError("Color must be #RRGGBB.")
        try:
            vals = [int(h[i : i + 2], 16) / 255.0 for i in range(0, len(h), 2)]
        except ValueError:
            raise UserError("Color must be #RRGGBB.")
        rgb = [srgb_to_linear(x) for x in vals[:3]]
        return (*rgb, vals[3] if len(vals) == 4 else alpha)
    try:
        vals = [float(x) for x in v]
    except Exception:
        raise UserError("Color must be #RRGGBB or [r, g, b].")
    if len(vals) == 3:
        vals.append(alpha)
    if len(vals) != 4:
        raise UserError("Color must be #RRGGBB or [r, g, b].")
    return tuple(vals)


def srgb_to_linear(c):
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def obj_get(name):
    if not isinstance(name, str) or not name:
        raise UserError("name is required.")
    o = bpy.data.objects.get(name)
    if o is None:
        lowered = {x.name.lower(): x for x in bpy.data.objects}
        o = lowered.get(name.lower())
    if o is None:
        close = [x.name for x in bpy.data.objects if name.lower() in x.name.lower()][:5]
        raise UserError("No object named '%s'.%s" % (name, (" Similar: " + ", ".join(close)) if close else ""))
    return o


def scene():
    return bpy.context.scene


def link(obj, collection_name=None):
    col = None
    if collection_name:
        col = bpy.data.collections.get(collection_name)
        if col is None:
            col = bpy.data.collections.new(collection_name)
            scene().collection.children.link(col)
    (col or scene().collection).objects.link(obj)


def r3(v):
    return [round(float(x), 4) for x in v]


def obj_summary(o):
    d = {
        "name": o.name,
        "type": o.type,
        "location": r3(o.location),
        "rotation_deg": [round(math.degrees(a), 2) for a in o.rotation_euler],
        "scale": r3(o.scale),
        "dimensions": r3(o.dimensions),
        "visible": not o.hide_get() if o.name in bpy.context.view_layer.objects else False,
    }
    if o.parent:
        d["parent"] = o.parent.name
    mats = [s.material.name for s in getattr(o, "material_slots", []) if s.material]
    if mats:
        d["materials"] = mats
    if o.type == "LIGHT":
        d["light"] = {"type": o.data.type, "energy": round(o.data.energy, 2)}
    if o.type == "CAMERA":
        d["camera"] = {"lens": round(o.data.lens, 2), "active": scene().camera == o}
    return d


def look_at_rotation(src, target):
    direction = Vector(target) - Vector(src)
    if direction.length < 1e-6:
        return None
    return direction.to_track_quat("-Z", "Y").to_euler()


def engine_id(name):
    items = {i.identifier for i in bpy.types.RenderSettings.bl_rna.properties["engine"].enum_items}
    n = (name or "").upper()
    if n in ("EEVEE", "BLENDER_EEVEE", "BLENDER_EEVEE_NEXT"):
        for cand in ("BLENDER_EEVEE_NEXT", "BLENDER_EEVEE"):
            if cand in items:
                return cand
    if n in ("CYCLES",):
        return "CYCLES"
    if n in ("WORKBENCH", "BLENDER_WORKBENCH"):
        return "BLENDER_WORKBENCH"
    if n in items:
        return n
    raise UserError("engine must be eevee, cycles or workbench.")


def principled(mat):
    mat.use_nodes = True
    nt = mat.node_tree
    p = next((n for n in nt.nodes if n.type == "BSDF_PRINCIPLED"), None)
    if p is None:
        p = nt.nodes.new("ShaderNodeBsdfPrincipled")
        out = next((n for n in nt.nodes if n.type == "OUTPUT_MATERIAL"), None) or nt.nodes.new("ShaderNodeOutputMaterial")
        nt.links.new(p.outputs[0], out.inputs[0])
    return p


def set_input(node, names, value):
    for n in names:
        s = node.inputs.get(n)
        if s is not None:
            s.default_value = value
            return True
    return False


def cache_dir():
    d = os.path.join(tempfile.gettempdir(), "klakix_bridge")
    os.makedirs(d, exist_ok=True)
    return d


def window_context(area_type="VIEW_3D"):
    """A context override dict with a window/area/region, or None in background mode."""
    wm = bpy.context.window_manager
    for win in getattr(wm, "windows", []):
        for area in win.screen.areas:
            if area.type == area_type:
                region = next((r for r in area.regions if r.type == "WINDOW"), None)
                return {"window": win, "screen": win.screen, "area": area, "region": region}
    for win in getattr(wm, "windows", []):
        return {"window": win, "screen": win.screen}
    return None


# ---------------------------------------------------------------------------- inspect


@op("scene_info", "Scene overview: file, frame range, fps, render settings, active camera, selection, collections and object counts by type.", read_only=True)
def scene_info(args):
    sc = scene()
    r = sc.render
    counts = {}
    for o in sc.objects:
        counts[o.type] = counts.get(o.type, 0) + 1
    return {
        "file": bpy.data.filepath or None,
        "saved": not bpy.data.is_dirty,
        "scene": sc.name,
        "frame_start": sc.frame_start,
        "frame_end": sc.frame_end,
        "frame_current": sc.frame_current,
        "fps": round(r.fps / r.fps_base, 3),
        "render": {"engine": r.engine, "resolution": [r.resolution_x, r.resolution_y], "percentage": r.resolution_percentage, "film_transparent": r.film_transparent},
        "camera": sc.camera.name if sc.camera else None,
        "selected": [o.name for o in bpy.context.selected_objects][:30] if hasattr(bpy.context, "selected_objects") else [],
        "active": bpy.context.view_layer.objects.active.name if bpy.context.view_layer.objects.active else None,
        "object_counts": counts,
        "collections": [c.name for c in sc.collection.children_recursive][:50],
        "unit_scale": sc.unit_settings.scale_length,
        "world": sc.world.name if sc.world else None,
    }


@op(
    "list_objects",
    "List objects with type, transform, dimensions and materials.",
    {
        "type": "object",
        "properties": {
            "type": {"type": "string", "description": "MESH, LIGHT, CAMERA, EMPTY, CURVE, FONT… (optional)"},
            "name_contains": {"type": "string"},
            "limit": {"type": "integer", "default": 100, "maximum": 300},
        },
    },
    read_only=True,
)
def list_objects(args):
    t = (args.get("type") or "").upper()
    q = (args.get("name_contains") or "").lower()
    limit = max(1, min(int(args.get("limit") or 100), 300))
    out = []
    total = 0
    for o in scene().objects:
        if t and o.type != t:
            continue
        if q and q not in o.name.lower():
            continue
        total += 1
        if len(out) < limit:
            out.append(obj_summary(o))
    return {"objects": out, "total": total}


@op("object_info", "Details of one object: transform, modifiers, materials, mesh stats, animation.", {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}, read_only=True)
def object_info(args):
    o = obj_get(args.get("name"))
    d = obj_summary(o)
    d["modifiers"] = [{"name": m.name, "type": m.type} for m in o.modifiers]
    if o.type == "MESH":
        d["mesh"] = {"vertices": len(o.data.vertices), "faces": len(o.data.polygons)}
    if o.animation_data and o.animation_data.action:
        fc = getattr(o.animation_data.action, "fcurves", None)
        if fc is not None:
            d["keyframes"] = sorted({int(k.co[0]) for f in fc for k in f.keyframe_points})[:100]
        else:
            d["animated"] = True
    d["children"] = [c.name for c in o.children][:50]
    if o.type == "FONT":
        d["text"] = o.data.body
    return d


# ---------------------------------------------------------------------------- create / edit

PRIMS = ["cube", "sphere", "ico_sphere", "plane", "cylinder", "cone", "torus", "monkey", "empty", "text", "circle"]


@op(
    "add_object",
    "Add a primitive (cube, sphere, ico_sphere, plane, cylinder, cone, torus, monkey, circle), an empty or a 3D text.",
    {
        "type": "object",
        "properties": {
            "type": {"type": "string", "enum": PRIMS},
            "name": {"type": "string"},
            "location": NUM3,
            "rotation": {**NUM3, "description": "Degrees"},
            "scale": NUM3,
            "size": {"type": "number", "description": "Base size in meters (default 2 for cube/plane, radius 1 for round shapes)."},
            "text": {"type": "string", "description": "Body for type=text"},
            "extrude": {"type": "number", "description": "Text depth"},
            "collection": {"type": "string"},
            "smooth": {"type": "boolean"},
        },
        "required": ["type"],
    },
)
def add_object(args):
    kind = (args.get("type") or "").lower()
    if kind not in PRIMS:
        raise UserError("type must be one of: " + ", ".join(PRIMS))
    size = float(args.get("size") or 0) or None
    loc = vec3(args.get("location"), "location", Vector((0, 0, 0)))
    if kind == "empty":
        o = bpy.data.objects.new(args.get("name") or "Empty", None)
        o.empty_display_type = "PLAIN_AXES"
        o.empty_display_size = size or 1
        link(o, args.get("collection"))
    elif kind == "text":
        cu = bpy.data.curves.new(args.get("name") or "Text", "FONT")
        cu.body = str(args.get("text") or "Text")[:2000]
        cu.align_x = "CENTER"
        cu.align_y = "BOTTOM"  # location = bottom center: z=0 sits on the floor
        if size:
            cu.size = size
        if args.get("extrude") is not None:
            cu.extrude = float(args["extrude"])
        o = bpy.data.objects.new(args.get("name") or "Text", cu)
        link(o, args.get("collection"))
    else:
        import bmesh

        me = bpy.data.meshes.new(args.get("name") or kind.capitalize())
        bm = bmesh.new()
        try:
            s = size or (2.0 if kind in ("cube", "plane") else 1.0)
            if kind == "cube":
                bmesh.ops.create_cube(bm, size=s)
            elif kind == "plane":
                bmesh.ops.create_grid(bm, x_segments=1, y_segments=1, size=s / 2)
            elif kind == "sphere":
                bmesh.ops.create_uvsphere(bm, u_segments=48, v_segments=24, radius=s)
            elif kind == "ico_sphere":
                bmesh.ops.create_icosphere(bm, subdivisions=3, radius=s)
            elif kind == "cylinder":
                bmesh.ops.create_cone(bm, cap_ends=True, segments=48, radius1=s, radius2=s, depth=s * 2)
            elif kind == "cone":
                bmesh.ops.create_cone(bm, cap_ends=True, segments=48, radius1=s, radius2=0, depth=s * 2)
            elif kind == "circle":
                bmesh.ops.create_circle(bm, cap_ends=True, segments=48, radius=s)
            elif kind == "monkey":
                bmesh.ops.create_monkey(bm)
                if size:
                    bmesh.ops.scale(bm, vec=(s, s, s), verts=bm.verts)
            elif kind == "torus":
                _torus(bm, s, s * 0.25)
            uv = bm.loops.layers.uv.verify()
            if kind in ("plane", "cube"):
                for f in bm.faces:
                    for lp in f.loops:
                        co = lp.vert.co
                        lp[uv].uv = ((co.x / s) + 0.5, (co.y / s) + 0.5)
            bm.to_mesh(me)
        finally:
            bm.free()
        if args.get("smooth") or (args.get("smooth") is None and kind in ("sphere", "cylinder", "cone", "torus", "monkey")):
            for p in me.polygons:
                p.use_smooth = True
        o = bpy.data.objects.new(me.name, me)
        link(o, args.get("collection"))
    o.location = loc
    rot = euler_deg(args.get("rotation"))
    if rot:
        o.rotation_euler = rot
    sc = vec3(args.get("scale"), "scale")
    if sc:
        o.scale = sc
    _select_only(o)
    return obj_summary(o)


def _torus(bm, major, minor, seg=48, ring=16):
    verts = []
    for i in range(seg):
        a = 2 * math.pi * i / seg
        row = []
        for j in range(ring):
            b = 2 * math.pi * j / ring
            x = (major + minor * math.cos(b)) * math.cos(a)
            y = (major + minor * math.cos(b)) * math.sin(a)
            z = minor * math.sin(b)
            row.append(bm.verts.new((x, y, z)))
        verts.append(row)
    for i in range(seg):
        for j in range(ring):
            bm.faces.new((verts[i][j], verts[(i + 1) % seg][j], verts[(i + 1) % seg][(j + 1) % ring], verts[i][(j + 1) % ring]))


def _select_only(o):
    try:
        for x in bpy.context.selected_objects:
            x.select_set(False)
        o.select_set(True)
        bpy.context.view_layer.objects.active = o
    except Exception:
        pass


@op(
    "transform",
    "Move, rotate or scale an object. mode=set (absolute) or add (relative).",
    {"type": "object", "properties": {"name": {"type": "string"}, "location": NUM3, "rotation": {**NUM3, "description": "Degrees"}, "scale": NUM3, "mode": {"type": "string", "enum": ["set", "add"], "default": "set"}}, "required": ["name"]},
)
def transform(args):
    o = obj_get(args.get("name"))
    add = args.get("mode") == "add"
    loc = vec3(args.get("location"), "location")
    if loc is not None:
        o.location = (o.location + loc) if add else loc
    rot = vec3(args.get("rotation"), "rotation")
    if rot is not None:
        rr = Euler([math.radians(x) for x in rot], "XYZ")
        if add:
            o.rotation_euler = Euler([a + b for a, b in zip(o.rotation_euler, rr)], "XYZ")
        else:
            o.rotation_euler = rr
    sc = vec3(args.get("scale"), "scale")
    if sc is not None:
        o.scale = Vector([a * b for a, b in zip(o.scale, sc)]) if add else sc
    return obj_summary(o)


@op(
    "set_material",
    "Give an object a material (creates or edits a Principled BSDF material).",
    {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Object name"},
            "material_name": {"type": "string", "description": "Reuse or create a material with this name"},
            "color": COLOR,
            "metallic": {"type": "number"},
            "roughness": {"type": "number"},
            "emission_color": COLOR,
            "emission_strength": {"type": "number"},
            "alpha": {"type": "number"},
            "new": {"type": "boolean", "description": "Always create a new material instead of editing the object's current one"},
        },
        "required": ["name"],
    },
)
def set_material(args):
    o = obj_get(args.get("name"))
    if not hasattr(o.data, "materials"):
        raise UserError("'%s' (%s) can't have materials." % (o.name, o.type))
    mname = args.get("material_name")
    mat = bpy.data.materials.get(mname) if mname else None
    if mat is None and not args.get("new") and not mname and o.active_material:
        mat = o.active_material
    if mat is None:
        mat = bpy.data.materials.new(mname or (o.name + " Material"))
    p = principled(mat)
    c = color4(args.get("color"))
    if c:
        set_input(p, ["Base Color"], c)
        mat.diffuse_color = c
    if args.get("metallic") is not None:
        set_input(p, ["Metallic"], max(0.0, min(1.0, float(args["metallic"]))))
    if args.get("roughness") is not None:
        set_input(p, ["Roughness"], max(0.0, min(1.0, float(args["roughness"]))))
    ec = color4(args.get("emission_color"))
    if ec:
        set_input(p, ["Emission Color", "Emission"], ec)
        if args.get("emission_strength") is None:
            set_input(p, ["Emission Strength"], 1.0)
    if args.get("emission_strength") is not None:
        set_input(p, ["Emission Strength"], float(args["emission_strength"]))
    if args.get("alpha") is not None:
        a = max(0.0, min(1.0, float(args["alpha"])))
        set_input(p, ["Alpha"], a)
        if a < 1:
            try:
                mat.surface_render_method = "BLENDED"
            except Exception:
                try:
                    mat.blend_method = "BLEND"
                except Exception:
                    pass
    if o.active_material != mat:
        if o.material_slots:
            o.material_slots[o.active_material_index].material = mat
        else:
            o.data.materials.append(mat)
    return {"object": o.name, "material": mat.name}


@op(
    "add_light",
    "Add a light: point, sun, spot or area. energy is Watts (sun: strength).",
    {
        "type": "object",
        "properties": {
            "type": {"type": "string", "enum": ["point", "sun", "spot", "area"]},
            "name": {"type": "string"},
            "location": NUM3,
            "rotation": {**NUM3, "description": "Degrees"},
            "look_at": {"description": "Object name or [x,y,z] to aim the light at", "oneOf": [{"type": "string"}, NUM3]},
            "energy": {"type": "number"},
            "color": COLOR,
            "size": {"type": "number", "description": "Area size / soft shadow radius"},
            "spot_angle": {"type": "number", "description": "Degrees"},
        },
        "required": ["type"],
    },
)
def add_light(args):
    kind = (args.get("type") or "point").upper()
    if kind not in ("POINT", "SUN", "SPOT", "AREA"):
        raise UserError("type must be point, sun, spot or area.")
    ld = bpy.data.lights.new(args.get("name") or (kind.capitalize() + " Light"), kind)
    ld.energy = float(args.get("energy") if args.get("energy") is not None else {"POINT": 1000, "SUN": 4, "SPOT": 1000, "AREA": 500}[kind])
    c = color4(args.get("color"))
    if c:
        ld.color = c[:3]
    if args.get("size") is not None:
        if kind == "AREA":
            ld.size = float(args["size"])
        elif kind == "SUN":
            ld.angle = math.radians(float(args["size"]))
        else:
            ld.shadow_soft_size = float(args["size"])
    if kind == "SPOT" and args.get("spot_angle") is not None:
        ld.spot_size = math.radians(float(args["spot_angle"]))
    o = bpy.data.objects.new(ld.name, ld)
    link(o)
    o.location = vec3(args.get("location"), "location", Vector((4, -4, 6)))
    _aim(o, args)
    return obj_summary(o)


def _target_point(t):
    if isinstance(t, str):
        return obj_get(t).matrix_world.translation.copy()
    return vec3(t, "look_at")


def _aim(o, args):
    if args.get("look_at") is not None:
        bpy.context.view_layer.update()
        rot = look_at_rotation(o.location, _target_point(args["look_at"]))
        if rot:
            o.rotation_euler = rot
    elif args.get("rotation") is not None:
        o.rotation_euler = euler_deg(args.get("rotation"))


@op(
    "add_camera",
    "Add a camera, aim it with look_at (object name or point) and make it the active scene camera.",
    {
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "location": NUM3,
            "rotation": {**NUM3, "description": "Degrees (ignored when look_at is set)"},
            "look_at": {"oneOf": [{"type": "string"}, NUM3]},
            "lens": {"type": "number", "description": "Focal length mm (default 50)"},
            "set_active": {"type": "boolean", "default": True},
            "dof_target": {"type": "string", "description": "Focus on this object (depth of field)"},
            "fstop": {"type": "number"},
        },
    },
)
def add_camera(args):
    cd = bpy.data.cameras.new(args.get("name") or "Camera")
    cd.lens = float(args.get("lens") or 50)
    o = bpy.data.objects.new(cd.name, cd)
    link(o)
    o.location = vec3(args.get("location"), "location", Vector((7, -7, 5)))
    if args.get("look_at") is None and args.get("rotation") is None:
        args = dict(args, look_at=[0, 0, 0])
    _aim(o, args)
    if args.get("dof_target"):
        cd.dof.use_dof = True
        cd.dof.focus_object = obj_get(args["dof_target"])
        cd.dof.aperture_fstop = float(args.get("fstop") or 2.8)
    if args.get("set_active", True):
        scene().camera = o
    return obj_summary(o)


@op("look_at", "Rotate an object (camera, light…) so it points at a target object or point.", {"type": "object", "properties": {"name": {"type": "string"}, "target": {"oneOf": [{"type": "string"}, NUM3]}}, "required": ["name", "target"]})
def look_at(args):
    o = obj_get(args.get("name"))
    _aim(o, {"look_at": args.get("target")})
    return obj_summary(o)


@op(
    "keyframe",
    "Set and keyframe location/rotation/scale of an object at a frame.",
    {
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "frame": {"type": "integer"},
            "location": NUM3,
            "rotation": {**NUM3, "description": "Degrees"},
            "scale": NUM3,
            "interpolation": {"type": "string", "enum": ["BEZIER", "LINEAR", "CONSTANT"]},
        },
        "required": ["name", "frame"],
    },
)
def keyframe(args):
    o = obj_get(args.get("name"))
    try:
        frame = int(args.get("frame"))
    except Exception:
        raise UserError("frame must be an integer.")
    keyed = []
    loc = vec3(args.get("location"), "location")
    if loc is not None:
        o.location = loc
        o.keyframe_insert("location", frame=frame)
        keyed.append("location")
    rot = euler_deg(args.get("rotation"))
    if rot is not None:
        o.rotation_euler = rot
        o.keyframe_insert("rotation_euler", frame=frame)
        keyed.append("rotation")
    sc = vec3(args.get("scale"), "scale")
    if sc is not None:
        o.scale = sc
        o.keyframe_insert("scale", frame=frame)
        keyed.append("scale")
    if not keyed:
        o.keyframe_insert("location", frame=frame)
        keyed.append("location")
    interp = args.get("interpolation")
    if interp and o.animation_data and o.animation_data.action:
        for kp in _keyframe_points(o.animation_data.action, frame):
            kp.interpolation = interp
    return {"object": o.name, "frame": frame, "keyed": keyed}


def _keyframe_points(action, frame):
    curves = getattr(action, "fcurves", None)
    if curves is None:
        # Blender 5 layered actions
        curves = []
        for layer in getattr(action, "layers", []):
            for strip in layer.strips:
                for bag in getattr(strip, "channelbags", []):
                    curves.extend(bag.fcurves)
    for fc in curves:
        for kp in fc.keyframe_points:
            if int(round(kp.co[0])) == frame:
                yield kp


@op(
    "set_frame_range",
    "Set the animation frame range, fps and current frame.",
    {"type": "object", "properties": {"start": {"type": "integer"}, "end": {"type": "integer"}, "fps": {"type": "number"}, "current": {"type": "integer"}}},
)
def set_frame_range(args):
    sc = scene()
    if args.get("start") is not None:
        sc.frame_start = int(args["start"])
    if args.get("end") is not None:
        sc.frame_end = int(args["end"])
    if args.get("fps") is not None:
        fps = float(args["fps"])
        sc.render.fps = int(round(fps))
        sc.render.fps_base = sc.render.fps / fps if fps else 1
    if args.get("current") is not None:
        sc.frame_set(int(args["current"]))
    return {"frame_start": sc.frame_start, "frame_end": sc.frame_end, "fps": round(sc.render.fps / sc.render.fps_base, 3), "frame_current": sc.frame_current}


@op(
    "set_render",
    "Render settings: engine (eevee, cycles, workbench), resolution, samples, transparent background, output path/format.",
    {
        "type": "object",
        "properties": {
            "engine": {"type": "string", "enum": ["eevee", "cycles", "workbench"]},
            "resolution_x": {"type": "integer"},
            "resolution_y": {"type": "integer"},
            "percentage": {"type": "integer"},
            "samples": {"type": "integer"},
            "film_transparent": {"type": "boolean"},
            "output_path": {"type": "string"},
            "file_format": {"type": "string", "enum": ["PNG", "JPEG", "OPEN_EXR", "FFMPEG"]},
        },
    },
)
def set_render(args):
    sc = scene()
    r = sc.render
    if args.get("engine"):
        r.engine = engine_id(args["engine"])
    if args.get("resolution_x"):
        r.resolution_x = max(16, min(int(args["resolution_x"]), 16384))
    if args.get("resolution_y"):
        r.resolution_y = max(16, min(int(args["resolution_y"]), 16384))
    if args.get("percentage"):
        r.resolution_percentage = max(1, min(int(args["percentage"]), 100))
    if args.get("samples"):
        n = max(1, min(int(args["samples"]), 8192))
        if r.engine == "CYCLES":
            sc.cycles.samples = n
        elif hasattr(sc, "eevee"):
            sc.eevee.taa_render_samples = n
    if args.get("film_transparent") is not None:
        r.film_transparent = bool(args["film_transparent"])
    if args.get("output_path"):
        r.filepath = str(args["output_path"])
    if args.get("file_format"):
        r.image_settings.file_format = args["file_format"]
    return {"engine": r.engine, "resolution": [r.resolution_x, r.resolution_y], "percentage": r.resolution_percentage, "output": r.filepath}


def _world():
    sc = scene()
    w = sc.world
    if w is None:
        w = bpy.data.worlds.new("World")
        sc.world = w
    w.use_nodes = True
    return w


def _world_bg(w):
    nt = w.node_tree
    bg = next((n for n in nt.nodes if n.type == "BACKGROUND"), None)
    out = next((n for n in nt.nodes if n.type == "OUTPUT_WORLD"), None) or nt.nodes.new("ShaderNodeOutputWorld")
    if bg is None:
        bg = nt.nodes.new("ShaderNodeBackground")
        nt.links.new(bg.outputs[0], out.inputs[0])
    return bg


def _world_hdri(path, strength=1.0, rotation_deg=0.0):
    w = _world()
    nt = w.node_tree
    bg = _world_bg(w)
    env = next((n for n in nt.nodes if n.type == "TEX_ENVIRONMENT"), None) or nt.nodes.new("ShaderNodeTexEnvironment")
    env.image = bpy.data.images.load(path, check_existing=True)
    nt.links.new(env.outputs["Color"], bg.inputs["Color"])
    bg.inputs["Strength"].default_value = float(strength)
    if rotation_deg:
        mapping = next((n for n in nt.nodes if n.type == "MAPPING"), None) or nt.nodes.new("ShaderNodeMapping")
        coord = next((n for n in nt.nodes if n.type == "TEX_COORD"), None) or nt.nodes.new("ShaderNodeTexCoord")
        nt.links.new(coord.outputs["Generated"], mapping.inputs["Vector"])
        nt.links.new(mapping.outputs["Vector"], env.inputs["Vector"])
        mapping.inputs["Rotation"].default_value[2] = math.radians(float(rotation_deg))
    return {"world": w.name, "hdri": os.path.basename(path), "strength": float(strength)}


def _prepare_url(field):
    def prepare(args, ctx):
        url = args.get(field)
        if isinstance(url, str) and url.startswith(("http://", "https://")):
            args = dict(args)
            args["_local_path"] = net.download(url, cache_dir())
        return args

    return prepare


@op(
    "set_world",
    "Set the world background: a color + strength, or an HDRI from a URL (gen:<id> or https) for realistic light.",
    {"type": "object", "properties": {"color": COLOR, "strength": {"type": "number"}, "hdri": {"type": "string", "description": "https URL of a .hdr/.exr"}, "rotation": {"type": "number", "description": "HDRI Z rotation in degrees"}}},
    prepare=_prepare_url("hdri"),
)
def set_world(args):
    if args.get("_local_path"):
        return _world_hdri(args["_local_path"], args.get("strength") or 1.0, args.get("rotation") or 0)
    w = _world()
    bg = _world_bg(w)
    nt = w.node_tree
    for n in [n for n in nt.nodes if n.type == "TEX_ENVIRONMENT"]:
        nt.nodes.remove(n)
    c = color4(args.get("color"))
    if c:
        bg.inputs["Color"].default_value = c
    if args.get("strength") is not None:
        bg.inputs["Strength"].default_value = float(args["strength"])
    return {"world": w.name, "color": list(bg.inputs["Color"].default_value)[:3], "strength": bg.inputs["Strength"].default_value}


# ---------------------------------------------------------------------------- media import

IMAGE_EXT = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff", ".tga"}
VIDEO_EXT = {".mp4", ".mov", ".webm", ".mkv", ".avi", ".m4v"}
MODEL_EXT = {".glb", ".gltf", ".obj", ".fbx", ".stl", ".usd", ".usdz", ".usda", ".usdc", ".ply", ".abc", ".dae"}
HDRI_EXT = {".hdr", ".exr"}
AUDIO_EXT = {".mp3", ".wav", ".ogg", ".flac", ".m4a", ".aac"}


def _media_plane(path, name, height, emission, kind):
    try:
        img = bpy.data.images.load(path, check_existing=True)
    except RuntimeError as e:
        raise UserError("Blender couldn't open this file: %s" % e)
    w, h = img.size[:]
    if not w or not h:
        try:
            img.reload()
            w, h = img.size[:]
        except Exception:
            pass
    if (not w or not h) and kind == "image":
        bpy.data.images.remove(img)
        raise UserError("The image couldn't be read (damaged or unsupported file).")
    if not w or not h:
        w, h = 16, 9  # movie size unknown until the first frame is decoded
    aspect = (w / h) if h else 1.0
    hh = float(height or 2.0)
    ww = hh * aspect
    me = bpy.data.meshes.new(name)
    me.from_pydata([(-ww / 2, 0, 0), (ww / 2, 0, 0), (ww / 2, 0, hh), (-ww / 2, 0, hh)], [], [(0, 1, 2, 3)])
    uv = me.uv_layers.new(name="UVMap")
    for lp, co in zip(uv.data, [(0, 0), (1, 0), (1, 1), (0, 1)]):
        lp.uv = co
    me.update()
    o = bpy.data.objects.new(name, me)
    link(o)
    mat = bpy.data.materials.new(name)
    mat.use_nodes = True
    nt = mat.node_tree
    for n in list(nt.nodes):
        nt.nodes.remove(n)
    out = nt.nodes.new("ShaderNodeOutputMaterial")
    tex = nt.nodes.new("ShaderNodeTexImage")
    tex.image = img
    if kind == "video":
        tex.image_user.frame_duration = max(1, img.frame_duration)
        tex.image_user.use_auto_refresh = True
        tex.image_user.use_cyclic = True
    if emission:
        em = nt.nodes.new("ShaderNodeEmission")
        nt.links.new(tex.outputs["Color"], em.inputs["Color"])
        shader = em
    else:
        shader = nt.nodes.new("ShaderNodeBsdfPrincipled")
        nt.links.new(tex.outputs["Color"], shader.inputs["Base Color"])
        set_input(shader, ["Roughness"], 0.6)
    if img.depth == 32 or img.alpha_mode != "NONE":
        tr = nt.nodes.new("ShaderNodeBsdfTransparent")
        mix = nt.nodes.new("ShaderNodeMixShader")
        nt.links.new(tex.outputs["Alpha"], mix.inputs["Fac"])
        nt.links.new(tr.outputs[0], mix.inputs[1])
        nt.links.new(shader.outputs[0], mix.inputs[2])
        nt.links.new(mix.outputs[0], out.inputs["Surface"])
    else:
        nt.links.new(shader.outputs[0], out.inputs["Surface"])
    me.materials.append(mat)
    return o, {"width_px": w, "height_px": h, "size_m": [round(ww, 3), round(hh, 3)]}


def _import_model(path):
    ext = os.path.splitext(path)[1].lower()
    before = set(bpy.data.objects)
    ops = bpy.ops
    if ext in (".glb", ".gltf"):
        ops.import_scene.gltf(filepath=path)
    elif ext == ".obj":
        ops.wm.obj_import(filepath=path)
    elif ext == ".fbx":
        ops.import_scene.fbx(filepath=path)
    elif ext == ".stl":
        (ops.wm.stl_import if hasattr(ops.wm, "stl_import") else ops.import_mesh.stl)(filepath=path)
    elif ext in (".usd", ".usdz", ".usda", ".usdc"):
        ops.wm.usd_import(filepath=path)
    elif ext == ".ply":
        ops.wm.ply_import(filepath=path)
    elif ext == ".abc":
        ops.wm.alembic_import(filepath=path)
    elif ext == ".dae":
        raise UserError("Collada (.dae) is not supported in recent Blender versions; use .glb.")
    new = [o for o in bpy.data.objects if o not in before]
    if not new:
        raise UserError("The file was imported but contained no objects.")
    return new


@op(
    "import_media",
    "Import a file into the scene from a URL. Use source=gen:<generation id> for something you generated with Klakix. "
    "Images and videos become textured planes (standing upright, facing -Y, bottom at location); 3D files (.glb .gltf .obj .fbx .stl .usd .ply) are imported as models; .hdr/.exr light the world; audio is added to the Video Sequencer.",
    {
        "type": "object",
        "properties": {
            "source": {"type": "string", "description": "gen:<generation id>, an asset id, or an https URL"},
            "name": {"type": "string"},
            "location": NUM3,
            "rotation": {**NUM3, "description": "Degrees"},
            "height": {"type": "number", "description": "Plane height or model target height in meters"},
            "emission": {"type": "boolean", "description": "Images/videos: shadeless (unaffected by lights), default true"},
            "frame_start": {"type": "integer", "description": "Videos/audio: start frame"},
        },
        "required": ["source"],
    },
    prepare=_prepare_url("source"),
)
def import_media(args):
    path = args.get("_local_path") or args.get("source")
    if not path or not os.path.exists(path):
        raise UserError("source must be a gen:<id>, asset id or https URL.")
    ext = os.path.splitext(path)[1].lower()
    name = args.get("name") or os.path.splitext(os.path.basename(path))[0].split("_", 1)[-1][:60] or "Media"
    loc = vec3(args.get("location"), "location", Vector((0, 0, 0)))
    rot = euler_deg(args.get("rotation"))
    if ext in IMAGE_EXT or ext in VIDEO_EXT:
        kind = "video" if ext in VIDEO_EXT else "image"
        o, info = _media_plane(path, name, args.get("height"), args.get("emission", True), kind)
        o.location = loc
        if rot:
            o.rotation_euler = rot
        if kind == "video" and args.get("frame_start") is not None:
            for n in o.active_material.node_tree.nodes:
                if n.type == "TEX_IMAGE":
                    n.image_user.frame_start = int(args["frame_start"])
        _select_only(o)
        return {"kind": kind, "object": o.name, **info}
    if ext in HDRI_EXT:
        return {"kind": "hdri", **_world_hdri(path)}
    if ext in AUDIO_EXT:
        sc = scene()
        if not sc.sequence_editor:
            sc.sequence_editor_create()
        seqs = getattr(sc.sequence_editor, "strips", None) or sc.sequence_editor.sequences
        s = seqs.new_sound(name, path, channel=1, frame_start=int(args.get("frame_start") or sc.frame_start))
        return {"kind": "audio", "strip": s.name, "frames": [s.frame_final_start, s.frame_final_end]}
    if ext in MODEL_EXT:
        new = _import_model(path)
        roots = [o for o in new if o.parent is None or o.parent not in new]
        bpy.context.view_layer.update()
        if args.get("height"):
            zs = [(o.matrix_world @ Vector(c)).z for o in new if o.type == "MESH" for c in o.bound_box]
            if zs and max(zs) - min(zs) > 1e-6:
                f = float(args["height"]) / (max(zs) - min(zs))
                for r in roots:
                    r.scale = r.scale * f
        for r in roots:
            r.location = r.location + loc
            if rot:
                r.rotation_euler = rot
        if len(roots) == 1 and args.get("name"):
            roots[0].name = args["name"]
        return {"kind": "model", "objects": [o.name for o in new][:50], "roots": [o.name for o in roots]}
    raise UserError("Unsupported file type '%s'." % ext)


# ---------------------------------------------------------------------------- Poly Haven (CC0)

PH = "https://api.polyhaven.com"


@op(
    "polyhaven_search",
    "Search free CC0 assets on Poly Haven: hdris (lighting), models or textures. Returns asset ids for polyhaven_apply.",
    {"type": "object", "properties": {"type": {"type": "string", "enum": ["hdris", "models", "textures"]}, "query": {"type": "string"}, "limit": {"type": "integer", "default": 12}}, "required": ["type"]},
    read_only=True,
    prepare=lambda args, ctx: dict(args, _results=_ph_search(args)),
)
def polyhaven_search(args):
    return {"assets": args.get("_results") or []}


def _ph_search(args):
    t = args.get("type")
    if t not in ("hdris", "models", "textures"):
        raise ValueError("type must be hdris, models or textures.")
    data = net.get_json("%s/assets?t=%s" % (PH, t))
    q = (args.get("query") or "").lower().split()
    out = []
    for aid, a in data.items():
        hay = " ".join([aid, a.get("name", "")] + list(a.get("tags", [])) + list(a.get("categories", []))).lower()
        if all(w in hay for w in q):
            out.append({"id": aid, "name": a.get("name"), "categories": a.get("categories", [])[:5], "downloads": a.get("download_count", 0)})
    out.sort(key=lambda x: -x["downloads"])
    return out[: max(1, min(int(args.get("limit") or 12), 40))]


def _ph_prepare(args, ctx):
    t = args.get("type")
    aid = args.get("asset_id")
    res = args.get("resolution") or "2k"
    if not aid:
        raise ValueError("asset_id is required.")
    files = net.get_json("%s/files/%s" % (PH, aid))
    d = cache_dir()
    args = dict(args)
    if t == "hdris":
        entry = files.get("hdri", {}).get(res) or files.get("hdri", {}).get("1k")
        if not entry:
            raise ValueError("No HDRI files for %s." % aid)
        args["_local_path"] = net.download(entry["hdr"]["url"], d)
    elif t == "models":
        g = files.get("gltf", {}).get(res) or files.get("gltf", {}).get("1k")
        if not g:
            raise ValueError("No glTF files for %s." % aid)
        folder = os.path.join(d, "ph_" + aid + "_" + res)
        os.makedirs(folder, exist_ok=True)
        main = g["gltf"]
        main_path = os.path.join(folder, os.path.basename(main["url"]))
        if not os.path.exists(main_path):
            _dl_to(main["url"], main_path)
        for rel, inc in (main.get("include") or {}).items():
            p = os.path.join(folder, rel.replace("/", os.sep))
            os.makedirs(os.path.dirname(p), exist_ok=True)
            if not os.path.exists(p):
                _dl_to(inc["url"], p)
        args["_local_path"] = main_path
    elif t == "textures":
        maps = {}
        for key, names in (("diffuse", ("Diffuse", "diff")), ("roughness", ("Rough", "rough")), ("normal", ("nor_gl", "Normal")), ("displacement", ("Displacement", "disp"))):
            for n in names:
                e = files.get(n, {}).get(res) or files.get(n, {}).get("1k")
                if e:
                    fmt = e.get("jpg") or e.get("png")
                    if fmt:
                        maps[key] = net.download(fmt["url"], d)
                        break
        if not maps:
            raise ValueError("No texture maps for %s." % aid)
        args["_maps"] = maps
    else:
        raise ValueError("type must be hdris, models or textures.")
    return args


def _dl_to(url, path):
    tmp = net.download(url, os.path.dirname(path))
    os.replace(tmp, path)


@op(
    "polyhaven_apply",
    "Download a Poly Haven asset and use it: hdris light the world, models are imported, textures become a PBR material on target object.",
    {
        "type": "object",
        "properties": {
            "type": {"type": "string", "enum": ["hdris", "models", "textures"]},
            "asset_id": {"type": "string"},
            "resolution": {"type": "string", "enum": ["1k", "2k", "4k"], "default": "2k"},
            "target": {"type": "string", "description": "textures: object to apply the material to"},
            "location": NUM3,
            "strength": {"type": "number", "description": "hdris: light strength"},
            "scale": {"type": "number", "description": "textures: UV scale"},
        },
        "required": ["type", "asset_id"],
    },
    prepare=_ph_prepare,
)
def polyhaven_apply(args):
    t = args.get("type")
    if t == "hdris":
        return _world_hdri(args["_local_path"], args.get("strength") or 1.0)
    if t == "models":
        new = _import_model(args["_local_path"])
        loc = vec3(args.get("location"), "location")
        roots = [o for o in new if o.parent is None or o.parent not in new]
        if loc is not None:
            for r in roots:
                r.location = loc
        return {"objects": [o.name for o in new][:50], "roots": [o.name for o in roots]}
    o = obj_get(args.get("target"))
    if not hasattr(o.data, "materials"):
        raise UserError("'%s' can't have materials." % o.name)
    mat = bpy.data.materials.new(args["asset_id"])
    p = principled(mat)
    nt = mat.node_tree
    coord = nt.nodes.new("ShaderNodeTexCoord")
    mapping = nt.nodes.new("ShaderNodeMapping")
    s = float(args.get("scale") or 1.0)
    mapping.inputs["Scale"].default_value = (s, s, s)
    nt.links.new(coord.outputs["UV"], mapping.inputs["Vector"])
    maps = args["_maps"]

    def tex(path, non_color):
        n = nt.nodes.new("ShaderNodeTexImage")
        n.image = bpy.data.images.load(path, check_existing=True)
        if non_color:
            n.image.colorspace_settings.name = "Non-Color"
        nt.links.new(mapping.outputs["Vector"], n.inputs["Vector"])
        return n

    if "diffuse" in maps:
        nt.links.new(tex(maps["diffuse"], False).outputs["Color"], p.inputs["Base Color"])
    if "roughness" in maps:
        nt.links.new(tex(maps["roughness"], True).outputs["Color"], p.inputs["Roughness"])
    if "normal" in maps:
        nm = nt.nodes.new("ShaderNodeNormalMap")
        nt.links.new(tex(maps["normal"], True).outputs["Color"], nm.inputs["Color"])
        nt.links.new(nm.outputs["Normal"], p.inputs["Normal"])
    if o.material_slots:
        o.material_slots[o.active_material_index].material = mat
    else:
        o.data.materials.append(mat)
    return {"object": o.name, "material": mat.name, "maps": sorted(maps.keys())}


# ---------------------------------------------------------------------------- structure

MODIFIERS = {
    "subdivision": "SUBSURF",
    "bevel": "BEVEL",
    "array": "ARRAY",
    "mirror": "MIRROR",
    "solidify": "SOLIDIFY",
    "boolean": "BOOLEAN",
    "displace": "DISPLACE",
    "wireframe": "WIREFRAME",
    "smooth": "SMOOTH",
    "decimate": "DECIMATE",
    "screw": "SCREW",
    "triangulate": "TRIANGULATE",
    "weld": "WELD",
    "remesh": "REMESH",
}


@op(
    "add_modifier",
    "Add a modifier: subdivision, bevel, array, mirror, solidify, boolean, displace, wireframe, smooth, decimate, screw, triangulate, weld, remesh. "
    "settings sets modifier properties by their Blender names (e.g. {levels: 2}, {width: 0.05, segments: 3}, {count: 5, relative_offset_displace: [1.2,0,0]}, {operation: 'DIFFERENCE', object: 'Cutter'}).",
    {"type": "object", "properties": {"name": {"type": "string"}, "type": {"type": "string", "enum": sorted(MODIFIERS)}, "settings": {"type": "object"}}, "required": ["name", "type"]},
)
def add_modifier(args):
    o = obj_get(args.get("name"))
    t = MODIFIERS.get((args.get("type") or "").lower())
    if not t:
        raise UserError("type must be one of: " + ", ".join(sorted(MODIFIERS)))
    try:
        m = o.modifiers.new(args.get("type").capitalize(), t)
    except Exception as e:
        raise UserError("Can't add %s to %s (%s): %s" % (t, o.name, o.type, e))
    applied = {}
    for k, v in (args.get("settings") or {}).items():
        if not hasattr(m, k):
            raise UserError("%s has no setting '%s'." % (t, k))
        if k in ("object", "offset_object", "mirror_object") and isinstance(v, str):
            v = obj_get(v)
        try:
            setattr(m, k, v)
        except Exception as e:
            raise UserError("Can't set %s=%r: %s" % (k, v, e))
        applied[k] = v.name if hasattr(v, "name") else v
    if t == "SUBSURF" and "render_levels" not in applied and "levels" in applied:
        m.render_levels = m.levels
    return {"object": o.name, "modifier": m.name, "type": t, "settings": applied}


@op("rename_object", "Rename an object.", {"type": "object", "properties": {"name": {"type": "string"}, "new_name": {"type": "string"}}, "required": ["name", "new_name"]})
def rename_object(args):
    o = obj_get(args.get("name"))
    o.name = str(args.get("new_name"))[:63]
    return {"name": o.name}


@op("set_parent", "Parent an object to another (keeps its world transform). parent=null clears it.", {"type": "object", "properties": {"name": {"type": "string"}, "parent": {"type": ["string", "null"]}}, "required": ["name"]})
def set_parent(args):
    o = obj_get(args.get("name"))
    mw = o.matrix_world.copy()
    o.parent = obj_get(args["parent"]) if args.get("parent") else None
    o.matrix_world = mw
    return {"name": o.name, "parent": o.parent.name if o.parent else None}


@op("set_visibility", "Show or hide objects in the viewport and/or render.", {"type": "object", "properties": {"names": {"type": "array", "items": {"type": "string"}}, "viewport": {"type": "boolean"}, "render": {"type": "boolean"}}, "required": ["names"]})
def set_visibility(args):
    done = []
    for n in args.get("names") or []:
        o = obj_get(n)
        if args.get("viewport") is not None:
            o.hide_set(not args["viewport"])
            o.hide_viewport = not args["viewport"]
        if args.get("render") is not None:
            o.hide_render = not args["render"]
        done.append(o.name)
    return {"objects": done}


@op("delete_objects", "Delete objects by name.", {"type": "object", "properties": {"names": {"type": "array", "items": {"type": "string"}, "maxItems": 200}}, "required": ["names"]}, destructive=True)
def delete_objects(args):
    names = args.get("names") or []
    objs = [obj_get(n) for n in names]
    deleted = [o.name for o in objs]
    for o in objs:
        bpy.data.objects.remove(o, do_unlink=True)
    return {"deleted": deleted}


@op("save_file", "Save the .blend file (path optional if it was saved before). Only when the user asks.", {"type": "object", "properties": {"path": {"type": "string"}}}, destructive=True)
def save_file(args):
    path = args.get("path")
    if path:
        path = os.path.abspath(os.path.expanduser(str(path)))
        if not path.lower().endswith(".blend"):
            raise UserError("path must end with .blend")
        if not os.path.isdir(os.path.dirname(path)):
            raise UserError("Folder does not exist: " + os.path.dirname(path))
        bpy.ops.wm.save_as_mainfile(filepath=path)
    else:
        if not bpy.data.filepath:
            raise UserError("The file was never saved; pass a path ending in .blend.")
        bpy.ops.wm.save_mainfile()
    return {"file": bpy.data.filepath}


@op(
    "run_python",
    "Run Python in Blender (bpy). Only available when the user enabled 'Allow Python' in the add-on panel. Set a variable named `result` to return a JSON-serializable value; print() output is returned too.",
    {"type": "object", "properties": {"code": {"type": "string", "maxLength": 20000}}, "required": ["code"]},
    destructive=True,
    requires="allow_python",
)
def run_python(args):
    import contextlib
    import io

    code = args.get("code") or ""
    buf = io.StringIO()
    env = {"bpy": bpy, "__name__": "__klakix__"}
    with contextlib.redirect_stdout(buf):
        exec(compile(code, "<agent>", "exec"), env)
    res = env.get("result")
    try:
        import json

        json.dumps(res)
    except Exception:
        res = repr(res)[:4000]
    return {"result": res, "stdout": buf.getvalue()[-4000:]}


# ---------------------------------------------------------------------------- catalog


def catalog(allow_python=False):
    items = []
    for name, d in REGISTRY.items():
        if d["requires"] == "allow_python" and not allow_python:
            continue
        items.append({"ad": name, "aciklama": d["description"], "sema": d["schema"], "salt_okunur": d["read_only"], "yikici": d["destructive"]})
    return {"surum": CATALOG_VERSION, "rehber": GUIDE, "islemler": items}
