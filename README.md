# cadMeshImport

Import **IFC**, **STEP**, **IGES** and **STL** files into Autodesk Maya as polygon meshes.

- **IFC (BIM)** – Maya has no IFC importer of its own. This one gives you one mesh per building element, grouped by storey, with shaders from the IFC colours and each element's `ifcGuid`, `ifcClass` and `ifcName` stored as attributes.
- **STEP / IGES** – keeps the assembly hierarchy, part names, colours, CAD layers and visibility, and repeated parts become Maya instances. Import as **polygon meshes** (exact CAD normals, quality presets) or as **trimmed NURBS surfaces** (the exact CAD geometry, one surface per CAD face).
- **STL** – binary and ASCII, multi-solid files, with welding, soft edges and unit and Z-up conversion. No dependencies.

Use it from its own importer dialog, the **CAD Import** menu, **File > Import**, or Python.

Tested with Maya 2027 on Windows.

## Install

1. Download this repository (green **Code** button → **Download ZIP**, or a release zip) and unzip it somewhere permanent, for example `Documents/maya/tools/cadMeshImport`.
2. Drag **`install.mel`** into a Maya viewport.
3. When asked, click **Install** to download STEP, IGES and IFC support. STL works without it.

The installer registers the folder as a Maya module, loads the plugin, sets it to auto-load and adds a **CAD** button to the current shelf. Don't move the folder afterwards; if you do, drag `install.mel` in again.

**Manual install:** copy `plug-ins/cadMeshImport.py` into `Documents/maya/<version>/plug-ins` and load it from **Windows > Settings/Preferences > Plug-in Manager**.

## STEP, IGES and IFC support

These formats need two Python packages for Maya's own Python:

| Formats | Package | Size |
|---|---|---|
| STEP, IGES | `cadquery-ocp` (OpenCASCADE) | large, a few hundred MB |
| IFC | `ifcopenshell` | moderate |

Install them from Maya with **CAD Import > Install STEP / IFC support**. The plugin also offers to install them the first time you import one of these formats. They go into `Documents/maya/cadMeshImport/python<X.Y>`, so you don't need Administrator rights, and Maya versions with different Python versions each get their own copy.

**By hand,** if the in-Maya install fails (run the terminal as Administrator):

```
"C:\Program Files\Autodesk\Maya2027\bin\mayapy.exe" -m pip install cadquery-ocp ifcopenshell
```

