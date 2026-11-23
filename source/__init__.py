"""PULLS'NPiNS ClothLite - free, minimal Pull Tool for existing Cloth simulations.

Click-drag a vertex of a mesh that already has a Cloth modifier to pull and pin it. Nothing else.
Sets the cloth up the same way as the full EverCloth (PNP_OriginalCoords modifier + a separate rest shape key),
so the cloth stretches like real cloth, and moves the pin a small step per frame so it stays smooth.
Blender extension: metadata lives in blender_manifest.toml. Stays dormant (no panel, no tool) while the full
PULLS'NPiNS EverCloth is enabled. Pins are plain pins (Pin Shape Key + pin group); hooks and Empties belong to
the full EverCloth only.
"""

import time

import numpy as np
import bpy
import bpy.utils.previews
import gpu
from bpy_extras import view3d_utils
from gpu_extras.batch import batch_for_shader
from mathutils import Vector

LITE_VERSION = "1.0.15"   # keep in sync with blender_manifest.toml
LITE_MARKER_PROP = "pnp_lite_setup"   # custom property on the object: lets EverCloth recognise a ClothLite setup
DEBUG_PIN_GAP = False           # developer switch: console line twice a second while a pin is held (pin key vs. evaluated mesh)
PIN_KEY = "PNP_Lite_Pin"        # shape key that carries the pin positions
REST_KEY = "PNP_Lite_Rest"      # shape key that holds the unpinned shape: the cloth's rest shape
ORCO_MOD_NAME = "PNP_OriginalCoords"   # empty Geometry Nodes modifier in front of Cloth (same as EverCloth)
PIN_GROUP = "PNP_Lite_Pins"     # vertex group used as the cloth pin group (if none is set yet)
NO_CLOTH_MSG = "No Cloth simulation found. Please create cloth sim first."

# ---------------------------------------------------------------------------------------------------------
# State (module level, so a forced cleanup never depends on a possibly dead operator instance)
# ---------------------------------------------------------------------------------------------------------
_S = {
    "running": False,
    "stop": False,
    "tick": 0.0,
    "timer": None,
    "draw": None,
    "obj": None,
    "ids": set(),
    "drag_vid": None,
    "we_started_play": False,
    "scene": None,
    "sync": None,
    "movers": [],       # pins that are still travelling to their target (moved once per frame)
    "last_step_frame": None,   # frame on which the pins last took a step while the timeline was playing
    "warned_full": False,
}


# ---------------------------------------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------------------------------------
def full_addon_present():
    """True while the full PULLS'NPiNS EverCloth is registered."""
    return (hasattr(bpy.types.Scene, "pulls_n_pins_props")
            or hasattr(bpy.types, "PNP_OT_pull_cloth"))


def get_cloth(obj):
    if obj is None or obj.type != 'MESH':
        return None
    for m in obj.modifiers:
        if m.type == 'CLOTH':
            return m
    return None


def pin_group_name(obj):
    cloth = get_cloth(obj)
    return (cloth.settings.vertex_group_mass if cloth else "") or PIN_GROUP


def get_pin_key(obj):
    sk = obj.data.shape_keys
    return sk.key_blocks.get(PIN_KEY) if sk else None


# ---------------------------------------------------------------------------------------------------------
# Playback control. The cloth only simulates while frames are evaluated in order, so the tool has to be able to
# start playback itself. animation_play() needs a 3D viewport in its context; called bare from the modal
# handler it can silently do nothing, so it runs under an explicit window/area/region override and the result is
# checked. When Blender still refuses, the tool steps the frames itself so the simulation keeps running.
# ---------------------------------------------------------------------------------------------------------
def _view3d_target(context=None):
    """(window, area, region) of a 3D viewport: the one the tool runs in first, any other as a fallback."""
    ctx = context or bpy.context
    try:
        win, area = ctx.window, ctx.area
        if win is not None and area is not None and area.type == 'VIEW_3D':
            for region in area.regions:
                if region.type == 'WINDOW':
                    return win, area, region
    except Exception:
        pass
    try:
        for win in ctx.window_manager.windows:
            for area in win.screen.areas:
                if area.type != 'VIEW_3D':
                    continue
                for region in area.regions:
                    if region.type == 'WINDOW':
                        return win, area, region
    except Exception:
        pass
    return None, None, None


def _is_playing():
    try:
        if bpy.context.screen is not None:
            return bool(bpy.context.screen.is_animation_playing)
    except Exception:
        pass
    try:
        return any(w.screen.is_animation_playing for w in bpy.context.window_manager.windows)
    except Exception:
        return False


def _toggle_playback():
    """Start or stop playback in the 3D viewport. Returns False when Blender raised an error."""
    win, area, region = _view3d_target()
    try:
        if win is not None:
            with bpy.context.temp_override(window=win, area=area, region=region):
                bpy.ops.screen.animation_play()
        else:
            bpy.ops.screen.animation_play()
        return True
    except Exception as exc:
        print(f"[PULLS'NPiNS ClothLite] could not toggle playback: {exc}")
        return False


def _step_frame(context):
    """Advance the timeline by one frame (wrapping at the end), used when playback cannot be started."""
    scene = context.scene
    if scene.use_preview_range:
        lo, hi = scene.frame_preview_start, scene.frame_preview_end
    else:
        lo, hi = scene.frame_start, scene.frame_end
    frame = scene.frame_current + 1
    if frame > hi:
        frame = lo
    scene.frame_set(frame)


