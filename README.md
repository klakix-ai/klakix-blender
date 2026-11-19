# Klakix Bridge for Blender (Blender MCP)

Let Claude, ChatGPT, Cursor, Claude Code or any MCP client work in your open Blender scene.
Klakix Bridge is a free, GPL-3.0 Blender add-on (Blender 4.2 or later) that connects Blender to the
hosted [Klakix MCP server](https://klakix.com/mcp). Your agent builds sets, adds lights and cameras,
animates, renders and places AI-generated images and videos, and looks at the result after every step.

- **One connector.** The same MCP URL you already use for image and video generation
  (`https://klakix.com/api/mcp`) also drives Blender. No second "bridge" server to add.
- **No local port, no Python setup.** The add-on keeps an outgoing HTTPS connection; nothing listens
  on your machine, so it works from claude.ai and chatgpt.com in the browser too.
- **Every batch is one Ctrl+Z.** Operations run in batches; a failing batch is rolled back atomically.
- **The agent sees what it did.** Each batch can return a viewport, camera or quick render picture.
- **Python is off by default.** Arbitrary code (`run_python`) only appears after you enable
  "Allow Python" in the panel.

Guide, comparison and FAQ: **https://klakix.com/blender-mcp**

## Install

1. Download `klakix-blender.zip` from the [latest release](https://github.com/klakix-ai/klakix-blender/releases/latest)
   (or from https://klakix.com/indir/klakix-blender.zip).
2. In Blender 4.2+: drag the zip into the window, or *Edit → Preferences → Get Extensions →
   ⌄ → Install from Disk*. Allow **Online Access** when Blender asks (the add-on needs the network).
3. In the 3D Viewport press **N**, open the **Klakix** tab and click **Connect**. Your browser opens;
   sign in and confirm the 8-character code shown in Blender.
4. Add the Klakix connector to your AI app if you haven't yet
   ([setup for Claude, ChatGPT, Cursor, Claude Code…](https://klakix.com/mcp)) and ask:
   *"Build a moody product shot of a perfume bottle in my Blender scene."*

The bridge tools don't use credits. Generating images, videos or 3D models for the scene uses your
Klakix credits as usual.

## What the agent can do

The agent calls `list_desktop_apps`, `get_app_operations`, `run_in_app` and `capture_app_view` on the
MCP server. The add-on currently exposes these operations:

| Area | Operations |
|---|---|
| Inspect | `scene_info`, `list_objects`, `object_info` |
| Build | `add_object` (primitives, text), `transform`, `rename_object`, `set_parent`, `set_visibility`, `delete_objects`, `add_modifier` |
| Look | `set_material` (color, metallic, roughness, emission), `add_light`, `set_world`, `polyhaven_search`, `polyhaven_apply` (free CC0 HDRIs, textures, models) |
| Camera and time | `add_camera`, `look_at`, `keyframe`, `set_frame_range` |
| Output | `set_render`, `save_file`, viewport / camera / render snapshots |
| Media | `import_media` (generated images and videos as textured planes; glTF, OBJ, FBX, USD and other models; HDR/EXR as world light) |
| Advanced | `run_python` (only after you allow it) |

The server-side catalog comes from the add-on itself (`klakix_bridge/ops.py`), so new operations show
up for the agent without a server change.

## How it works

```
AI agent ──MCP──▶ klakix.com/api/mcp ──▶ command hub ◀── long poll (HTTPS) ── Klakix Bridge (Blender)
                                                   ◀── results + snapshot ──
```

- Pairing is a device flow (like RFC 8628): the add-on shows a code, you approve it on klakix.com and
  the add-on receives a device token. The token is stored only in the add-on's user folder
  (`extensions/.user/…/klakix_bridge/config.json`), never in your .blend file, and you can revoke it
  any time at https://klakix.com/app/kopru.
- Network calls run on background threads; scene changes run on Blender's main thread through a
  timer, wrapped in an undo step. Downloads (Poly Haven, generated media) happen off the main thread
  so Blender doesn't freeze.
- If Online Access is disabled in Blender, the add-on does not connect.

## Develop and test

```bash
# Operation tests (60 checks), no network needed
blender -b --factory-startup --python tests/test_ops.py

# Validate and build the extension package
blender --command extension validate klakix_bridge
blender --command extension build --source-dir klakix_bridge --output-dir dist
```

`KLAKIX_BRIDGE_URL` overrides the server address for local development. Tested on Blender 4.2 LTS
and 5.2 LTS on Windows; the add-on is pure Python with no native parts.

## Related

- [klakix-ai/klakix-mcp](https://github.com/klakix-ai/klakix-mcp): open-source MCP packages (OAuth 2.1
  server, stateless MCP server, stdio bridge) behind the hosted Klakix MCP server.
- After Effects and Premiere Pro bridges are next, using the same protocol.

Problems or ideas: open an issue, or ask your agent to call `report_issue`; unexpected add-on errors
are reported to us automatically (the error message and stack trace).

## License

GPL-3.0-or-later, as required for Blender add-ons. © 2026 Klakix.