If pip reports *no matching distribution*, there's no prebuilt package yet for your Maya's Python version (check it with `import sys; print(sys.version)`). For STEP, `pythonocc-core` also works. For IFC, download a build from [ifcopenshell.org](https://ifcopenshell.org) and unzip it into the folder above.

## Usage

**Dialog:** click the **CAD** shelf button, use **CAD Import > Import STL / STEP / IGES / IFC...**, or run:

```python
import cadMeshImport
cadMeshImport.show_dialog()
```

Pick one or more files, set the options and click **Import**. Settings are remembered between sessions.

**File > Import:** choose `STLMeshImport`, `STEPCADImport`, `IGESCADImport` or `IFCBIMImport` as the file type.

**Python:**

```python
import cadMeshImport
cadMeshImport.import_stl("C:/parts/bracket.stl", scale=0.1)
cadMeshImport.import_step("C:/parts/gearbox.step", linearDeflection=0.05)
cadMeshImport.import_step("C:/parts/frame.igs", hierarchy=1)
cadMeshImport.import_step("C:/parts/housing.step", geometry=1)   # NURBS
cadMeshImport.import_ifc("C:/bim/site.ifc", groupByStorey=1)
```

### Options

| Option | Formats | Default | Meaning |
|---|---|---|---|
| `scale` | STL | 0.1 | Unit multiplier (0.1 = millimetres → Maya centimetres). STEP, IGES and IFC read their units from the file |
| `zUp` | all | 1 | Rotate Z-up CAD data to Maya's Y-up |
| `weld` | all | 1 | Merge coincident vertices |
| `smoothAngle` | all | 30 | Soften edges below this angle; -1 to skip. Not used when `cadNormals` is on |
| `geometry` | STEP, IGES | 0 | 0 = polygon meshes, 1 = trimmed NURBS surfaces |
| `hierarchy` | STEP, IGES | 0 | 0 = assembly hierarchy, 1 = flat (one object per part), 2 = single merged mesh (meshes only; NURBS use flat) |
| `instances` | STEP, IGES | 1 | Repeated parts (bolts, screws) share one mesh as Maya instances |
| `colors` | STEP, IGES | 1 | Create lambert shaders from CAD colours (per part and per face) |
| `cadNormals` | STEP, IGES | 1 | Meshes: exact normals from the CAD surfaces. Locks normals |
| `layers` | STEP, IGES | 1 | CAD layers become Maya display layers |
| `skipHidden` | STEP, IGES | 1 | Leave out parts hidden in the CAD file |
| `linearDeflection` | STEP, IGES | 0.1 | Meshes: max distance from the true surface in mm. Smaller = denser mesh |
| `angularDeflection` | STEP, IGES | 0.5 | Meshes: max angle between facets in radians. Smaller = smoother curves |
| `groupByStorey` | IFC | 1 | Group elements under their storey or space |
| `materials` | IFC | 1 | Create lambert shaders from IFC colours |
| `includeSpaces` | IFC | 0 | Also import IfcSpace room volumes |

The dialog's **Mesh quality** presets set the two deflection values: Coarse (0.5 mm, 0.8), Medium (0.1 mm, 0.5) and Fine (0.02 mm, 0.25).

### NURBS import

With **Import as: NURBS surfaces** (or `geometry=1`), each CAD face becomes a trimmed NURBS surface, rebuilt from the CAD file's exact control points, knots and trim curves. Each CAD face is its own surface object (`<part>_face0001`, `<part>_face0002`, ...), grouped under one transform per part, so a part still moves and instances as one group. Hierarchy, names, colours, layers and instancing work as they do for meshes.

Press **3** in the viewport for smoother NURBS display.

CAD files often have small flaws that Maya won't accept when trimming, so the importer repairs them first: trim curves that don't quite meet are joined, curves that stray slightly outside their surface are pulled back in, and cut-outs that run off the edge of their face are skipped so the rest of the face still trims. If OpenCASCADE can't convert a B-spline or flat face, the surface is used as it is. The import reports anything it skipped or couldn't trim.

STEP and IGES parts carry `cadName` and `cadInstance` attributes, and IFC elements carry `ifcGuid`, `ifcClass` and `ifcName`. They survive FBX export, so the data reaches Unreal and other engines.

## Compared with Maya's built-in importers

- **IFC:** Maya can't import IFC. This is the main reason to use cadMeshImport.
- **STEP / IGES:** Maya's Autodesk Translation Framework plugin (`ATFPlugin`) covers similar ground, as meshes or NURBS. What cadMeshImport adds is one dialog and one scripting API across every format, attribute tags on every part, and an open-source plugin you can adapt to your pipeline.
- **STL:** Maya's `stlTranslator` imports a single mesh. cadMeshImport splits multi-solid files and handles welding, soft edges, units and Z-up.

## Troubleshooting

- **"needs OpenCASCADE" / "needs IfcOpenShell"** – use **CAD Import > Install STEP / IFC support**. The Script Editor shows pip's output and the underlying import error.
- **A STEP file comes in without its hierarchy or colours** – the assembly reader hit a problem and the plugin fell back to a plain one-mesh-per-solid import. The Script Editor has the details; please open an issue with them.
- **NURBS faces come in inverted (the hole kept, the surface removed)** or **curved NURBS faces look bulged** – set `_FLIP_TRIM_LOOPS` or `_HOMOGENEOUS_CVS` to `True` near the top of the NURBS section in `cadMeshImport.py`, and please open an issue.
- **Something imports wrong** – run `cadMeshImport.diagnose(r"C:/path/to/file.step")` in the Script Editor. It prints the plugin version, every copy of the plugin Python can see, the OpenCASCADE version, and which stage of the STEP reader fails. Include that output in an issue.
- **Changes to the plugin don't show up** – Maya caches Python modules. Restart Maya, or use the shelf button, which reloads the module each time.
- **STL file type clashes with Maya's own STL importer** – pick `STLMeshImport` in the file type list.

## Uninstall

```python
import sys; sys.path.insert(0, r"<path to this folder>")
import installer; installer.uninstall()
```

or delete `cadMeshImport.mod` from `Documents/maya/modules` and remove the shelf button. Downloaded packages live in `Documents/maya/cadMeshImport` and can be deleted too.

## Licence

MIT – see [LICENSE](LICENSE). OpenCASCADE and IfcOpenShell are not included; they're downloaded separately under their own licences (LGPL).