def _add_geometry_io(node_group):
    try:
        node_group.interface.new_socket(name="Geometry", in_out='INPUT', socket_type='NodeSocketGeometry')
        node_group.interface.new_socket(name="Geometry", in_out='OUTPUT', socket_type='NodeSocketGeometry')
    except AttributeError:                                   # very old API
        node_group.inputs.new('NodeSocketGeometry', "Geometry")
        node_group.outputs.new('NodeSocketGeometry', "Geometry")


def ensure_original_coords_modifier(obj):
    """The 'original coords' workaround: an empty Geometry Nodes modifier directly in front of Cloth.

    Cloth reads its rest shape from a hidden 'original coordinates' layer that Blender only attaches to the
    mesh when something sits in front of Cloth. Without it the rest shape silently falls back to the current
    (pulled) pose, so the springs adopt the stretched shape. Same modifier and name as the full EverCloth.
    Returns True when it had to create or move anything.
    """
    cloth = get_cloth(obj)
    changed = False
    gn = obj.modifiers.get(ORCO_MOD_NAME)
    if gn is None or gn.type != 'NODES':
        gn = obj.modifiers.new(name=ORCO_MOD_NAME, type='NODES')
        changed = True
    if gn.node_group is None:
        group = bpy.data.node_groups.new(ORCO_MOD_NAME, 'GeometryNodeTree')
        _add_geometry_io(group)
        gin = group.nodes.new('NodeGroupInput')
        gout = group.nodes.new('NodeGroupOutput')
        gin.location, gout.location = (-200, 0), (200, 0)
        group.links.new(gin.outputs[0], gout.inputs[0])
        gn.node_group = group
        changed = True
    mods = list(obj.modifiers)
    ci, gi = mods.index(cloth), mods.index(gn)
    if gi != ci - 1:
        obj.modifiers.move(gi, ci - 1 if gi < ci else ci)
        changed = True
    return changed


def _basis_flat(key):
    buf = np.empty(len(key.data) * 3, dtype=np.float64)
    key.data.foreach_get('co', buf)
    return buf


def ensure_pin_setup(obj):
    """Make sure the cloth has a pin group, the pin key, a dedicated rest key and the original-coords modifier.

    Only writes what is missing or different. Returns (vertex group, pin key, changed), where changed is True when
    the simulation setup was altered, which means the cloth has to start again from the first frame.
    """
    cloth = get_cloth(obj)
    settings = cloth.settings
    changed = False
    name = settings.vertex_group_mass or PIN_GROUP
    vg = obj.vertex_groups.get(name)
    if vg is None:
        vg = obj.vertex_groups.new(name=name)
        changed = True
    if settings.vertex_group_mass != name:
        settings.vertex_group_mass = name
        changed = True
    if obj.data.shape_keys is None:
        obj.shape_key_add(name="Basis", from_mix=False)
        changed = True
    blocks = obj.data.shape_keys.key_blocks
    key = blocks.get(PIN_KEY)
    if key is None:
        key = obj.shape_key_add(name=PIN_KEY, from_mix=False)
        changed = True
    if _hooks_back_to_key(obj, key):
        changed = True
    if key.value != 1.0:
        key.value = 1.0
    if key.mute:
        key.mute = False

    # The rest shape must be its own key, not Basis: with Basis Blender ignores it and the cloth is built from
    # the pulled pose. The key follows Basis (the untouched unpinned shape) and never contributes to the mesh.
    basis = obj.data.shape_keys.reference_key
    rest = blocks.get(REST_KEY)
    if rest is None:
        rest = obj.shape_key_add(name=REST_KEY, from_mix=False)
        changed = True
    if rest.value != 0.0:
        rest.value = 0.0
    if len(rest.data) == len(basis.data):
        b = _basis_flat(basis)
        if not np.array_equal(b, _basis_flat(rest)):
            rest.data.foreach_set('co', b)
            changed = True
    current = settings.rest_shape_key
    if current is None or current == basis or current.name == REST_KEY:
        if current is None or current.name != REST_KEY:
            settings.rest_shape_key = rest
            changed = True

    if ensure_original_coords_modifier(obj):
        changed = True
    if settings.use_dynamic_mesh:
        settings.use_dynamic_mesh = False        # a dynamic mesh ignores the rest shape entirely
        changed = True
    if settings.pin_stiffness < 1.0:
        settings.pin_stiffness = 1.0          # a weak pin would lag behind the mouse
    if obj.get(LITE_MARKER_PROP) != LITE_VERSION:
        obj[LITE_MARKER_PROP] = LITE_VERSION   # lets the full EverCloth recognise and adopt this setup
    return vg, key, changed


def _hooks_back_to_key(obj, key):
    """Setups made by ClothLite 1.0.13 carry their pins in hooks. Put those pulls back into the Pin Shape Key."""
    hooks = [m for m in obj.modifiers if m.type == 'HOOK' and m.name.startswith("PNP_LiteHook_")]
    if not hooks:
        return False
    inv = obj.matrix_world.inverted()
    n = len(key.data)
    for mod in hooks:
        empty = mod.object
        try:
            vid = int(mod.name.split("_")[-1])
        except ValueError:
            vid = -1
        if empty is not None and 0 <= vid < n and mod.strength > 0.0:
            key.data[vid].co = inv @ Vector(empty.location)
        mod.object = None
        obj.modifiers.remove(mod)
        if empty is not None and empty.get("pnp_lite_hook_of") is not None:
            for coll in tuple(empty.users_collection):
                coll.objects.unlink(empty)
            bpy.data.objects.remove(empty)
    return True


def weight_of(vg, vid):
    try:
        return vg.weight(vid)
    except RuntimeError:
        return None


