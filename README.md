# PULLS'NPiNS ClothLite

**Click-drag pull and pin on existing Blender cloth simulations**

![Blender](https://img.shields.io/badge/Blender-4.2%2B-orange)
![License](https://img.shields.io/badge/license-GPL--3.0--or--later-blue)
![Type](https://img.shields.io/badge/Blender-Extension-green)

<!-- Add a demo GIF here, e.g.: ![Demo](docs/demo.gif) -->

## Overview

ClothLite is the free, minimal edition of PULLS'NPiNS EverCloth. It does one thing: select a mesh that
already has a **Cloth** modifier, start the **Pull Tool**, and click-drag any vertex to pull it around and pin
it in place while the simulation keeps running.

It does not create cloth for you. Set up your cloth simulation first (modifier, settings, collisions), then use
ClothLite to pose and pin it by hand.

## Features

- **Click-drag pulling**: grab a vertex, drag it, and the cloth follows like it is held by a hand.
- **Pin on release**: the vertex stays pinned where you let go.
- **Grab without pinning**: Shift+LMB pulls a vertex and lets it go again when you release.
- **Quick unpin**: RMB unpins the nearest pin, even from behind the cloth.
- **Smooth motion**: the pin moves a small step per simulated frame (at most 4 % of the mesh size), so the
  springs are not stretched past their limit and the cloth does not spike or jump.
- **Real cloth behaviour**: the cloth keeps its true rest shape while pulled (see
  [How it works](#how-it-works)), so it stretches like cloth instead of adopting the pulled pose.
- **Automatic playback**: the simulation runs while you hold or release a pin, then pauses again when things
  settle. Press Space to take over playback yourself.
- **Live overlay**: pinned vertices are drawn as orange squares, the vertex being pulled in lime.
- **No extra objects**: no Empties and no hooks, only a shape key, a vertex group and one empty modifier.
- **No dependencies**: pure Blender Python, plus the NumPy that ships with Blender.

## Requirements

- Blender **4.2** or newer
- Windows, macOS or Linux
- A mesh object with a Cloth modifier

## Installation

### Blender Extensions platform

Once published, open **Edit > Preferences > Get Extensions**, search for *PULLS'NPiNS ClothLite* and click
**Install**.

### From a downloaded zip (recommended for GitHub releases)

1. Download the latest `pullsnpins_clothlite-x.y.z.zip` from the
   [Releases](../../releases) page. Do not unzip it.
2. In Blender open **Edit > Preferences > Get Extensions**.
3. Open the dropdown (v) in the top right and choose **Install from Disk...**
   (or drag the zip onto the Blender window).
4. Select the zip and make sure the extension is enabled.

## Usage

1. Make sure your mesh has a Cloth modifier and is the **active object**, in **Object Mode**.
2. In the 3D Viewport press `N` to open the sidebar and go to the **PULLS'NPiNS** tab.
3. Click **Pull Tool**.
4. Click-drag on the cloth to pull and pin. Use the controls below.
5. Press `Enter`, `Tab` or `Esc` (or click **Stop Pull Tool**) to leave the tool.

The first time you start the tool on an object, ClothLite prepares the cloth (see
[What it changes](#what-it-changes-on-your-object)). If that changes the simulation setup, the timeline jumps
back to the first frame so the cloth restarts with the correct rest shape.

### Controls

| Input | Action |
|---|---|
| **LMB** drag | Grab the nearest vertex, pull it, pin it where you release |
| **Shift + LMB** drag | Grab and pull only. On release the vertex is unpinned (or returns to its earlier pin) |
| **RMB** | Unpin the nearest pin (works from behind the cloth, up to about 140 px away) |
| **Esc** while dragging | Cancel the drag, same result as Shift+LMB |
| **Space** | Take over playback. The tool stops auto-pausing |
| **Enter / Tab / Esc** | Exit the tool (Esc exits only when no drag is in progress) |
| **Middle mouse, scroll, Alt+click, Numpad** | Navigate the viewport as usual |

While the tool is running, letter and number keys are swallowed so you cannot accidentally press `X`, `A` or
`G` on your scene.

### Picking a vertex

A click grabs a vertex in this order:

1. An existing pin within about 28 px of the cursor (re-grabs that pin, even if hidden behind the cloth).
2. The vertex of the face under the cursor that is closest to the click.
3. The nearest vertex within about 70 px of the cursor if you clicked just off the cloth.

## What it changes on your object

Starting the tool adds, only where missing:

| Item | Name | Purpose |
|---|---|---|
| Vertex group | `PNP_Lite_Pins` (or your cloth's existing pin group) | Marks pinned vertices; assigned as the cloth's Pin Group |
| Shape key | `Basis` (only if the mesh had no shape keys) | Reference shape |
| Shape key | `PNP_Lite_Pin` | Carries the pin positions (value 1.0) |
| Shape key | `PNP_Lite_Rest` | Copy of the unpinned shape, value 0.0, used as the cloth's Rest Shape Key |
| Modifier | `PNP_OriginalCoords` | Empty Geometry Nodes modifier placed directly before Cloth |
| Custom property | `pnp_lite_setup` | Marks the object as a ClothLite setup |

It also turns **Dynamic Mesh** off (a dynamic mesh ignores the rest shape) and raises **Pin Stiffness** to 1.0
if it was lower, so the pin does not lag behind the mouse. While the tool runs, the scene's **Sync** mode is
set to *Play Every Frame* so the cloth never skips steps, and it is restored when the tool exits.

If your cloth already has a Pin Group, ClothLite uses it, and vertices already weighted in it show up as pins.

### Removing ClothLite's setup from an object

To go back to a plain cloth, delete the `PNP_Lite_Pin` and `PNP_Lite_Rest` shape keys, the
`PNP_OriginalCoords` modifier and (if you want) the `PNP_Lite_Pins` vertex group. Clear the Rest Shape Key and
Pin Group fields on the Cloth modifier if they still point at them.

## How it works

Blender's Cloth builds its springs from a rest shape. Two details make a pulled cloth misbehave:

- Cloth only reads the rest shape from a hidden "original coordinates" layer if something sits in front of
  it in the modifier stack. Without that, the rest shape silently becomes the *current, pulled* pose. ClothLite
  puts an empty Geometry Nodes modifier (`PNP_OriginalCoords`) directly in front of Cloth for this reason.
- If the rest shape key is the Basis key, Blender ignores it. ClothLite therefore keeps a separate
  `PNP_Lite_Rest` key that follows the Basis and never contributes to the mesh.

Pins are moved by the tool's own timer between frames, never from a frame handler, and never by teleporting.
That avoids both the sudden spring stretch and the extra solve that a mid-frame write would cause.

## Relationship to PULLS'NPiNS EverCloth

ClothLite uses the same modifier and key approach as the full EverCloth and marks its objects so EverCloth
can recognise and adopt them. When the full EverCloth is enabled, ClothLite goes dormant on its own: its panel
disappears, the tool cannot start, and a running pull ends. Nothing is disabled or uninstalled.

Hooks and Empties belong to the full EverCloth only.

## Known limitations

- Works on one mesh at a time, in **Object Mode** only.
- Needs an existing Cloth modifier; it never creates one.
- Changing the mesh topology after setup (adding or deleting vertices) invalidates the shape keys. Remove the
  ClothLite keys and start the tool again.
- The simulation is cached by Blender as usual. Pulling and pinning change the cloth, so bake or free the cache
  as you normally would.
- Because Dynamic Mesh is turned off, the cloth cannot use that feature while ClothLite's setup is active.

## Troubleshooting

**The Pull Tool button is greyed out or the panel is missing**
Select a mesh with a Cloth modifier in Object Mode. If the full EverCloth is enabled, ClothLite is dormant by
design.

**The cloth looks stretched or adopts the pulled shape**
Check that `PNP_OriginalCoords` sits directly above Cloth and that the Cloth Rest Shape Key is
`PNP_Lite_Rest`. Start the tool again to have ClothLite repair the setup.

**The simulation does not play while pulling**
ClothLite starts playback itself, and steps frames directly if Blender refuses. If you pressed Space, you are in
control: press Space again to run or pause.

**Pins stay in place after I exit**
That is intended: pins live in the vertex group and Pin Shape Key and stay until you unpin them with RMB, or
remove the setup as described above.

**Debug output**
Developers can set `DEBUG_PIN_GAP = True` near the top of `source/__init__.py` to print the gap between the pin and the
evaluated mesh to the console.

## Project structure

```
pullsnpins_clothlite/
├── source/                    <- the extension itself (this folder becomes the zip)
│   ├── __init__.py
│   ├── blender_manifest.toml
│   ├── LICENSE
│   └── README.md          <- packaged copy of this README (no repo-only sections)
├── docs/PUBLISHING.md         <- build, validate and upload checklist
├── .github/                   <- issue templates and the release workflow
├── CHANGELOG.md
├── LICENSE
└── README.md
```

## Development

1. Clone the repository.
2. Link or copy the `source/` folder into your Blender extensions directory, or build a zip and use
   **Install from Disk**.
3. Build and validate with Blender's command line tool (see
   [docs/PUBLISHING.md](docs/PUBLISHING.md)):

   ```
   blender --command extension validate source
   blender --command extension build --source-dir source --output-dir .
   ```

## Contributing

Bug reports and pull requests are welcome. Please use the issue templates and include your Blender version,
ClothLite version, OS and steps to reproduce. Open an issue first for larger changes.

## License

GPL-3.0-or-later. See [LICENSE](LICENSE).

## Credits

Author and maintainer: Raja-Muda