def rest_co(key, vid):
    return key.relative_key.data[vid].co.copy()


def collect_pinned(obj):
    vg = obj.vertex_groups.get(pin_group_name(obj))
    if vg is None:
        return set()
    idx = vg.index
    return {v.index for v in obj.data.vertices
            for g in v.groups if g.group == idx and g.weight > 0.0}


GRAB_PIN_RADIUS = 28      # px: LMB near an existing pin re-grabs that pin (even from behind the cloth)
GRAB_SURFACE_RADIUS = 70  # px: LMB just off the cloth still grabs the nearest vertex
UNPIN_RUN_SECONDS = 1.0   # playback runs this long after an unpin, then pauses again
UNPIN_RADIUS = 140        # px: RMB unpins the nearest pin, even from far away or from behind
PULL_STEP_FRACTION = 0.04  # a pin never moves more than this share of the mesh size per simulated frame


def world_coords(context, obj):
    """World positions of every vertex as they are right now (cloth-deformed when the counts match)."""
    me = obj.evaluated_get(context.evaluated_depsgraph_get()).data
    if len(me.vertices) != len(obj.data.vertices):
        me = obj.data
    n = len(me.vertices)
    co = np.empty(n * 3, dtype=np.float64)
    me.vertices.foreach_get('co', co)
    mw = np.array(obj.matrix_world, dtype=np.float64)
    return co.reshape(n, 3) @ mw[:3, :3].T + mw[:3, 3]


def nearest_on_screen(region, rv3d, coord, world, ids, max_px):
    """Vertex (from ids, or all) whose screen position is nearest the cursor, within max_px. Ignores occlusion."""
    if ids is None:
        idx = np.arange(len(world))
    else:
        idx = np.fromiter((i for i in ids if i < len(world)), dtype=np.int64)
    if idx.size == 0:
        return None
    pts = world[idx]
    pm = np.array(rv3d.perspective_matrix, dtype=np.float64)
    clip = np.hstack([pts, np.ones((len(pts), 1))]) @ pm.T
    w = clip[:, 3]
    ok = w > 1e-6
    if not ok.any():
        return None
    w = np.where(ok, w, 1.0)
    sx = (clip[:, 0] / w * 0.5 + 0.5) * region.width
    sy = (clip[:, 1] / w * 0.5 + 0.5) * region.height
    dist = np.hypot(sx - coord.x, sy - coord.y)
    dist[~ok] = np.inf
    best = int(np.argmin(dist))
    if dist[best] > max_px:
        return None
    vid = int(idx[best])
    return vid, Vector(world[vid])


def surface_vertex(context, obj, region, rv3d, coord, world):
    """Vertex of the face under the cursor (ray cast), nearest to the hit point."""
    depsgraph = context.evaluated_depsgraph_get()
    eval_obj = obj.evaluated_get(depsgraph)
    eval_me = eval_obj.data
    if len(eval_me.vertices) != len(obj.data.vertices):
        return None
    inv = obj.matrix_world.inverted()
    origin = view3d_utils.region_2d_to_origin_3d(region, rv3d, coord)
    direction = view3d_utils.region_2d_to_vector_3d(region, rv3d, coord)
    ok, loc, _nrm, face = eval_obj.ray_cast(inv @ origin, (inv.to_3x3() @ direction).normalized(),
                                            depsgraph=depsgraph)
    if not ok or face < 0 or face >= len(eval_me.polygons):
        return None
    best = min(eval_me.polygons[face].vertices,
               key=lambda v: (eval_me.vertices[v].co - loc).length_squared)
    return best, Vector(world[best])


def find_vertex_to_grab(context, obj, region, coord, pinned):
    rv3d = region.data
    world = world_coords(context, obj)
    hit = nearest_on_screen(region, rv3d, coord, world, pinned, GRAB_PIN_RADIUS)
    if hit is None:
        hit = surface_vertex(context, obj, region, rv3d, coord, world)
    if hit is None:
        hit = nearest_on_screen(region, rv3d, coord, world, None, GRAB_SURFACE_RADIUS)
    return hit


def find_pin_to_remove(context, obj, region, coord, pinned):
    rv3d = region.data
    world = world_coords(context, obj)
    hit = nearest_on_screen(region, rv3d, coord, world, pinned, UNPIN_RADIUS)
    if hit is None:
        hit = surface_vertex(context, obj, region, rv3d, coord, world)
    return hit



# ---------------------------------------------------------------------------------------------------------
# Pin motion: the pin walks to the mouse a small step per frame, like real cloth being pulled by a hand.
# Teleporting the pin makes the springs stretch far past their limit in one step (the "weird" spike), and a
# shape key write in the middle of a frame makes Blender solve that frame again (the lag). So the pin is only
# moved by the tool's own timer between two frames, by at most a few percent of the mesh size per solved frame.
# ---------------------------------------------------------------------------------------------------------
def pull_step_limit(obj):
    """Largest pin move per frame, in the object's local units."""
    key = get_pin_key(obj)
    n = len(key.data) if key else 0
    if n == 0:
        return 0.05
    co = _basis_flat(key.relative_key).reshape(-1, 3)
    diag = float(np.linalg.norm(co.max(axis=0) - co.min(axis=0)))
    return max(diag * PULL_STEP_FRACTION, 1e-4)


def _tag_pin_changed(obj):
    """Flag the cloth object for re-evaluation (the same tag the confirm step always used)."""
    try:
        if obj is not None:
            obj.update_tag()
    except Exception:
        pass


def _advance_movers():
    """Move every travelling pin one step towards its target. Finished, released pins are dropped."""
    movers = _S["movers"]
    obj = bpy.data.objects.get(_S["obj"]) if _S["obj"] else None
    key = get_pin_key(obj) if obj else None
    if not movers or key is None:
        movers.clear()
        return
    n = len(key.data)
    for m in tuple(movers):
        vid = m["vid"]
        if vid >= n:
            movers.remove(m)
            continue
        delta = m["target"] - m["cur"]
        dist = delta.length
        if dist > m["limit"]:
            m["cur"] = m["cur"] + delta * (m["limit"] / dist)
        else:
            m["cur"] = m["target"].copy()
        key.data[vid].co = m["cur"]
        if not m["active"] and (m["target"] - m["cur"]).length_squared < 1e-14:
            movers.remove(m)
    _tag_pin_changed(obj)


def _snap_movers():
    """Put every travelling pin on its target at once (used when the tool is closed)."""
    obj = bpy.data.objects.get(_S["obj"]) if _S["obj"] else None
    key = get_pin_key(obj) if obj else None
    if key is not None:
        for m in _S["movers"]:
            if m["vid"] < len(key.data):
                key.data[m["vid"]].co = m["target"]
        _tag_pin_changed(obj)
    _S["movers"].clear()


# ---------------------------------------------------------------------------------------------------------
# Pin overlay
# ---------------------------------------------------------------------------------------------------------
COLOR_PIN = (1.0, 0.533, 0.0, 0.95)       # pinned vertices while the tool runs (orange)
COLOR_DRAG = (0.878, 0.992, 0.02, 1.0)    # vertex being pulled (lime)
_ACCENT_PALETTE = ((0.004, 0.608, 0.886), (0.157, 0.459, 0.451), (0.408, 0.275, 0.651),
                   (0.929, 0.855, 0.059), (0.690, 0.078, 0.094), (0.965, 0.380, 0.733),
                   (0.988, 0.427, 0.043))


def _shader():
    for name in ('UNIFORM_COLOR', '3D_UNIFORM_COLOR'):
        try:
            return gpu.shader.from_builtin(name)
        except Exception:
            continue
    return None


def _draw_squares(shader, points, size_px, color):
    """Square dots of a fixed pixel size (two triangles each, same result on every backend/version)."""
    region, rv3d = bpy.context.region, bpy.context.region_data
    if not points or region is None or rv3d is None or region.width <= 0 or region.height <= 0:
        return
    pm = rv3d.perspective_matrix
    pm_inv = pm.inverted()
    hx, hy = size_px / region.width, size_px / region.height
    tris = []
    for p in points:
        c = pm @ Vector((p[0], p[1], p[2], 1.0))
        if abs(c.w) < 1e-9:
            continue
        corners = []
        for sx, sy in ((-1, -1), (1, -1), (1, 1), (-1, 1)):
            wp = pm_inv @ Vector((c.x + sx * hx * c.w, c.y + sy * hy * c.w, c.z, c.w))
            corners.append((wp.x / wp.w, wp.y / wp.w, wp.z / wp.w))
        tris.extend((corners[0], corners[1], corners[2], corners[0], corners[2], corners[3]))
    if tris:
        shader.uniform_float("color", color)
        batch_for_shader(shader, 'TRIS', {"pos": tris}).draw(shader)


def _draw_pins():
    obj = bpy.data.objects.get(_S["obj"]) if _S["obj"] else None
    if obj is None or not _S["ids"]:
        return
    try:
        key = get_pin_key(obj)
        shader = _shader()
        if key is None or shader is None:
            return
        n = len(key.data)
        mw = obj.matrix_world
        gpu.state.blend_set('ALPHA')
        gpu.state.depth_test_set('ALWAYS')
        shader.bind()
        drag = _S["drag_vid"]
        _draw_squares(shader, [mw @ key.data[i].co for i in tuple(_S["ids"]) if i < n and i != drag],
                      10, COLOR_PIN)
        if drag is not None and drag < n:
            _draw_squares(shader, [mw @ key.data[drag].co], 13, COLOR_DRAG)
        gpu.state.depth_test_set('NONE')
        gpu.state.blend_set('NONE')
    except Exception:
        pass


# ---------------------------------------------------------------------------------------------------------
# Square colour swatch on the panel button (cycles while idle, orange while running, lime while dragging)
# ---------------------------------------------------------------------------------------------------------
_ICON = {"coll": None, "last": None, "step": 0.0, "timer": None}
_ICON_SIZE, _SQUARE = 32, 20


def _swatch_icon_id(color):
    color = tuple(round(c, 3) for c in color)
    if _ICON["coll"] is None:
        _ICON["coll"] = bpy.utils.previews.new()
    coll = _ICON["coll"]
    preview = coll["swatch"] if "swatch" in coll else coll.new("swatch")
    if _ICON["last"] != color:
        pad = (_ICON_SIZE - _SQUARE) // 2
        r, g, b, a = color
        pixels = []
        for y in range(_ICON_SIZE):
            for x in range(_ICON_SIZE):
                inside = pad <= x < pad + _SQUARE and pad <= y < pad + _SQUARE
                pixels.extend((r, g, b, a) if inside else (0.0, 0.0, 0.0, 0.0))
        preview.image_size = preview.icon_size = (_ICON_SIZE, _ICON_SIZE)
        preview.image_pixels_float = preview.icon_pixels_float = pixels
        _ICON["last"] = color
    return preview.icon_id


def _button_color():
    if _S["running"]:
        return COLOR_DRAG if _S["drag_vid"] is not None else COLOR_PIN
    n = len(_ACCENT_PALETTE)
    pos = _ICON["step"] * n
    i = int(pos) % n
    t = pos - int(pos)
    c0, c1 = _ACCENT_PALETTE[i], _ACCENT_PALETTE[(i + 1) % n]
    return (c0[0] + (c1[0] - c0[0]) * t, c0[1] + (c1[1] - c0[1]) * t,
            c0[2] + (c1[2] - c0[2]) * t, 1.0)


def _accent_tick():
    if _S["running"]:
        return 0.5              # the swatch is a fixed colour while the tool runs: no redraw load during playback
    _ICON["step"] = (_ICON["step"] + 0.0125) % 1.0
    try:
        for win in bpy.context.window_manager.windows:
            for area in win.screen.areas:
                if area.type == 'VIEW_3D' and area.spaces.active.show_region_ui:
                    for r in area.regions:
                        if r.type == 'UI':
                            r.tag_redraw()
    except Exception:
        pass
    return 0.1


def _tag_ui_redraw():
    try:
        for win in bpy.context.window_manager.windows:
            for area in win.screen.areas:
                if area.type == 'VIEW_3D':
                    area.tag_redraw()
    except Exception:
        pass


def _cleanup_runtime(context=None):
    """Tear down everything the running tool owns. Safe to call twice."""
    ctx = context or bpy.context
    if _S["timer"] is not None:
        try:
            ctx.window_manager.event_timer_remove(_S["timer"])
        except Exception:
            pass
        _S["timer"] = None
    if _S["draw"] is not None:
        try:
            bpy.types.SpaceView3D.draw_handler_remove(_S["draw"], 'WINDOW')
        except Exception:
            pass
        _S["draw"] = None
    try:
        ctx.workspace.status_text_set(None)
        ctx.window.cursor_modal_restore()
    except Exception:
        pass
    if _S["we_started_play"]:
        try:
            if _is_playing():
                _toggle_playback()
        except Exception:
            pass
    scene = bpy.data.scenes.get(_S["scene"]) if _S["scene"] else None
    if scene is not None and _S["sync"] is not None:
        try:
            scene.sync_mode = _S["sync"]
        except Exception:
            pass
    _S.update(running=False, stop=False, obj=None, ids=set(), drag_vid=None, we_started_play=False,
              scene=None, sync=None, movers=[], last_step_frame=None)
    try:
        for win in ctx.window_manager.windows:
            for area in win.screen.areas:
                if area.type == 'VIEW_3D':
                    area.tag_redraw()
    except Exception:
        pass


# ---------------------------------------------------------------------------------------------------------
# The Pull Tool
# ---------------------------------------------------------------------------------------------------------
_PASS_TYPES = {
    'MIDDLEMOUSE', 'WHEELUPMOUSE', 'WHEELDOWNMOUSE', 'WHEELINMOUSE', 'WHEELOUTMOUSE',
    'TRACKPADPAN', 'TRACKPADZOOM', 'MOUSEROTATE', 'MOUSESMARTZOOM', 'NDOF_MOTION',
    'NDOF_BUTTON_FIT', 'SPACE', 'HOME', 'MOUSEMOVE', 'INBETWEEN_MOUSEMOVE',
}
_SWALLOW_TYPES = {
    'ZERO', 'ONE', 'TWO', 'THREE', 'FOUR', 'FIVE', 'SIX', 'SEVEN', 'EIGHT', 'NINE',
    'DEL', 'BACK_SPACE', 'PERIOD', 'COMMA', 'SLASH', 'MINUS', 'EQUAL', 'SEMI_COLON', 'QUOTE',
    'ACCENT_GRAVE', 'LEFT_BRACKET', 'RIGHT_BRACKET', 'BACK_SLASH',
}
_UI_REGIONS = {
    'UI': 'show_region_ui',
    'TOOLS': 'show_region_toolbar',
    'HEADER': 'show_region_header',
    'TOOL_HEADER': 'show_region_tool_header',
}


class PNPLITE_OT_pull_tool(bpy.types.Operator):
    bl_idname = "pnp_lite.pull_tool"
    bl_label = "Pull Tool"
    bl_description = ("Click-drag a vertex of the Cloth mesh to pull and pin it\n"
                      "LMB: grab and pin | Shift+LMB: grab only | RMB: unpin\n"
                      "Enter, Tab or Esc: exit (click the button again to exit too)")
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        if full_addon_present():
            return False
        if context.area is None or context.area.type != 'VIEW_3D':
            return False
        if _S["running"]:
            return True
        return get_cloth(context.view_layer.objects.active) is not None

    # -- lifecycle ------------------------------------------------------------------------------------
    def invoke(self, context, event):
        if _S["running"]:
            if time.monotonic() - _S["tick"] > 1.5:
                _cleanup_runtime(context)       # an earlier run died without cleaning up
            else:
                _S["stop"] = True               # second click on the button = turn the tool off
                return {'FINISHED'}

        obj = context.view_layer.objects.active
        if obj is None or obj.type != 'MESH' or get_cloth(obj) is None:
            self.report({'ERROR'}, NO_CLOTH_MSG)
            return {'CANCELLED'}
        if obj.mode != 'OBJECT':
            self.report({'ERROR'}, "Switch to Object Mode to use the Pull Tool.")
            return {'CANCELLED'}

        try:
            changed = ensure_pin_setup(obj)[2]
        except Exception as exc:
            self.report({'ERROR'}, f"Could not prepare the cloth for pulling: {exc}")
            return {'CANCELLED'}
        scene = context.scene
        if changed and scene.frame_current != scene.frame_start:
            # The cloth only reads its rest shape when it starts, so a changed setup has to restart on frame 1.
            scene.frame_set(scene.frame_start)
            self.report({'INFO'}, "Cloth set up for pulling - simulation restarted from the first frame.")

        self.obj_name = obj.name
        self.drag = None
        self.drag_region_xy = None
        self.play_until = 0.0
        self.stop_when_settled = False
        self.user_control = False
        self._fail = 0
        self._last_try = 0.0
        self._stepping = False

        _S.update(running=True, stop=False, tick=time.monotonic(), obj=obj.name,
                  ids=collect_pinned(obj), drag_vid=None, we_started_play=False)
        _S["draw"] = bpy.types.SpaceView3D.draw_handler_add(_draw_pins, (), 'WINDOW', 'POST_VIEW')
        _S["timer"] = context.window_manager.event_timer_add(0.03, window=context.window)
        context.window.cursor_modal_set('CROSSHAIR')
        context.workspace.status_text_set(
            "Pull Tool:  LMB grab & pin   |   Shift+LMB grab only   |   RMB unpin   |   Enter / Tab / Esc exit")

        _S["scene"], _S["sync"] = context.scene.name, context.scene.sync_mode
        context.scene.sync_mode = 'NONE'                # play every frame: the cloth must not skip steps
        context.window_manager.modal_handler_add(self)
        context.area.tag_redraw()
        return {'RUNNING_MODAL'}

    def _finish(self, context):
        if self.drag is not None:
            self._end_drag(cancel=False)
        _snap_movers()
        _cleanup_runtime(context)
        return {'FINISHED'}

    # -- playback: paused by default, runs while pulling (Space = the user takes over) ---------------
    def _play_start(self):
        """Start the simulation. Called on every grab and unpin; the timer keeps it running (see _ensure_running)."""
        self.user_control = False
        self._fail = 0
        self._last_try = 0.0
        if not _is_playing():
            if _toggle_playback() and _is_playing():
                _S["we_started_play"] = True

    def _play_stop(self):
        if _S["we_started_play"]:
            _S["we_started_play"] = False
            if _is_playing():
                _toggle_playback()

    def _report_pin_gap(self, context):
        """Twice a second: how far the evaluated pin vertex is from the Pin Shape Key position (0 = it follows)."""
        now = time.monotonic()
        if now - getattr(self, "_gap_t", 0.0) < 0.5:
            return
        self._gap_t = now
        try:
            obj, d = self._obj(), self.drag
            key = get_pin_key(obj)
            ev = obj.evaluated_get(context.evaluated_depsgraph_get()).data
            vid = d["vid"]
            if key is None or vid >= len(ev.vertices):
                return
            gap = (ev.vertices[vid].co - key.data[vid].co).length
            print(f"[PULLS'NPiNS ClothLite] frame {context.scene.frame_current} playing={_is_playing()} "
                  f"stepping={self._stepping} pin-to-mesh gap={gap:.4f} "
                  f"pin-to-target={(d['target'] - d['cur']).length:.4f} cache='{self._cache_info(obj)}'")
        except Exception as exc:
            print(f"[PULLS'NPiNS ClothLite] pin gap check failed: {exc}")

    @staticmethod
    def _cache_info(obj):
        cloth = get_cloth(obj)
        try:
            return str(cloth.point_cache.info)
        except Exception:
            return "?"

    def _ensure_running(self, context):
        """Called every timer tick: while a pin is held, travelling or just released, the cloth must be running.

        Returns True when this tick advanced a frame itself (so the caller does not move the pins twice).
        """
        wanted = self.drag is not None or self.stop_when_settled or bool(self.play_until)
        if not wanted or self.user_control:
            self._fail = 0
            self._stepping = False
            return False
        if _is_playing():
            self._fail = 0
            self._stepping = False
            return False
        now = time.monotonic()
        if self._fail < 3:                                   # try to start playback a few times first
            if now - self._last_try >= 0.2:
                self._last_try = now
                self._fail += 1
                if _toggle_playback() and _is_playing():
                    _S["we_started_play"] = True
                    self._fail = 0
            return False
        if not self._stepping:
            self._stepping = True
            print("[PULLS'NPiNS ClothLite] playback could not be started from the tool - stepping frames directly.")
        try:
            _step_frame(context)                             # the next timer tick moves the pins as usual
            return True
        except Exception as exc:
            print(f"[PULLS'NPiNS ClothLite] could not step the timeline: {exc}")
            return False

    def _walk_pins(self, context, stepped):
        """Take one pin step from the tool's timer, never from a frame handler (a write made while a frame is
        being evaluated does not reach cached cloth frames). While the timeline plays, at most one step per
        frame that was really solved, so a heavy cloth is never pulled further than the per-frame limit."""
        if _is_playing() or stepped:
            frame = context.scene.frame_current
            if frame == _S["last_step_frame"]:
                return
            _S["last_step_frame"] = frame
        _advance_movers()

    # -- region helpers -------------------------------------------------------------------------------
    @staticmethod
    def _inside(r, mx, my):
        return r.x <= mx < r.x + r.width and r.y <= my < r.y + r.height

    def _viewport_region(self, area, event):
        """The 3D viewport region under the mouse, or None when the mouse is over UI / another area."""
        mx, my = event.mouse_x, event.mouse_y
        space = area.spaces.active
        for r in area.regions:
            flag = _UI_REGIONS.get(r.type)
            if flag and r.width > 1 and r.height > 1 and getattr(space, flag, True) \
                    and self._inside(r, mx, my):
                return None
        for r in area.regions:
            if r.type == 'WINDOW' and self._inside(r, mx, my):
                return r
        return None

    def _drag_region(self, area):
        if self.drag_region_xy is None:
            return None
        for r in area.regions:
            if r.type == 'WINDOW' and (r.x, r.y) == self.drag_region_xy:
                return r
        return None

    # -- data helpers ---------------------------------------------------------------------------------
    def _obj(self):
        obj = bpy.data.objects.get(self.obj_name)
        return obj if get_cloth(obj) is not None else None

    # -- pin / drag -----------------------------------------------------------------------------------
    def _begin_drag(self, context, region, coord, grab_only):
        obj = self._obj()
        hit = find_vertex_to_grab(context, obj, region, coord, _S["ids"])
        if hit is None:
            return
        vid, p0 = hit
        vg = obj.vertex_groups.get(pin_group_name(obj))
        key = get_pin_key(obj)
        if vg is None or key is None or vid >= len(key.data):     # e.g. after an undo
            vg, key, _changed = ensure_pin_setup(obj)
        walking = [m for m in _S["movers"] if m["vid"] == vid]
        _S["movers"][:] = [m for m in _S["movers"] if m["vid"] != vid]
        prev_w = weight_of(vg, vid)
        prev_co = walking[0]["target"].copy() if walking else key.data[vid].co.copy()
        # Pin the vertex where it is right now, so grabbing never makes the cloth jump.
        start = obj.matrix_world.inverted() @ p0
        key.data[vid].co = start
        vg.add([vid], 1.0, 'REPLACE')
        press_loc = view3d_utils.region_2d_to_location_3d(region, region.data, coord, p0)
        self.drag = {"vid": vid, "depth": p0.copy(), "offset": p0 - press_loc,
                     "prev_w": prev_w, "prev_co": prev_co, "grab_only": grab_only,
                     "cur": start.copy(), "target": start.copy(),
                     "limit": pull_step_limit(obj), "active": True}
        _S["movers"].append(self.drag)
        self.stop_when_settled = False
        self.drag_region_xy = (region.x, region.y)
        _S["ids"].add(vid)
        _S["drag_vid"] = vid
        self.play_until = 0.0
        self._play_start()
        _tag_ui_redraw()
        obj.update_tag()

    def _move_drag(self, region, coord):
        obj, d = self._obj(), self.drag
        if obj is None:
            return
        loc = view3d_utils.region_2d_to_location_3d(region, region.data, coord, d["depth"])
        if loc is not None:
            d["target"] = obj.matrix_world.inverted() @ (loc + d["offset"])

    def _end_drag(self, cancel):
        d, self.drag = self.drag, None
        self.drag_region_xy = None
        _S["drag_vid"] = None
        _tag_ui_redraw()
        obj = self._obj()
        if d is None or obj is None:
            self._play_stop()
            return
        key = get_pin_key(obj)
        vg = obj.vertex_groups.get(pin_group_name(obj))
        d["active"] = False                  # from here the pin only finishes its walk to the target
        if key is None or vg is None or d["vid"] >= len(key.data):
            _S["movers"][:] = [m for m in _S["movers"] if m is not d]
            self._play_stop()
            return
        if cancel or d["grab_only"]:
            if d["prev_w"] is None:
                vg.remove([d["vid"]])
                _S["ids"].discard(d["vid"])
                key.data[d["vid"]].co = rest_co(key, d["vid"])
                _S["movers"][:] = [m for m in _S["movers"] if m is not d]
            else:
                vg.add([d["vid"]], d["prev_w"], 'REPLACE')
                d["target"] = d["prev_co"]   # an older pin walks back to where it was
        # keep the cloth playing until the pin has arrived, then pause again
        if any(m is d for m in _S["movers"]):
            self.stop_when_settled = True
        else:
            self._play_stop()
        obj.update_tag()

    def _unpin(self, context, region, coord):
        obj = self._obj()
        hit = find_pin_to_remove(context, obj, region, coord, _S["ids"])
        if hit is None:
            return
        vid = hit[0]
        vg = obj.vertex_groups.get(pin_group_name(obj))
        key = get_pin_key(obj)
        if vg is None or weight_of(vg, vid) is None:
            return
        vg.remove([vid])
        _S["ids"].discard(vid)
        _S["movers"][:] = [m for m in _S["movers"] if m["vid"] != vid]
        if key is not None and vid < len(key.data):
            key.data[vid].co = rest_co(key, vid)
        obj.update_tag()
        self._play_start()                              # let the cloth fall for a moment
        self.play_until = time.monotonic() + UNPIN_RUN_SECONDS

    # -- events ---------------------------------------------------------------------------------------
    def modal(self, context, event):
        _S["tick"] = time.monotonic()
        area = context.area

        if _S["stop"] or area is None:
            return self._finish(context)
        if self._obj() is None:
            self.report({'WARNING'}, NO_CLOTH_MSG)
            return self._finish(context)

        et, val = event.type, event.value

        if et in {'TIMER', 'TIMER_REPORT'}:
            if et == 'TIMER':
                stepped = self._ensure_running(context)
                if DEBUG_PIN_GAP and self.drag is not None:
                    self._report_pin_gap(context)
                if _S["movers"]:
                    self._walk_pins(context, stepped)
                if self.stop_when_settled and not _S["movers"]:
                    self.stop_when_settled = False
                    self._play_stop()
                if self.drag is None and self.play_until and time.monotonic() > self.play_until:
                    self.play_until = 0.0
                    self._play_stop()
            return {'PASS_THROUGH'}
        if et == 'SPACE':                                # the user takes over playback: never auto-pause
            if val == 'PRESS':
                _S["we_started_play"] = False
                self.play_until = 0.0
                self.user_control = True         # Space toggles playback itself: the tool stays out of the way
            return {'PASS_THROUGH'}
        if et in _PASS_TYPES and not (et == 'MOUSEMOVE' and self.drag is not None):
            return {'PASS_THROUGH'}      # navigation, hover, space, etc. (mouse move is ours while dragging)
        if et.startswith('NUMPAD') and et != 'NUMPAD_ENTER':
            return {'PASS_THROUGH'}

        # exit / cancel
        if val == 'PRESS' and et == 'ESC':
            if self.drag is not None:
                self._end_drag(cancel=True)
                area.tag_redraw()
                return {'RUNNING_MODAL'}
            return self._finish(context)
        if val == 'PRESS' and et in {'RET', 'NUMPAD_ENTER', 'TAB'}:
            return self._finish(context)

        # drag in progress: follow the mouse, release to finish
        if self.drag is not None:
            region = self._drag_region(area)
            if region is None:
                self._end_drag(cancel=True)
                return {'RUNNING_MODAL'}
            coord = Vector((event.mouse_x - region.x, event.mouse_y - region.y))
            if et == 'MOUSEMOVE':
                self._move_drag(region, coord)
                area.tag_redraw()
                return {'RUNNING_MODAL'}
            if et == 'LEFTMOUSE' and val == 'RELEASE':
                self._end_drag(cancel=False)
                area.tag_redraw()
                return {'RUNNING_MODAL'}
            return {'RUNNING_MODAL'}

        if et in {'LEFTMOUSE', 'RIGHTMOUSE'}:
            region = self._viewport_region(area, event)
            if region is None or event.alt:
                return {'PASS_THROUGH'}
            if val == 'PRESS':
                coord = Vector((event.mouse_x - region.x, event.mouse_y - region.y))
                if et == 'LEFTMOUSE':
                    self._begin_drag(context, region, coord, grab_only=event.shift)
                else:
                    self._unpin(context, region, coord)
                area.tag_redraw()
            return {'RUNNING_MODAL'}

        if event.ctrl or event.oskey:
            return {'PASS_THROUGH'}
        if len(et) == 1 or et in _SWALLOW_TYPES:     # letters/numbers: never let X, A, G... hit the scene
            return {'RUNNING_MODAL'}
        return {'PASS_THROUGH'}

    def cancel(self, context):
        _cleanup_runtime(context)


# ---------------------------------------------------------------------------------------------------------
# Panel
# ---------------------------------------------------------------------------------------------------------
class PNPLITE_PT_main(bpy.types.Panel):
    bl_label = "PULLS'NPiNS ClothLite"
    bl_idname = "PNPLITE_PT_main"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "PULLS'NPiNS"

    @classmethod
    def poll(cls, context):
        return not full_addon_present()

    def draw(self, context):
        layout = self.layout
        running = _S["running"]
        obj = context.active_object
        ready = running or get_cloth(obj) is not None

        row = layout.row()
        row.scale_y = 1.6
        row.enabled = ready
        row.operator("pnp_lite.pull_tool",
                     text="Stop Pull Tool" if running else "Pull Tool",
                     icon_value=_swatch_icon_id(_button_color()), depress=running)

        if running:
            col = layout.column(align=True)
            col.label(text="LMB: grab and pin")
            col.label(text="Shift+LMB: grab only")
            col.label(text="RMB: unpin")
            col.label(text="Space: keep playing")
            col.label(text="Enter / Tab / Esc: exit")
        elif obj is None or obj.type != 'MESH':
            layout.label(text="Select a mesh with Cloth.", icon='INFO')
        elif not ready:
            col = layout.column(align=True)
            col.label(text="No Cloth simulation found.", icon='ERROR')
            col.label(text="Please create cloth sim first.")

        layout.separator()
        layout.label(text="Lite version - full: EverCloth")


# ---------------------------------------------------------------------------------------------------------
# Auto-off when the full add-on is present
# ---------------------------------------------------------------------------------------------------------
def _watch_for_full_addon():
    """Stay out of the way while the full EverCloth is enabled: the panel and the tool go dormant and a running
    pull ends. Nothing is disabled from a timer."""
    if not full_addon_present():
        _S["warned_full"] = False
        return 2.0
    if _S["running"]:
        _S["stop"] = True
        return 0.3
    if not _S["warned_full"]:
        _S["warned_full"] = True
        print("[PULLS'NPiNS ClothLite] EverCloth is enabled - the lite version stays dormant.")
    return 2.0


classes = (PNPLITE_OT_pull_tool, PNPLITE_PT_main)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    if not bpy.app.timers.is_registered(_accent_tick):
        bpy.app.timers.register(_accent_tick, first_interval=0.1, persistent=True)
    if not bpy.app.timers.is_registered(_watch_for_full_addon):
        bpy.app.timers.register(_watch_for_full_addon, first_interval=1.0, persistent=True)


def unregister():
    try:
        if bpy.app.timers.is_registered(_watch_for_full_addon):
            bpy.app.timers.unregister(_watch_for_full_addon)
    except Exception:
        pass
    try:
        if bpy.app.timers.is_registered(_accent_tick):
            bpy.app.timers.unregister(_accent_tick)
    except Exception:
        pass
    _cleanup_runtime()
    if _ICON["coll"] is not None:
        bpy.utils.previews.remove(_ICON["coll"])
        _ICON.update(coll=None, last=None)
    for cls in reversed(classes):
        try:
            bpy.utils.unregister_class(cls)
        except Exception:
            pass
