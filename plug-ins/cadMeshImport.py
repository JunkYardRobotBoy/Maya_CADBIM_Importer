"""
cadMeshImport.py - Maya plugin: import STL, STEP, IGES and IFC files as
polygon meshes.

Adds four file types to File > Import:
    STLMeshImport   (*.stl)          pure Python, no dependencies
    STEPCADImport   (*.step, *.stp)  needs OpenCASCADE (cadquery-ocp)
    IGESCADImport   (*.igs, *.iges)  needs OpenCASCADE (cadquery-ocp)
    IFCBIMImport    (*.ifc)          needs IfcOpenShell (ifcopenshell)

The STEP/IFC packages can be installed from inside Maya with
CAD Import > Install STEP / IFC support, or cadMeshImport.install_dependencies().
They go into Documents/maya/cadMeshImport/python<X.Y>, so no admin rights
are needed.

STEP and IGES keep the assembly hierarchy, part names, colours, CAD layers
and visibility, and instance repeated parts. They come in either as
polygon meshes with exact CAD normals or as trimmed NURBS surfaces.

Dialog:
    import cadMeshImport; cadMeshImport.show_dialog()
    (or run this file in the Script Editor, or use the "CAD Import" menu
    that appears when the plugin is loaded)

Scripting:
    import cadMeshImport
    cadMeshImport.import_stl("C:/parts/bracket.stl", scale=0.1)
    cadMeshImport.import_step("C:/parts/gearbox.step", linearDeflection=0.05)
    cadMeshImport.import_step("C:/parts/frame.igs", hierarchy=1)
    cadMeshImport.import_ifc("C:/bim/site.ifc", groupByStorey=1)

Options (option string "key=value;key=value" or keyword args):
    scale              unit multiplier, default 0.1 (mm -> Maya cm)
    zUp                1 = rotate result so CAD Z-up becomes Maya Y-up
    weld               1 = merge coincident vertices
    smoothAngle        polySoftEdge angle in degrees, -1 to skip
    linearDeflection   STEP/IGES: max distance from the true surface, mm
    angularDeflection  STEP/IGES: max angle between facets, radians
    geometry           STEP/IGES: 0 = polygon meshes, 1 = trimmed NURBS
                       surfaces (one object per CAD face, grouped per part)
    hierarchy          STEP/IGES: 0 = assembly hierarchy, 1 = flat parts,
                       2 = single merged mesh (meshes only)
    instances          STEP/IGES: 1 = repeated parts become Maya instances
    colors             STEP/IGES: 1 = create shaders from CAD colours
    cadNormals         STEP/IGES: 1 = exact surface normals (no soften pass)
    layers             STEP/IGES: 1 = CAD layers become display layers
    skipHidden         STEP/IGES: 1 = skip parts hidden in the CAD file
    includeSpaces      IFC only: 1 = also import IfcSpace volumes
    groupByStorey      IFC only: 1 = group elements under their storey/space
    materials          IFC only: 1 = create shaders from IFC colours

STEP and IGES geometry comes out of OpenCASCADE in millimetres and IFC
geometry out of IfcOpenShell in metres, so their scales default
to 0.1 and 100 (-> Maya cm) whatever the file's own units are.
Each IFC mesh carries ifcGuid, ifcClass and ifcName string attributes.
"""

import importlib
import importlib.util
import os
import re
import site
import struct
import subprocess
import sys
from types import SimpleNamespace

import maya.cmds as cmds
import maya.OpenMaya as OpenMaya
import maya.OpenMayaMPx as OpenMayaMPx
import maya.api.OpenMaya as om2

PLUGIN_VERSION = "1.4.4"
STL_TRANSLATOR = "STLMeshImport"
STEP_TRANSLATOR = "STEPCADImport"
IGES_TRANSLATOR = "IGESCADImport"
IFC_TRANSLATOR = "IFCBIMImport"

STL_DEFAULTS = {"scale": 0.1, "zUp": 1, "weld": 1, "smoothAngle": 30.0}
STEP_DEFAULTS = {"scale": 0.1, "zUp": 1, "weld": 1, "smoothAngle": 30.0,
                 "linearDeflection": 0.1, "angularDeflection": 0.5,
                 "hierarchy": 0, "instances": 1, "colors": 1,
                 "cadNormals": 1, "layers": 1, "skipHidden": 1,
                 "geometry": 0}
CAD_EXTENSIONS = (".step", ".stp", ".igs", ".iges")
IFC_DEFAULTS = {"scale": 100.0, "zUp": 1, "weld": 1, "smoothAngle": 30.0,
                "includeSpaces": 0, "groupByStorey": 1, "materials": 1}


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def _parse_options(option_string, defaults):
    opts = dict(defaults)
    for pair in (option_string or "").split(";"):
        if "=" not in pair:
            continue
        key, value = [s.strip() for s in pair.split("=", 1)]
        if key in opts:
            try:
                opts[key] = type(defaults[key])(float(value))
            except ValueError:
                pass
    return opts


def _safe_name(name):
    name = re.sub(r"[^A-Za-z0-9_]", "_", name) or "mesh"
    return "_" + name if name[0].isdigit() else name


class MeshBuilder(object):
    """Collects triangles, optionally welding coincident vertices."""

    def __init__(self, weld=True, precision=6):
        self.weld = weld
        self.precision = precision
        self.points = []
        self.tris = []
        self.mats = []  # material index per triangle, -1 = none
        self.fvn = []   # optional face-vertex normals, 3 per triangle
        self.normals_ok = True
        self._lookup = {}

    def add_vertex(self, x, y, z):
        if self.weld:
            key = (round(x, self.precision), round(y, self.precision),
                   round(z, self.precision))
            idx = self._lookup.get(key)
            if idx is not None:
                return idx
            self._lookup[key] = len(self.points)
        self.points.append((x, y, z))
        return len(self.points) - 1

    def add_face(self, a, b, c, mat=-1, normals=None):
        if a == b or b == c or a == c:  # skip degenerate triangles
            return
        self.tris.append((a, b, c))
        self.mats.append(mat)
        if normals is None:
            if self.fvn:
                self.normals_ok = False
            return
        if any(n is None for n in normals):
            g = self._tri_normal(a, b, c)
            normals = [n or g for n in normals]
        self.fvn.extend(normals)

    def _tri_normal(self, a, b, c):
        (ax, ay, az), (bx, by, bz), (cx, cy, cz) = (
            self.points[a], self.points[b], self.points[c])
        ux, uy, uz = bx - ax, by - ay, bz - az
        vx, vy, vz = cx - ax, cy - ay, cz - az
        nx, ny, nz = uy * vz - uz * vy, uz * vx - ux * vz, ux * vy - uy * vx
        mag = (nx * nx + ny * ny + nz * nz) ** 0.5
        return (nx / mag, ny / mag, nz / mag) if mag > 1e-20 else (0, 1, 0)

    def has_normals(self):
        return (self.normals_ok and bool(self.fvn)
                and len(self.fvn) == 3 * len(self.tris))

    def add_triangle(self, p1, p2, p3):
        self.add_face(self.add_vertex(*p1), self.add_vertex(*p2),
                      self.add_vertex(*p3))


def _face_ranges(indices):
    """[0,1,2,5,6] -> ["f[0:2]", "f[5:6]"] so cmds.sets gets short lists."""
    out, start, prev = [], None, None
    for i in indices:
        if start is None:
            start = prev = i
        elif i == prev + 1:
            prev = i
        else:
            out.append("f[%d:%d]" % (start, prev))
            start = prev = i
    if start is not None:
        out.append("f[%d:%d]" % (start, prev))
    return out


def _build_mesh(name, builder, scale, smooth_angle, parent,
                shading_groups=None):
    if not builder.tris:
        return None
    pts = om2.MFloatPointArray(
        [om2.MFloatPoint(x * scale, y * scale, z * scale)
         for x, y, z in builder.points])
    counts = om2.MIntArray(len(builder.tris), 3)
    connects = om2.MIntArray([i for tri in builder.tris for i in tri])

    fn = om2.MFnMesh()
    transform = fn.create(pts, counts, connects)
    has_normals = builder.has_normals()
    if has_normals:
        try:
            fn.setFaceVertexNormals(
                om2.MVectorArray([om2.MVector(*n) for n in builder.fvn]),
                om2.MIntArray([f for f in range(len(builder.tris))
                               for _ in (0, 1, 2)]),
                connects)
        except Exception as e:
            print("cadMeshImport: couldn't set CAD normals: %s" % e)
            has_normals = False
    path = cmds.rename(om2.MFnDagNode(transform).fullPathName(),
                       _safe_name(name))
    shape = cmds.listRelatives(path, shapes=True, fullPath=True)[0]
    _assign_shading(shape, builder.mats, shading_groups)
    if smooth_angle >= 0 and not has_normals:
        cmds.polySoftEdge(path, angle=smooth_angle, constructionHistory=False)
    return cmds.parent(path, parent)[0] if parent else path


def _assign_shading(shape, mats, shading_groups):
    by_sg = {}
    for face, m in enumerate(mats):
        sg = "initialShadingGroup"
        if shading_groups and 0 <= m < len(shading_groups) \
                and shading_groups[m]:
            sg = shading_groups[m]
        by_sg.setdefault(sg, []).append(face)
    if len(by_sg) <= 1:
        sg = next(iter(by_sg), "initialShadingGroup")
        cmds.sets(shape, edit=True, forceElement=sg)
        return
    for sg, faces in by_sg.items():
        comps = ["%s.%s" % (shape, r) for r in _face_ranges(faces)]
        cmds.sets(comps, edit=True, forceElement=sg)


def _make_group(path, z_up):
    base = os.path.splitext(os.path.basename(path))[0]
    group = cmds.group(empty=True, name=_safe_name(base))
    group = cmds.ls(group, long=True)[0]  # parts may reuse the file's name
    if z_up:
        cmds.setAttr(group + ".rotateX", -90)
    return group


# --------------------------------------------------------------------------
# STL
# --------------------------------------------------------------------------
def _is_binary_stl(data):
    if len(data) < 84:
        return False
    count = struct.unpack_from("<I", data, 80)[0]
    expected = 84 + count * 50
    if expected == len(data):
        return True
    # ASCII files start with "solid"; some binary exporters also do,
    # so only fall back to ASCII when the size check fails.
    return not data[:5].lower() == b"solid" and len(data) >= expected


def read_stl(path, weld=True):
    """Return a list of (name, MeshBuilder), one per solid in the file."""
    with open(path, "rb") as f:
        data = f.read()
    base = os.path.splitext(os.path.basename(path))[0]

    if _is_binary_stl(data):
        count = struct.unpack_from("<I", data, 80)[0]
        builder = MeshBuilder(weld)
        for rec in struct.iter_unpack("<12fH", data[84:84 + count * 50]):
            builder.add_triangle(rec[3:6], rec[6:9], rec[9:12])
        return [(base, builder)]

    solids, builder, name, loop = [], None, base, []
    for line in data.decode("utf-8", errors="ignore").splitlines():
        tokens = line.split()
        if not tokens:
            continue
        word = tokens[0].lower()
        if word == "solid":
            name = " ".join(tokens[1:]) or base
            builder = MeshBuilder(weld)
        elif word == "vertex":
            loop.append(tuple(float(v) for v in tokens[1:4]))
        elif word == "endloop":
            if builder is None:
                builder = MeshBuilder(weld)
            idx = [builder.add_vertex(*p) for p in loop]
            for i in range(1, len(idx) - 1):  # fan-triangulate n-gons
                builder.add_face(idx[0], idx[i], idx[i + 1])
            loop = []
        elif word == "endsolid" and builder is not None:
            solids.append((name, builder))
            builder = None
    if builder is not None and builder.tris:
        solids.append((name, builder))
    return solids


def import_stl(path, **kwargs):
    opts = dict(STL_DEFAULTS, **kwargs)
    solids = read_stl(path, weld=bool(opts["weld"]))
    group = _make_group(path, opts["zUp"])
    for name, builder in solids:
        _build_mesh(name, builder, opts["scale"], opts["smoothAngle"], group)
    cmds.select(group)
    return group


# --------------------------------------------------------------------------
# STEP (via OpenCASCADE)
# --------------------------------------------------------------------------
def _static(cls, name):
    """Older OCP builds suffix static methods with _s; newer ones don't."""
    return getattr(cls, name + "_s", None) or getattr(cls, name)


def _load_occ():
    try:  # cadquery-ocp
        from OCP.STEPControl import STEPControl_Reader
        from OCP.IGESControl import IGESControl_Reader
        from OCP.IFSelect import IFSelect_RetDone
        from OCP.BRepMesh import BRepMesh_IncrementalMesh
        from OCP.TopExp import TopExp_Explorer
        from OCP.TopAbs import TopAbs_SOLID, TopAbs_FACE, TopAbs_REVERSED
        from OCP.TopoDS import TopoDS
        from OCP.BRep import BRep_Tool
        from OCP.TopLoc import TopLoc_Location
        return SimpleNamespace(
            Reader=STEPControl_Reader, IGESReader=IGESControl_Reader,
            RetDone=IFSelect_RetDone,
            Mesh=BRepMesh_IncrementalMesh, Explorer=TopExp_Explorer,
            SOLID=TopAbs_SOLID, FACE=TopAbs_FACE, REVERSED=TopAbs_REVERSED,
            to_face=_static(TopoDS, "Face"),
            triangulation=_static(BRep_Tool, "Triangulation"),
            Location=TopLoc_Location)
    except ImportError as e:
        print("cadMeshImport: OCP import failed: %s" % e)
    try:  # pythonocc-core
        from OCC.Core.STEPControl import STEPControl_Reader
        from OCC.Core.IGESControl import IGESControl_Reader
        from OCC.Core.IFSelect import IFSelect_RetDone
        from OCC.Core.BRepMesh import BRepMesh_IncrementalMesh
        from OCC.Core.TopExp import TopExp_Explorer
        from OCC.Core.TopAbs import TopAbs_SOLID, TopAbs_FACE, TopAbs_REVERSED
        from OCC.Core import TopoDS as _topods
        from OCC.Core.BRep import BRep_Tool
        from OCC.Core.TopLoc import TopLoc_Location
        to_face = (getattr(getattr(_topods, "topods", None), "Face", None)
                   or getattr(_topods, "topods_Face"))
        return SimpleNamespace(
            Reader=STEPControl_Reader, IGESReader=IGESControl_Reader,
            RetDone=IFSelect_RetDone,
            Mesh=BRepMesh_IncrementalMesh, Explorer=TopExp_Explorer,
            SOLID=TopAbs_SOLID, FACE=TopAbs_FACE, REVERSED=TopAbs_REVERSED,
            to_face=to_face, triangulation=BRep_Tool.Triangulation,
            Location=TopLoc_Location)
    except ImportError as e:
        print("cadMeshImport: pythonocc import failed: %s" % e)
        return None


def read_step(path, linear_deflection=0.1, angular_deflection=0.5,
              weld=True):
    """Simple STEP/IGES tessellation without assembly data.
    Returns a list of (name, MeshBuilder). Used as a fallback."""
    occ = _load_occ()
    if occ is None:
        raise RuntimeError(
            "STEP/IGES import needs OpenCASCADE. Use CAD Import > Install "
            "STEP / IFC support, or run: mayapy -m pip install cadquery-ocp")

    is_iges = path.lower().endswith((".igs", ".iges"))
    reader = occ.IGESReader() if is_iges else occ.Reader()
    if reader.ReadFile(path) != occ.RetDone:
        raise RuntimeError("Could not read CAD file: %s" % path)
    reader.TransferRoots()
    shape = reader.OneShape()
    occ.Mesh(shape, linear_deflection, False, angular_deflection, True)

    parts = []
    exp = occ.Explorer(shape, occ.SOLID)
    while exp.More():
        parts.append(exp.Current())
        exp.Next()
    if not parts:  # surface-only files: treat the whole shape as one part
        parts = [shape]

    base = os.path.splitext(os.path.basename(path))[0]
    results = []
    for n, part in enumerate(parts, 1):
        builder = MeshBuilder(weld)
        fexp = occ.Explorer(part, occ.FACE)
        while fexp.More():
            face = occ.to_face(fexp.Current())
            fexp.Next()
            loc = occ.Location()
            tri = occ.triangulation(face, loc)
            if tri is None or (hasattr(tri, "IsNull") and tri.IsNull()):
                continue
            trsf = loc.Transformation()
            flip = face.Orientation() == occ.REVERSED
            idx = []
            for j in range(1, tri.NbNodes() + 1):
                p = tri.Node(j).Transformed(trsf)
                idx.append(builder.add_vertex(p.X(), p.Y(), p.Z()))
            for j in range(1, tri.NbTriangles() + 1):
                a, b, c = tri.Triangle(j).Get()
                if flip:
                    b, c = c, b
                builder.add_face(idx[a - 1], idx[b - 1], idx[c - 1])
        results.append(("%s_part%03d" % (base, n), builder))
    return results


def _import_step_simple(path, opts):
    parts = read_step(path, opts["linearDeflection"],
                      opts["angularDeflection"], bool(opts["weld"]))
    group = _make_group(path, opts["zUp"])
    for name, builder in parts:
        _build_mesh(name, builder, opts["scale"], opts["smoothAngle"], group)
    cmds.select(group)
    return group


# --------------------------------------------------------------------------
# STEP / IGES with assembly structure (OpenCASCADE XCAF)
# --------------------------------------------------------------------------
QUALITY_PRESETS = [("Coarse", 0.5, 0.8), ("Medium", 0.1, 0.5),
                   ("Fine", 0.02, 0.25), ("Custom", None, None)]
HIERARCHY_MODES = ["Assembly hierarchy", "Flat (one mesh per part)",
                   "Single merged mesh"]
_AUTO_NAME = re.compile(r"^(=>\[|NAUO\d*$|SOLID$|SHELL$|COMPOUND$|"
                        r"Open CASCADE STEP translator)", re.I)


class _Cancelled(Exception):
    pass


class _CadNode(object):
    """One node of the CAD assembly tree, independent of Maya."""

    def __init__(self):
        self.name = ""
        self.part_name = ""
        self.instance_name = ""
        self.location = None   # TopLoc_Location relative to the parent
        self.children = []
        self.proto = None      # prototype key for parts, None for assemblies
        self.color = None      # (r, g, b, transparency) or None
        self.layers = []

    def part_count(self):
        if self.proto is not None:
            return 1
        return sum(c.part_count() for c in self.children)


def _pick_name(*names):
    for n in names:
        if n and not _AUTO_NAME.match(n):
            return n
    for n in names:
        if n:
            return n
    return "part"


class _Xcaf(object):
    """Reads a STEP/IGES file into an XCAF document and walks it.
    Hides the differences between OCP and pythonocc and between OCP
    builds that do or don't suffix static methods with _s."""

    def __init__(self):
        prefix, error = None, None
        for candidate in ("OCP", "OCC.Core"):
            try:
                importlib.import_module(candidate + ".XCAFDoc")
                prefix = candidate
                break
            except ImportError as e:
                error = e
        if prefix is None:
            raise ImportError("OpenCASCADE not found (%s)" % error)

        self._prefix = prefix
        self._module_list = None
        mod = self._mod
        find = self._find

        xcaf = mod("XCAFDoc")
        self.ShapeTool = find("XCAFDoc_ShapeTool", "XCAFDoc")
        self.DocTool = find("XCAFDoc_DocumentTool", "XCAFDoc")
        color_type = find("XCAFDoc_ColorType", "XCAFDoc", required=False)

        def enum(name):
            value = getattr(xcaf, name, None)
            if value is None and color_type is not None:
                value = getattr(color_type, name, None)
            return value if value is not None else find(name, "XCAFDoc")

        self.SURF = enum("XCAFDoc_ColorSurf")
        self.GEN = enum("XCAFDoc_ColorGen")
        self.TDF_Label = find("TDF_Label", "TDF")
        self.TDF_Tool = find("TDF_Tool", "TDF")
        self.ChildIterator = find("TDF_ChildIterator", "TDF")
        # A typedef that some OCP builds don't publish; the walk below
        # works without it.
        self.LabelSequence = find("TDF_LabelSequence", "TDF", "NCollection",
                                  required=False)
        self.TDataStd_Name = find("TDataStd_Name", "TDataStd")
        self.ExtStr = find("TCollection_ExtendedString", "TCollection")
        self.AsciiStr = find("TCollection_AsciiString", "TCollection")
        self.Document = find("TDocStd_Document", "TDocStd")
        self.ColorRGBA = find("Quantity_ColorRGBA", "Quantity",
                              required=False)
        self.Color = find("Quantity_Color", "Quantity")
        self.STEPReader = find("STEPCAFControl_Reader", "STEPCAFControl")
        self.IGESReader = find("IGESCAFControl_Reader", "IGESCAFControl",
                               required=False)
        self.RetDone = find("IFSelect_RetDone", "IFSelect")
        self.Mesh = find("BRepMesh_IncrementalMesh", "BRepMesh")
        self._nurbs = None
        self.Explorer = find("TopExp_Explorer", "TopExp")
        self.SOLID = find("TopAbs_SOLID", "TopAbs")
        self.FACE = find("TopAbs_FACE", "TopAbs")
        self.REVERSED = find("TopAbs_REVERSED", "TopAbs")
        topods = mod("TopoDS")
        self.TopoDS_Shape = find("TopoDS_Shape", "TopoDS")
        cls = getattr(topods, "TopoDS", None)
        if cls is not None and (hasattr(cls, "Face_s") or hasattr(cls, "Face")):
            self.to_face = _static(cls, "Face")
            self.to_wire = _static(cls, "Wire")
        else:  # pythonocc
            legacy = getattr(topods, "topods", None)
            self.to_face = (getattr(legacy, "Face", None)
                            or getattr(topods, "topods_Face"))
            self.to_wire = (getattr(legacy, "Wire", None)
                            or getattr(topods, "topods_Wire"))
        self.BRep_Tool = find("BRep_Tool", "BRep")
        self.triangulation = _static(self.BRep_Tool, "Triangulation")
        self.Location = find("TopLoc_Location", "TopLoc")
        self.FaceProps = find("BRepGProp_Face", "BRepGProp", required=False)
        self.gp_Pnt = find("gp_Pnt", "gp")
        self.gp_Vec = find("gp_Vec", "gp")
        try:
            self.set_static_cval = _static(
                find("Interface_Static", "Interface"), "SetCVal")
        except Exception:
            self.set_static_cval = None
        self.protos = {}

    # -- finding OpenCASCADE classes --------------------------------------
    def _mod(self, name):
        return importlib.import_module(self._prefix + "." + name)

    def _all_modules(self):
        """Every OpenCASCADE submodule we can see, for names that a given
        build publishes somewhere other than the usual place."""
        if self._module_list is None:
            found = []
            try:
                root = importlib.import_module(self._prefix)
                for holder in (root, getattr(root, "OCP", None)):
                    if holder is None:
                        continue
                    for attr in dir(holder):
                        value = getattr(holder, attr, None)
                        if isinstance(value, type(sys)):
                            found.append(value)
            except Exception:
                pass
            for key, value in list(sys.modules.items()):
                if key.startswith(self._prefix + ".") and value is not None:
                    found.append(value)
            self._module_list = found
        return self._module_list

    def _find(self, name, *modules, **kwargs):
        """Look a class or enum value up in its usual module, then in any
        OpenCASCADE module. required=False returns None if it's missing."""
        for module in modules:
            try:
                value = getattr(self._mod(module), name, None)
            except ImportError:
                value = None
            if value is not None:
                return value
        for module in self._all_modules():
            value = getattr(module, name, None)
            if value is not None:
                return value
        if kwargs.get("required", True):
            raise ImportError("this OpenCASCADE build has no %s" % name)
        return None

    # -- reading ----------------------------------------------------------
    def read(self, path):
        if self.set_static_cval is not None:
            try:  # make sure geometry arrives in millimetres
                self.set_static_cval("xstep.cascade.unit", "MM")
            except Exception:
                pass
        doc = self.Document(self.ExtStr("XmlOcaf"))
        is_iges = path.lower().endswith((".igs", ".iges"))
        if is_iges and self.IGESReader is None:
            raise RuntimeError("this OpenCASCADE build can't read IGES")
        reader = self.IGESReader() if is_iges else self.STEPReader()
        for mode in ("SetColorMode", "SetNameMode", "SetLayerMode"):
            try:
                getattr(reader, mode)(True)
            except Exception:
                pass
        if reader.ReadFile(path) != self.RetDone:
            raise RuntimeError("Could not read CAD file: %s" % path)
        if not reader.Transfer(doc):
            raise RuntimeError("Could not transfer CAD data from %s" % path)
        self.doc = doc  # keep the document alive while we use its labels
        main = doc.Main()
        self.shape_tool = _static(self.DocTool, "ShapeTool")(main)
        self.color_tool = _static(self.DocTool, "ColorTool")(main)
        try:
            self.layer_tool = _static(self.DocTool, "LayerTool")(main)
        except Exception:
            self.layer_tool = None

    # -- label helpers ----------------------------------------------------
    def _st(self, name):
        return _static(self.ShapeTool, name)

    def _labels(self, seq):
        return [seq.Value(i) for i in range(1, seq.Length() + 1)]

    def _children(self, label, test):
        """Child labels that pass an XCAFDoc_ShapeTool test such as
        IsComponent. This is what GetFreeShapes, GetComponents and
        GetSubShapes do internally, minus the label sequence."""
        out = []
        it = self.ChildIterator(label, False)
        while it.More():
            child = it.Value()
            it.Next()
            try:
                if self._st(test)(child):
                    out.append(child)
            except Exception:
                pass
        return out

    def free_labels(self):
        if self.LabelSequence is not None:
            try:
                seq = self.LabelSequence()
                self.shape_tool.GetFreeShapes(seq)
                return self._labels(seq)
            except Exception:
                pass
        return self._children(self.shape_tool.Label(), "IsFree")

    def component_labels(self, label):
        if self.LabelSequence is not None:
            try:
                seq = self.LabelSequence()
                self._st("GetComponents")(label, seq, False)
                return self._labels(seq)
            except Exception:
                pass
        return self._children(label, "IsComponent")

    def has_subshapes(self, label):
        if self.LabelSequence is not None:
            try:
                seq = self.LabelSequence()
                self._st("GetSubShapes")(label, seq)
                return seq.Length() > 0
            except Exception:
                pass
        return bool(self._children(label, "IsSubShape"))

    def name(self, label):
        attr = self.TDataStd_Name()
        try:
            if label.FindAttribute(_static(self.TDataStd_Name, "GetID")(),
                                   attr):
                return self.AsciiStr(attr.Get()).ToCString()
        except Exception:
            pass
        return ""

    def entry(self, label):
        text = self.AsciiStr()
        _static(self.TDF_Tool, "Entry")(label, text)
        return text.ToCString()

    def shape(self, label):
        get = self._st("GetShape")
        try:
            return get(label)
        except TypeError:
            shape = self.TopoDS_Shape()
            get(label, shape)
            return shape

    def visible(self, label):
        try:
            return bool(self.color_tool.IsVisible(label))
        except Exception:
            return True

    def color(self, item):
        """Colour of a label or shape as (r, g, b, transparency), or None."""
        for ctype in (self.SURF, self.GEN):
            try:
                if self.ColorRGBA is None:
                    raise TypeError("no Quantity_ColorRGBA")
                rgba = self.ColorRGBA()
                if self.color_tool.GetColor(item, ctype, rgba):
                    rgb = rgba.GetRGB()
                    return (round(rgb.Red(), 4), round(rgb.Green(), 4),
                            round(rgb.Blue(), 4), round(1.0 - rgba.Alpha(), 4))
            except Exception:
                try:
                    col = self.Color()
                    if self.color_tool.GetColor(item, ctype, col):
                        return (round(col.Red(), 4), round(col.Green(), 4),
                                round(col.Blue(), 4), 0.0)
                except Exception:
                    pass
        return None

    def layers(self, label):
        if self.layer_tool is None:
            return []
        try:
            seq = self.layer_tool.GetLayers(label)
            return [self.AsciiStr(seq.Value(i)).ToCString()
                    for i in range(1, seq.Length() + 1)]
        except Exception:
            return []

    # -- tree -------------------------------------------------------------
    def tree(self, skip_hidden=True, use_colors=True):
        roots = []
        for label in self.free_labels():
            node = self._walk(label, None, skip_hidden, use_colors)
            if node is not None:
                roots.append(node)
        return roots

    def _walk(self, label, inherited, skip_hidden, use_colors):
        if skip_hidden and not self.visible(label):
            return None
        proto, location = label, None
        if self._st("IsReference")(label):
            proto = self.TDF_Label()
            self._st("GetReferredShape")(label, proto)
            location = self._st("GetLocation")(label)
            if skip_hidden and not self.visible(proto):
                return None

        node = _CadNode()
        node.instance_name = self.name(label)
        node.part_name = self.name(proto) if proto is not label else \
            node.instance_name
        node.name = _pick_name(node.part_name, node.instance_name)
        node.location = location
        node.layers = self.layers(label)
        if proto is not label:
            node.layers += [n for n in self.layers(proto)
                            if n not in node.layers]

        color = inherited
        if use_colors:  # instance colour beats part colour beats parent's
            color = (self.color(label)
                     or (self.color(proto) if proto is not label else None)
                     or inherited)

        if self._st("IsAssembly")(proto):
            for comp in self.component_labels(proto):
                child = self._walk(comp, color, skip_hidden, use_colors)
                if child is not None:
                    node.children.append(child)
            return node if node.children else None

        key = self.entry(proto)
        self.protos[key] = proto
        node.proto = key
        node.color = color
        return node

    # -- tessellation -----------------------------------------------------
    def _colored_faces(self, label, shape, base_color, use_colors):
        """[(face, colour)] for every face of a part. Colours can sit on
        the part, on its solids or on single faces."""
        sub_colors = False
        if use_colors:
            try:
                sub_colors = self.has_subshapes(label)
            except Exception:
                pass
        out = []

        def faces(container, base, avoid=None):
            exp = (self.Explorer(container, self.FACE) if avoid is None
                   else self.Explorer(container, self.FACE, avoid))
            while exp.More():
                face = self.to_face(exp.Current())
                exp.Next()
                color = base
                if sub_colors:
                    color = self.color(face) or base
                out.append((face, color))

        if sub_colors:
            exp = self.Explorer(shape, self.SOLID)
            while exp.More():
                solid = exp.Current()
                exp.Next()
                faces(solid, self.color(solid) or base_color)
            faces(shape, base_color, avoid=self.SOLID)
        else:
            faces(shape, base_color)
        return out

    def mesh(self, key, base_color, linear, angular, weld=True,
             cad_normals=True, use_colors=True):
        """Mesh one part. Returns (MeshBuilder, palette); each triangle's
        material index points into palette, a list of colours."""
        label = self.protos[key]
        shape = self.shape(label)
        self.Mesh(shape, linear, False, angular, True)
        builder = MeshBuilder(weld)
        palette = []
        for face, color in self._colored_faces(label, shape, base_color,
                                               use_colors):
            mat = -1
            if color is not None:
                if color not in palette:
                    palette.append(color)
                mat = palette.index(color)
            self._add_face(builder, face, mat, cad_normals)
        return builder, palette

    # -- NURBS ------------------------------------------------------------
    def _nurbs_api(self):
        if self._nurbs is None:
            find = self._find
            self._nurbs = SimpleNamespace(
                Convert=find("BRepBuilderAPI_NurbsConvert", "BRepBuilderAPI"),
                AdaptorSurface=find("BRepAdaptor_Surface", "BRepAdaptor"),
                AdaptorCurve2d=find("BRepAdaptor_Curve2d", "BRepAdaptor"),
                BSPLINE=find("GeomAbs_BSplineSurface", "GeomAbs"),
                PLANE=find("GeomAbs_Plane", "GeomAbs"),
                TrimmedSurface=find("Geom_RectangularTrimmedSurface", "Geom"),
                to_bspline_surface=_static(
                    find("GeomConvert", "GeomConvert"),
                    "SurfaceToBSplineSurface"),
                to_bspline_curve2d=_static(
                    find("Geom2dConvert", "Geom2dConvert"),
                    "CurveToBSplineCurve"),
                Trimmed2d=find("Geom2d_TrimmedCurve", "Geom2d"),
                WireExplorer=find("BRepTools_WireExplorer", "BRepTools"),
                WIRE=find("TopAbs_WIRE", "TopAbs"),
                surface_of=_static(self.BRep_Tool, "Surface"),
                curve_on_surface=_static(self.BRep_Tool, "CurveOnSurface"))
        return self._nurbs

    def nurbs(self, key, base_color, use_colors=True):
        """NURBS data for every face of one part:
        [((surface, loops), colour)] as plain Python data (see
        _face_nurbs). Faces that can't be converted are skipped."""
        api = self._nurbs_api()
        label = self.protos[key]
        shape = self.shape(label)
        out, failed = [], 0
        for index, (face, color) in enumerate(self._colored_faces(
                label, shape, base_color, use_colors)):
            try:
                data = self._face_nurbs(api, face)
            except Exception as e:
                failed += 1
                if failed == 1:
                    print("cadMeshImport: NURBS conversion failed on a face "
                          "of %s: %s" % (self.name(label) or key, e))
                continue
            if data is not None:
                out.append((data, color, index))
        if failed:
            cmds.warning("cadMeshImport: %d face(s) of %s couldn't be "
                         "converted to NURBS and were skipped."
                         % (failed, self.name(label) or key))
        return out

    def _face_nurbs(self, api, face):
        """One CAD face -> (surface, loops).
        surface = (cvs, uKnots, vKnots, uDegree, vDegree, rational) with
                  cvs as (x, y, z, w), V varying fastest, Maya-style knots.
        loops   = [[curve, ...], ...], outer loop first, each curve
                  (cvs as (u, v, w), knots, degree, rational).
        Reversed faces get their U direction flipped so the Maya surface
        normal points the same way as the CAD face."""
        try:
            converted = api.Convert(face, True).Shape()
            exp = self.Explorer(converted, self.FACE)
            if not exp.More():
                return None
            cface = self.to_face(exp.Current())
        except Exception as convert_error:
            # The converter can choke on a face's edges. B-spline and
            # plane faces don't need converting, so use them as they are:
            # their trim curves are already in the right UV space.
            kind = api.AdaptorSurface(face).GetType()
            if kind not in (api.BSPLINE, api.PLANE):
                raise convert_error
            cface = face

        adaptor = api.AdaptorSurface(cface)
        kind = adaptor.GetType()
        if kind == api.BSPLINE:
            bs = adaptor.BSpline()
        elif kind == api.PLANE:  # bound the infinite plane to the face
            bs = api.to_bspline_surface(api.TrimmedSurface(
                api.surface_of(cface),
                adaptor.FirstUParameter(), adaptor.LastUParameter(),
                adaptor.FirstVParameter(), adaptor.LastVParameter()))
        else:
            bs = api.to_bspline_surface(api.surface_of(cface))
        if bs.IsUPeriodic():
            bs.SetUNotPeriodic()
        if bs.IsVPeriodic():
            bs.SetVNotPeriodic()

        nu, nv = bs.NbUPoles(), bs.NbVPoles()
        rational = bool(bs.IsURational() or bs.IsVRational())
        grid = []
        for i in range(1, nu + 1):
            row = []
            for j in range(1, nv + 1):
                p = bs.Pole(i, j)
                w = bs.Weight(i, j) if rational else 1.0
                row.append((p.X(), p.Y(), p.Z(), w))
            grid.append(row)
        u_knots = _maya_knots([bs.UKnot(i) for i in
                               range(1, bs.NbUKnots() + 1)],
                              [bs.UMultiplicity(i) for i in
                               range(1, bs.NbUKnots() + 1)])
        v_knots = _maya_knots([bs.VKnot(i) for i in
                               range(1, bs.NbVKnots() + 1)],
                              [bs.VMultiplicity(i) for i in
                               range(1, bs.NbVKnots() + 1)])

        loops = []
        wexp = self.Explorer(cface, api.WIRE)
        while wexp.More():
            wire = self.to_wire(wexp.Current())
            wexp.Next()
            curves, samples = [], []
            edges = api.WireExplorer(wire, cface)
            while edges.More():
                edge = edges.Current()
                edges.Next()
                result = self._pcurve(api, edge, cface)
                if result is not None:
                    curves.append(result[0])
                    samples.extend(result[1])
            if curves:
                loops.append([curves, samples])

        if face.Orientation() == self.REVERSED:
            grid.reverse()
            lo, hi = u_knots[0], u_knots[-1]
            u_knots = [lo + hi - k for k in reversed(u_knots)]
            for loop in loops:
                loop[0] = [([(lo + hi - u, v, w) for u, v, w in c[0]],)
                           + tuple(c[1:]) for c in loop[0]]
                loop[1] = [(lo + hi - u, v) for u, v in loop[1]]

        domain = (u_knots[0], u_knots[-1], v_knots[0], v_knots[-1])
        for loop in loops:
            loop[0] = _close_loop(loop[0], domain)
            loop[1] = _loop_samples(loop[0])  # what Maya will actually see

        cvs = [cv for row in grid for cv in row]
        surface = (cvs, u_knots, v_knots, bs.UDegree(), bs.VDegree(),
                   rational, nu, nv)
        return surface, _orient_loops(loops)

    def _pcurve(self, api, edge, face):
        """The edge's 2D curve in the face's UV space, as Maya-style curve
        data, plus a few sample points for working out loop direction."""
        adaptor = api.AdaptorCurve2d(edge, face)
        first, last = adaptor.FirstParameter(), adaptor.LastParameter()
        if abs(last - first) < 1e-12:
            return None
        try:
            curve = adaptor.Curve()
        except Exception:
            curve = api.curve_on_surface(edge, face, first, last)
        try:  # edge ranges can overrun a non-periodic curve slightly
            if not curve.IsPeriodic():
                first = max(first, curve.FirstParameter())
                last = min(last, curve.LastParameter())
        except Exception:
            pass
        if last - first < 1e-12:
            return None
        bs = api.to_bspline_curve2d(api.Trimmed2d(curve, first, last))
        if bs.IsPeriodic():
            bs.SetNotPeriodic()
        if edge.Orientation() == self.REVERSED:
            bs.Reverse()
        rational = bool(bs.IsRational())
        cvs = []
        for i in range(1, bs.NbPoles() + 1):
            p = bs.Pole(i)
            cvs.append((p.X(), p.Y(), bs.Weight(i) if rational else 1.0))
        knots = _maya_knots([bs.Knot(i) for i in range(1, bs.NbKnots() + 1)],
                            [bs.Multiplicity(i)
                             for i in range(1, bs.NbKnots() + 1)])
        t0, t1 = bs.FirstParameter(), bs.LastParameter()
        samples = []
        for k in range(8):
            p = bs.Value(t0 + (t1 - t0) * k / 8.0)
            samples.append((p.X(), p.Y()))
        return (cvs, knots, bs.Degree(), rational), samples

    def _add_face(self, builder, face, mat, cad_normals):
        loc = self.Location()
        tri = self.triangulation(face, loc)
        if tri is None or (hasattr(tri, "IsNull") and tri.IsNull()):
            return
        trsf = loc.Transformation()
        flip = face.Orientation() == self.REVERSED
        count = tri.NbNodes()
        idx = []
        for j in range(1, count + 1):
            p = tri.Node(j).Transformed(trsf)
            idx.append(builder.add_vertex(p.X(), p.Y(), p.Z()))
        normals = self._face_normals(face, tri, count) if cad_normals \
            else None
        if cad_normals and normals is None:
            builder.normals_ok = False
        for j in range(1, tri.NbTriangles() + 1):
            a, b, c = tri.Triangle(j).Get()
            if flip:
                b, c = c, b
            n = None
            if normals is not None:
                n = (normals[a - 1], normals[b - 1], normals[c - 1])
            builder.add_face(idx[a - 1], idx[b - 1], idx[c - 1], mat, n)

    def _face_normals(self, face, tri, count):
        """Exact surface normals at each triangulation node. BRepGProp_Face
        already accounts for face orientation and location."""
        try:
            if self.FaceProps is None or not tri.HasUVNodes():
                return None
            props = self.FaceProps(face)
            p, v = self.gp_Pnt(), self.gp_Vec()
            out = []
            for j in range(1, count + 1):
                uv = tri.UVNode(j)
                props.Normal(uv.X(), uv.Y(), p, v)
                mag = v.Magnitude()
                out.append((v.X() / mag, v.Y() / mag, v.Z() / mag)
                           if mag > 1e-12 else None)  # None at poles/apexes
            return out
        except Exception:
            return None


# Maya NURBS CVs are passed as (x, y, z, w) with x, y, z the actual position.
# If rational shapes (cylinders, fillets) come in bulged, set this to True
# to pass homogeneous (w*x, w*y, w*z, w) instead.
_HOMOGENEOUS_CVS = False
# Outer trim loop counter-clockwise in UV, holes clockwise. If trimmed
# faces come in inverted (hole kept, surface removed), set this to True.
_FLIP_TRIM_LOOPS = False


def _maya_knots(knots, mults):
    """OpenCASCADE knots + multiplicities -> Maya knot vector, which has
    one fewer knot at each end."""
    flat = []
    for k, m in zip(knots, mults):
        flat.extend([k] * m)
    return flat[1:-1]


def _signed_area(points):
    area = 0.0
    for i in range(len(points)):
        x1, y1 = points[i]
        x2, y2 = points[(i + 1) % len(points)]
        area += x1 * y2 - x2 * y1
    return area / 2.0


def _reverse_curve(curve):
    cvs, knots = curve[0], curve[1]
    lo, hi = knots[0], knots[-1]
    return (list(reversed(cvs)), [lo + hi - k for k in reversed(knots)]) \
        + tuple(curve[2:])


def _close_loop(curves, domain, clamp_fraction=0.02):
    """Repair a trim loop for Maya, which refuses loops that don't close
    exactly or that stray outside the surface: snap each pair of meeting
    curve ends to their midpoint, and pull control points that overshoot
    the UV range by up to clamp_fraction of its size back onto it.
    CAD files often store edge curves that only meet to within the
    modeller's tolerance."""
    u0, u1, v0, v1 = domain
    du, dv = (u1 - u0) * clamp_fraction, (v1 - v0) * clamp_fraction

    def clamp(cv):
        u, v, w = cv
        if u0 - du <= u < u0:
            u = u0
        elif u1 < u <= u1 + du:
            u = u1
        if v0 - dv <= v < v0:
            v = v0
        elif v1 < v <= v1 + dv:
            v = v1
        return (u, v, w)

    curves = [([clamp(cv) for cv in c[0]],) + tuple(c[1:]) for c in curves]
    n = len(curves)
    if n == 1:  # a single closed curve: make its ends coincide
        cvs = curves[0][0]
        if len(cvs) > 2:
            mid = ((cvs[0][0] + cvs[-1][0]) / 2.0, (cvs[0][1] + cvs[-1][1]) / 2.0)
            cvs[0] = (mid[0], mid[1], cvs[0][2])
            cvs[-1] = (mid[0], mid[1], cvs[-1][2])
        return curves
    for i in range(n):
        a = curves[i][0]
        b = curves[(i + 1) % n][0]
        mid = ((a[-1][0] + b[0][0]) / 2.0, (a[-1][1] + b[0][1]) / 2.0)
        a[-1] = (mid[0], mid[1], a[-1][2])
        b[0] = (mid[0], mid[1], b[0][2])
    return curves


_DROPPED_HOLES = [0]  # holes skipped because they weren't inside the face


def _inside(point, polygon):
    x, y = point
    inside = False
    j = len(polygon) - 1
    for i in range(len(polygon)):
        xi, yi = polygon[i]
        xj, yj = polygon[j]
        if (yi > y) != (yj > y) and \
                x < (xj - xi) * (y - yi) / ((yj - yi) or 1e-300) + xi:
            inside = not inside
        j = i
    return inside


def _eval_curve(cvs, knots, degree, t):
    """Point on a Maya-style 2D NURBS curve (de Boor). cvs are (u, v, w)."""
    k = [knots[0]] + list(knots) + [knots[-1]]
    n = len(cvs)
    span = degree
    while span < n - 1 and not (k[span] <= t < k[span + 1]):
        span += 1
    pts = [[cvs[j][0] * cvs[j][2], cvs[j][1] * cvs[j][2], cvs[j][2]]
           for j in range(span - degree, span + 1)]
    for r in range(1, degree + 1):
        for j in range(degree, r - 1, -1):
            i = j + span - degree
            den = k[i + degree - r + 1] - k[i]
            a = 0.0 if den == 0 else (t - k[i]) / den
            pts[j] = [(1 - a) * pts[j - 1][q] + a * pts[j][q]
                      for q in range(3)]
    x, y, w = pts[degree]
    return (x / w, y / w) if w else (x, y)


def _loop_samples(curves, per_curve=12):
    out = []
    for cvs, knots, degree, _ in curves:
        if len(cvs) <= degree:
            out.extend((u, v) for u, v, _w in cvs)
            continue
        a, b = knots[0], knots[-1]
        for i in range(per_curve):
            out.append(_eval_curve(cvs, knots, degree,
                                   a + (b - a) * i / float(per_curve)))
    return out


def _segments_cross(p1, p2, p3, p4):
    def cross(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])
    d1, d2 = cross(p3, p4, p1), cross(p3, p4, p2)
    d3, d4 = cross(p1, p2, p3), cross(p1, p2, p4)
    return d1 * d2 < 0 and d3 * d4 < 0


def _hole_fits(samples, outer):
    """True if a hole loop lies inside the outer loop without crossing
    it. Some CAD files have cut-outs that run off the edge of their face;
    Maya then fails to tessellate the whole surface once there's more
    than one hole, and the face vanishes."""
    if not samples or not outer:
        return True
    hu = [u for u, _ in samples]
    hv = [v for _, v in samples]
    box = (min(hu), max(hu), min(hv), max(hv))
    hole_edges = [(samples[i], samples[(i + 1) % len(samples)])
                  for i in range(len(samples))]
    for i in range(len(outer)):
        a, b = outer[i], outer[(i + 1) % len(outer)]
        if max(a[0], b[0]) < box[0] or min(a[0], b[0]) > box[1] or \
                max(a[1], b[1]) < box[2] or min(a[1], b[1]) > box[3]:
            continue
        for c, d in hole_edges:
            if _segments_cross(a, b, c, d):
                return False
    outside = sum(1 for pt in samples if not _inside(pt, outer))
    return outside <= len(samples) * 0.25


def _orient_loops(loops):
    """[[curves, samples], ...] -> [curves, ...], outer loop (largest
    area) first and counter-clockwise, holes clockwise. Holes that don't
    sit inside the outer loop are dropped (counted in _DROPPED_HOLES)."""
    if not loops:
        return []
    areas = [_signed_area(samples) for _, samples in loops]
    outer = max(range(len(loops)), key=lambda i: abs(areas[i]))
    holes = []
    for i in range(len(loops)):
        if i == outer:
            continue
        if _hole_fits(loops[i][1], loops[outer][1]):
            holes.append(i)
        else:
            _DROPPED_HOLES[0] += 1
    order = [outer] + holes
    result = []
    for n, i in enumerate(order):
        curves = loops[i][0]
        want_ccw = (n == 0) != _FLIP_TRIM_LOOPS
        if (areas[i] > 0) != want_ccw:
            curves = [_reverse_curve(c) for c in reversed(curves)]
        result.append(curves)
    return result


def _mpoint(x, y, z, w):
    if _HOMOGENEOUS_CVS:
        return OpenMaya.MPoint(x * w, y * w, z * w, w)
    return OpenMaya.MPoint(x, y, z, w)


def _double_array(values):
    arr = OpenMaya.MDoubleArray()
    for v in values:
        arr.append(v)
    return arr


def _mobject(node):
    sel = OpenMaya.MSelectionList()
    sel.add(node)
    obj = OpenMaya.MObject()
    sel.getDependNode(0, obj)
    return obj


def _create_nurbs_surface(surface, loops, scale, parent_obj):
    """Build one trimmed NURBS surface shape under parent_obj. Returns
    (shape path, trimmed ok)."""
    cvs, u_knots, v_knots, u_deg, v_deg, rational, nu, nv = surface
    points = OpenMaya.MPointArray()
    for x, y, z, w in cvs:
        points.append(_mpoint(x * scale, y * scale, z * scale, w))
    fn = OpenMaya.MFnNurbsSurface()
    shape_obj = fn.create(points, _double_array(u_knots),
                          _double_array(v_knots), u_deg, v_deg,
                          OpenMaya.MFnNurbsSurface.kOpen,
                          OpenMaya.MFnNurbsSurface.kOpen, rational,
                          parent_obj)
    shape = OpenMaya.MFnDagNode(shape_obj).fullPathName()
    if not loops:
        return shape, True

    boundaries = OpenMaya.MTrimBoundaryArray()
    for loop in loops:
        boundary = OpenMaya.MObjectArray()
        for c_cvs, c_knots, c_deg, c_rational in loop:
            data = OpenMaya.MFnNurbsCurveData().create()
            pts = OpenMaya.MPointArray()
            for u, v, w in c_cvs:
                pts.append(_mpoint(u, v, 0.0, w))
            OpenMaya.MFnNurbsCurve().create(
                pts, _double_array(c_knots), c_deg,
                OpenMaya.MFnNurbsCurve.kOpen, True, c_rational, data)
            boundary.append(data)
        boundaries.append(boundary)
    fn.setObject(shape_obj)
    attempts = (lambda: fn.trimWithBoundaries(boundaries),
                lambda: fn.trimWithBoundaries(boundaries, False, 1e-2, 1e-2),
                lambda: fn.setTrimBoundaries(boundaries))
    first_error = None
    for attempt in attempts:
        try:
            attempt()
            return shape, True
        except Exception as e:
            first_error = first_error or e
    print("cadMeshImport: trim failed on %s: %s" % (shape, first_error))
    return shape, False


def _face_name(part, index):
    return "%s_face%04d" % (_safe_name(part), index + 1)


def _build_nurbs_part(name, faces, scale, parent, shaders):
    """One part as a group holding a separate NURBS surface object per
    CAD face. Returns (group, [(face name, shape, shading group)],
    untrimmed count)."""
    t = _new_transform(name, parent)
    shapes, untrimmed = [], 0
    for position, item in enumerate(faces):
        (surface, loops), color = item[0], item[1]
        index = item[2] if len(item) > 2 else position
        face_name = _face_name(name, index)
        face_t = _new_transform(face_name, t)
        try:
            shape, trimmed = _create_nurbs_surface(surface, loops, scale,
                                                   _mobject(face_t))
        except Exception as e:
            print("cadMeshImport: couldn't build %s: %s" % (face_name, e))
            cmds.delete(face_t)
            continue
        untrimmed += 0 if trimmed else 1
        sg = "initialShadingGroup"
        if shaders is not None and color is not None:
            sg = shaders.get(_color_name(color), color[:3], color[3])
        cmds.sets(shape, edit=True, forceElement=sg)
        shapes.append((face_name, shape, sg))
    return t, shapes, untrimmed


def _loc_matrix(location, scale):
    """TopLoc_Location -> om2.MMatrix (Maya row-vector convention)."""
    if location is None:
        return om2.MMatrix()
    v = location.Transformation().Value
    return om2.MMatrix([v(1, 1), v(2, 1), v(3, 1), 0.0,
                        v(1, 2), v(2, 2), v(3, 2), 0.0,
                        v(1, 3), v(2, 3), v(3, 3), 0.0,
                        v(1, 4) * scale, v(2, 4) * scale, v(3, 4) * scale,
                        1.0])


def _set_matrix(node, matrix):
    if matrix == om2.MMatrix():
        return
    cmds.xform(node, objectSpace=True,
               matrix=[matrix.getElement(r, c)
                       for r in range(4) for c in range(4)])


def _color_name(color):
    r, g, b = [max(0, min(255, int(round(c * 255)))) for c in color[:3]]
    name = "cad_%02x%02x%02x" % (r, g, b)
    return name + ("_t%02d" % int(color[3] * 100) if color[3] > 0 else "")


def _new_transform(name, parent):
    node = cmds.createNode("transform", name=_safe_name(name), parent=parent,
                           skipSelect=True)
    return cmds.ls(node, long=True)[0]


def _import_xcaf(xc, path, opts, group):
    xc.read(path)
    roots = xc.tree(bool(opts["skipHidden"]), bool(opts["colors"]))
    if not roots:
        raise RuntimeError("No visible shapes found in %s" % path)

    scale = opts["scale"]
    as_nurbs = int(opts.get("geometry", 0)) == 1
    _DROPPED_HOLES[0] = 0
    mode = int(opts["hierarchy"])
    if as_nurbs and mode == 2:
        mode = 1  # NURBS can't be merged into one mesh; keep parts flat
    instancing = bool(opts["instances"]) and mode != 2
    shaders = _ShaderCache() if opts["colors"] else None
    use_layers = bool(opts["layers"])
    meshes = {}    # (part key, colour) -> (MeshBuilder, shading groups)
    shapes = {}    # (part key, colour) -> first mesh shape, for instancing
    nurbs_data = {}  # (part key, colour) -> face data
    untrimmed = [0]
    built = []
    layer_nodes = {}
    total = max(1, sum(r.part_count() for r in roots))
    done = [0]

    show_progress = not cmds.about(batch=True)
    if show_progress:
        cmds.progressWindow(title="CAD import", progress=0, maxValue=total,
                            status="Reading assembly...", isInterruptable=True)

    def tick(name):
        done[0] += 1
        if show_progress:
            cmds.progressWindow(edit=True, progress=done[0],
                                status="%d / %d  %s" % (done[0], total,
                                                        name[:40]))
            if cmds.progressWindow(query=True, isCancelled=True):
                raise _Cancelled()

    def add_layers(node_path, names):
        if not use_layers:
            return
        for name in names:
            layer = layer_nodes.get(name)
            if layer is None:
                layer = cmds.createDisplayLayer(
                    name=_safe_name("cad_" + name), empty=True,
                    noRecurse=True)
                layer_nodes[name] = layer
            cmds.editDisplayLayerMembers(layer, node_path, noRecurse=True)

    def get_mesh(node):
        key = (node.proto, node.color)
        if key not in meshes:
            builder, palette = xc.mesh(
                node.proto, node.color, opts["linearDeflection"],
                opts["angularDeflection"], bool(opts["weld"]),
                bool(opts["cadNormals"]), bool(opts["colors"]))
            sgs = None
            if shaders is not None:
                sgs = [shaders.get(_color_name(c), c[:3], c[3])
                       for c in palette]
            meshes[key] = (builder, sgs)
        return key, meshes[key]

    def place_nurbs(node, parent):
        key = (node.proto, node.color)
        if instancing and key in shapes:
            t = _new_transform(node.name, parent)
            for face_name, shape, sg in shapes[key]:
                face_t = _new_transform(face_name, t)
                new = cmds.parent(shape, face_t, addObject=True, shape=True)
                cmds.sets(cmds.ls(new, long=True)[0], edit=True,
                          forceElement=sg)
            return t
        if key not in nurbs_data:
            nurbs_data[key] = xc.nurbs(node.proto, node.color,
                                       bool(opts["colors"]))
        if not nurbs_data[key]:
            return None
        t, made, bad = _build_nurbs_part(node.name, nurbs_data[key], scale,
                                         parent, shaders)
        untrimmed[0] += bad
        shapes.setdefault(key, made)
        return t

    def place(node, parent, parent_world):
        local = _loc_matrix(node.location, scale)
        world = local * parent_world
        if node.proto is None:  # assembly
            if mode == 0:
                t = _new_transform(node.name, parent)
                _set_matrix(t, local)
                _tag(t, cadName=node.part_name,
                     cadInstance=node.instance_name)
                add_layers(t, node.layers)
                for child in node.children:
                    place(child, t, om2.MMatrix())
            else:
                for child in node.children:
                    place(child, parent, world)
            return

        if as_nurbs:
            t = place_nurbs(node, parent)
            if t is not None:
                _set_matrix(t, local if mode == 0 else world)
                _tag(t, cadName=node.part_name,
                     cadInstance=node.instance_name)
                add_layers(t, node.layers)
                built.append(t)
            tick(node.name)
            return

        key, (builder, sgs) = get_mesh(node)
        if instancing and key in shapes:
            t = _new_transform(node.name, parent)
            cmds.parent(shapes[key], t, addObject=True, shape=True)
            inst = cmds.listRelatives(t, shapes=True, fullPath=True)[0]
            _assign_shading(inst, builder.mats, sgs)
        else:
            t = _build_mesh(node.name, builder, scale, opts["smoothAngle"],
                            parent, sgs)
            if t is None:
                tick(node.name)
                return
            t = cmds.ls(t, long=True)[0]
            shapes.setdefault(
                key, cmds.listRelatives(t, shapes=True, fullPath=True)[0])
        _set_matrix(t, local if mode == 0 else world)
        _tag(t, cadName=node.part_name, cadInstance=node.instance_name)
        add_layers(t, node.layers)
        built.append(t)
        tick(node.name)

    try:
        for root in roots:
            place(root, group, om2.MMatrix())
    except _Cancelled:
        cmds.warning("cadMeshImport: import cancelled; keeping the parts "
                     "built so far.")
    finally:
        if show_progress:
            cmds.progressWindow(endProgress=True)

    if _DROPPED_HOLES[0]:
        cmds.warning("cadMeshImport: skipped %d trim hole(s) that stick out "
                     "past their face's edge in the CAD data; the faces "
                     "keep their other holes." % _DROPPED_HOLES[0])
    if untrimmed[0]:
        cmds.warning("cadMeshImport: %d NURBS face(s) couldn't be trimmed "
                     "and were left untrimmed. Details in the Script Editor."
                     % untrimmed[0])

    if mode == 2 and len(built) > 1:
        base = os.path.splitext(os.path.basename(path))[0]
        merged = cmds.polyUnite(built, constructionHistory=False,
                                mergeUVSets=1,
                                name=_safe_name(base + "_merged"))[0]
        cmds.parent(merged, group)
        for child in cmds.listRelatives(group, children=True,
                                        fullPath=True) or []:
            if not cmds.listRelatives(child, allDescendents=True):
                cmds.delete(child)


def import_step(path, **kwargs):
    """Import a STEP or IGES file, keeping assembly structure, names,
    colours and layers. Falls back to a plain per-solid import if the
    assembly reader isn't available or fails."""
    opts = dict(STEP_DEFAULTS, **kwargs)
    print("cadMeshImport %s: importing %s as %s (from %s)"
          % (PLUGIN_VERSION, os.path.basename(path),
             "NURBS" if int(opts.get("geometry", 0)) == 1 else "meshes",
             _source_file()))
    try:
        xc = _Xcaf()
    except Exception as e:
        import traceback
        traceback.print_exc()
        return _step_fallback(path, opts, "assembly reader unavailable: %s"
                              % e)

    group = _make_group(path, False)  # rotate after building (see merge)
    try:
        _import_xcaf(xc, path, opts, group)
    except Exception as e:
        import traceback
        traceback.print_exc()
        if cmds.objExists(group):
            cmds.delete(group)
        return _step_fallback(path, opts, "%s: %s" % (type(e).__name__, e))
    if opts["zUp"]:
        cmds.setAttr(group + ".rotateX", -90)
    cmds.select(group)
    return group


def _source_file():
    """Where this code was loaded from. Maya runs plug-in files without
    defining __file__, so ask Maya in that case."""
    path = globals().get("__file__")
    if path:
        return path
    try:
        return cmds.pluginInfo("cadMeshImport", query=True, path=True)
    except Exception:
        return "unknown location"


def _step_fallback(path, opts, reason):
    """The assembly/NURBS reader failed. Never swap NURBS for polygons
    silently: ask first in the UI, refuse in batch."""
    name = os.path.basename(path)
    print("cadMeshImport: assembly import failed for %s: %s" % (name, reason))
    if int(opts.get("geometry", 0)) == 1:
        if cmds.about(batch=True):
            raise RuntimeError("NURBS import failed for %s: %s"
                               % (name, reason))
        answer = cmds.confirmDialog(
            title="cadMeshImport",
            message=("NURBS import failed for %s:\n\n%s\n\nThe full "
                     "error is in the Script Editor. Import it as plain "
                     "polygons instead (no hierarchy, names or colours)?"
                     % (name, reason[:400])),
            button=["Import polygons", "Cancel"],
            defaultButton="Cancel", cancelButton="Cancel",
            dismissString="Cancel")
        if answer != "Import polygons":
            raise RuntimeError("NURBS import failed for %s: %s"
                               % (name, reason))
    else:
        cmds.warning("cadMeshImport: assembly import failed for %s; "
                     "importing plain polygons instead. Details in the "
                     "Script Editor." % name)
    return _import_step_simple(path, opts)


_OCC_NAMES = (
    ("BRepAdaptor_Curve2d", "BRepAdaptor"),
    ("BRepAdaptor_Surface", "BRepAdaptor"),
    ("BRepBuilderAPI_NurbsConvert", "BRepBuilderAPI"),
    ("BRepGProp_Face", "BRepGProp"),
    ("BRepMesh_IncrementalMesh", "BRepMesh"),
    ("BRepTools_WireExplorer", "BRepTools"),
    ("BRep_Tool", "BRep"),
    ("Geom2dConvert", "Geom2dConvert"),
    ("Geom2d_TrimmedCurve", "Geom2d"),
    ("GeomAbs_BSplineSurface", "GeomAbs"),
    ("GeomConvert", "GeomConvert"),
    ("IFSelect_RetDone", "IFSelect"),
    ("IGESCAFControl_Reader", "IGESCAFControl"),
    ("Interface_Static", "Interface"),
    ("Quantity_Color", "Quantity"),
    ("Quantity_ColorRGBA", "Quantity"),
    ("STEPCAFControl_Reader", "STEPCAFControl"),
    ("TCollection_AsciiString", "TCollection"),
    ("TCollection_ExtendedString", "TCollection"),
    ("TDF_ChildIterator", "TDF"),
    ("TDF_Label", "TDF"),
    ("TDF_LabelSequence", "TDF"),
    ("TDF_Tool", "TDF"),
    ("TDataStd_Name", "TDataStd"),
    ("TDocStd_Document", "TDocStd"),
    ("TopAbs_FACE", "TopAbs"),
    ("TopAbs_REVERSED", "TopAbs"),
    ("TopAbs_SOLID", "TopAbs"),
    ("TopAbs_WIRE", "TopAbs"),
    ("TopExp_Explorer", "TopExp"),
    ("TopLoc_Location", "TopLoc"),
    ("TopoDS_Shape", "TopoDS"),
    ("XCAFDoc_ColorType", "XCAFDoc"),
    ("XCAFDoc_DocumentTool", "XCAFDoc"),
    ("XCAFDoc_ShapeTool", "XCAFDoc"),
    ("gp_Pnt", "gp"),
    ("gp_Vec", "gp"),
)


def _occ_probe():
    """Check every OpenCASCADE name this plugin uses; returns the missing
    ones. Static methods are checked with and without the _s suffix."""
    prefix = None
    for candidate in ("OCP", "OCC.Core"):
        try:
            importlib.import_module(candidate)
            prefix = candidate
            break
        except ImportError:
            pass
    if prefix is None:
        return None, ["OpenCASCADE itself"]
    probe = object.__new__(_Xcaf)
    probe._prefix, probe._module_list = prefix, None
    missing = [n for n, m in _OCC_NAMES
               if probe._find(n, m, required=False) is None]
    statics = (("XCAFDoc_ShapeTool", ("IsReference", "GetReferredShape",
                                      "GetLocation", "IsAssembly", "IsFree",
                                      "IsComponent", "IsSubShape", "GetShape")),
               ("XCAFDoc_DocumentTool", ("ShapeTool", "ColorTool")),
               ("TDF_Tool", ("Entry",)), ("TDataStd_Name", ("GetID",)),
               ("BRep_Tool", ("Triangulation", "Surface")))
    for cls_name, methods in statics:
        cls = probe._find(cls_name, required=False)
        for meth in methods:
            if cls is not None and getattr(cls, meth + "_s", None) is None \
                    and getattr(cls, meth, None) is None:
                missing.append("%s.%s" % (cls_name, meth))
    return prefix, missing


def diagnose(path=None):
    """Print what this plugin can see and, for a STEP/IGES file, run each
    stage of the assembly and NURBS readers on its own, so a failure
    shows exactly where it happens. Paste the output into a bug report."""
    import traceback
    print("=" * 70)
    print("cadMeshImport %s from %s" % (PLUGIN_VERSION, _source_file()))
    print("Python %s" % sys.version.split()[0])
    for mod in ("OCP", "OCC", "ifcopenshell"):
        try:
            spec = importlib.util.find_spec(mod)
        except Exception:
            spec = None
        print("  %-14s %s" % (mod, spec.origin if spec else "not found"))
    try:
        import importlib.metadata as md
        for dist in ("cadquery-ocp", "pythonocc-core", "ifcopenshell"):
            try:
                print("  %-14s %s" % (dist, md.version(dist)))
            except Exception:
                pass
    except ImportError:
        pass
    prefix, missing = _occ_probe()
    if prefix:
        print("  OpenCASCADE names used: %d, missing: %s"
              % (len(_OCC_NAMES), ", ".join(missing) or "none"))
    for folder in sys.path:
        if folder and os.path.isfile(os.path.join(folder, "cadMeshImport.py")):
            print("  copy on sys.path: %s"
                  % os.path.join(folder, "cadMeshImport.py"))
    if path is None:
        print("=" * 70)
        return

    def step(label, func):
        try:
            result = func()
            print("  OK    %s" % label)
            return result
        except Exception:
            print("  FAIL  %s" % label)
            traceback.print_exc()
            raise

    print("File: %s" % path)
    try:
        xc = step("load OpenCASCADE XCAF", _Xcaf)
        step("read file", lambda: xc.read(path))
        roots = step("walk assembly", lambda: xc.tree(True, True))

        def parts(nodes):
            for n in nodes:
                if n.proto is not None:
                    yield n
                for p in parts(n.children):
                    yield p
        found = list(parts(roots))
        print("        %d root(s), %d part instance(s), %d unique part(s)"
              % (len(roots), len(found), len(xc.protos)))
        if not found:
            return
        first = found[0]
        print("        first part: %r colour %r" % (first.name, first.color))
        step("mesh first part",
             lambda: xc.mesh(first.proto, first.color, 0.1, 0.5))
        api = step("load NURBS converters", xc._nurbs_api)
        label = xc.protos[first.proto]
        shape = xc.shape(label)
        faces = step("list faces", lambda: xc._colored_faces(
            label, shape, first.color, True))
        print("        %d face(s)" % len(faces))
        ok = bad = 0
        for i, (face, _) in enumerate(faces[:25]):
            try:
                surface, loops = xc._face_nurbs(api, face)
                ok += 1
                if i < 3:
                    print("        face %d: %dx%d CVs, degree %d/%d, "
                          "rational %s, %d trim loop(s)"
                          % (i, surface[6], surface[7], surface[3],
                             surface[4], surface[5], len(loops)))
            except Exception:
                bad += 1
                if bad == 1:
                    print("  FAIL  NURBS conversion of face %d:" % i)
                    traceback.print_exc()
        print("  %s  NURBS conversion: %d ok, %d failed (first %d faces)"
              % ("OK  " if not bad else "FAIL", ok, bad,
                 min(25, len(faces))))
    except Exception:
        pass
    finally:
        print("=" * 70)


import_iges = import_step


# --------------------------------------------------------------------------
# IFC (via IfcOpenShell)
# --------------------------------------------------------------------------
IFC_SKIP_ALWAYS = ("IfcOpeningElement", "IfcVirtualElement")


def _load_ifcopenshell():
    try:
        import ifcopenshell
        import ifcopenshell.geom
        return ifcopenshell
    except ImportError as e:
        print("cadMeshImport: IfcOpenShell import failed: %s" % e)
        return None


def _ifc_settings(geom):
    """World-coordinate triangulation; option names differ by version."""
    settings = geom.settings()
    try:  # IfcOpenShell 0.8+
        settings.set("use-world-coords", True)
    except Exception:  # 0.7
        settings.set(settings.USE_WORLD_COORDS, True)
    return settings


def _attr(obj, name, default=None):
    """Read an attribute that is a property in one version, a method in another."""
    value = getattr(obj, name, default)
    return value() if callable(value) else value


def _ifc_material(mat):
    """Return (name, (r, g, b), transparency) for an IfcOpenShell material."""
    name = _attr(mat, "name", "") or _attr(mat, "original_name", "") or ""
    rgb = (0.8, 0.8, 0.8)
    diffuse = _attr(mat, "diffuse")
    if diffuse is not None:
        try:
            if callable(getattr(diffuse, "r", None)):
                rgb = (diffuse.r(), diffuse.g(), diffuse.b())
            else:
                rgb = tuple(float(c) for c in list(diffuse)[:3])
        except Exception:
            pass
    transparency = _attr(mat, "transparency", 0.0) or 0.0
    if transparency != transparency:  # NaN means "not set"
        transparency = 0.0
    return name, rgb, float(transparency)


class _ShaderCache(object):
    def __init__(self):
        self._cache = {}

    def get(self, name, rgb, transparency):
        key = (name, tuple(round(c, 4) for c in rgb), round(transparency, 4))
        sg = self._cache.get(key)
        if sg:
            return sg
        shader = cmds.shadingNode("lambert", asShader=True,
                                  name=_safe_name(name or "ifcMaterial"))
        cmds.setAttr(shader + ".color", *rgb, type="double3")
        if transparency > 0:
            cmds.setAttr(shader + ".transparency", transparency,
                         transparency, transparency, type="double3")
        sg = cmds.sets(renderable=True, noSurfaceShader=True, empty=True,
                       name=shader + "SG")
        cmds.connectAttr(shader + ".outColor", sg + ".surfaceShader")
        self._cache[key] = sg
        return sg


def _tag(node, **attrs):
    for key, value in attrs.items():
        if not cmds.attributeQuery(key, node=node, exists=True):
            cmds.addAttr(node, longName=key, dataType="string")
        cmds.setAttr("%s.%s" % (node, key), value or "", type="string")


def _container_of(element):
    try:
        import ifcopenshell.util.element as ue
        return ue.get_container(element)
    except Exception:
        for rel in getattr(element, "ContainedInStructure", None) or ():
            return rel.RelatingStructure
    return None


def import_ifc(path, **kwargs):
    opts = dict(IFC_DEFAULTS, **kwargs)
    ifcopenshell = _load_ifcopenshell()
    if ifcopenshell is None:
        raise RuntimeError(
            "IFC import needs IfcOpenShell. Use CAD Import > Install "
            "STEP / IFC support, or run: mayapy -m pip install ifcopenshell")
    import multiprocessing

    ifc = ifcopenshell.open(path)
    settings = _ifc_settings(ifcopenshell.geom)
    iterator = ifcopenshell.geom.iterator(settings, ifc,
                                          multiprocessing.cpu_count())
    if not iterator.initialize():
        raise RuntimeError("No geometry could be generated from %s" % path)

    group = _make_group(path, opts["zUp"])
    shaders = _ShaderCache() if opts["materials"] else None
    containers = {}
    show_progress = not cmds.about(batch=True)
    if show_progress:
        cmds.progressWindow(title="IFC import", progress=0, maxValue=100,
                            status="Building meshes...", isInterruptable=True)
    count = 0
    try:
        while True:
            shape = iterator.get()
            element = ifc.by_id(shape.id)
            ifc_class = element.is_a()
            skip = ifc_class in IFC_SKIP_ALWAYS or (
                ifc_class == "IfcSpace" and not opts["includeSpaces"])
            if not skip:
                geo = shape.geometry
                verts, faces = geo.verts, geo.faces
                mat_ids = list(geo.material_ids) or [-1] * (len(faces) // 3)
                builder = MeshBuilder(bool(opts["weld"]))
                idx = [builder.add_vertex(verts[i], verts[i + 1],
                                          verts[i + 2])
                       for i in range(0, len(verts), 3)]
                for t in range(0, len(faces), 3):
                    builder.add_face(idx[faces[t]], idx[faces[t + 1]],
                                     idx[faces[t + 2]], mat_ids[t // 3])

                sgs = None
                if shaders is not None:
                    sgs = [shaders.get(*_ifc_material(m))
                           for m in geo.materials]

                parent = group
                if opts["groupByStorey"]:
                    container = _container_of(element)
                    if container is not None:
                        parent = containers.get(container.id())
                        if parent is None:
                            label = container.Name or container.is_a()
                            parent = cmds.group(empty=True, parent=group,
                                                name=_safe_name(label))
                            parent = cmds.ls(parent, long=True)[0]
                            containers[container.id()] = parent
                            _tag(parent, ifcGuid=container.GlobalId,
                                 ifcClass=container.is_a(),
                                 ifcName=container.Name)

                name = "%s_%s" % (ifc_class[3:],
                                  element.Name or element.id())
                node = _build_mesh(name, builder, opts["scale"],
                                   opts["smoothAngle"], parent, sgs)
                if node:
                    _tag(node, ifcGuid=getattr(element, "GlobalId", ""),
                         ifcClass=ifc_class, ifcName=element.Name)
                    count += 1

            if show_progress:
                cmds.progressWindow(edit=True, progress=iterator.progress(),
                                    status="%d elements" % count)
                if cmds.progressWindow(query=True, isCancelled=True):
                    cmds.warning("cadMeshImport: IFC import cancelled.")
                    break
            if not iterator.next():
                break
    finally:
        if show_progress:
            cmds.progressWindow(endProgress=True)
    cmds.select(group)
    return group


# --------------------------------------------------------------------------
# Python dependencies (installed into a per-user folder, no admin needed)
# --------------------------------------------------------------------------
DEPENDENCIES = (("STEP / IGES", ("OCP", "OCC"), "cadquery-ocp"),
                ("IFC", ("ifcopenshell",), "ifcopenshell"))
_dll_handles = []


def _deps_dir():
    try:
        base = cmds.internalVar(userAppDir=True)
    except Exception:
        base = os.environ.get("MAYA_APP_DIR") or os.path.join(
            os.path.expanduser("~"),
            "Documents/maya" if os.name == "nt" else "maya")
    return os.path.normpath(os.path.join(
        base, "cadMeshImport", "python%d.%d" % sys.version_info[:2]))


def _add_deps_path():
    path = _deps_dir()
    if not os.path.isdir(path) or path in sys.path:
        return
    site.addsitedir(path)
    if os.name == "nt" and hasattr(os, "add_dll_directory"):
        for d in [path] + [os.path.join(path, n) for n in os.listdir(path)
                           if n.endswith(".libs")]:
            try:
                _dll_handles.append(os.add_dll_directory(d))
            except OSError:
                pass


def _has_module(names):
    for name in names:
        try:
            if importlib.util.find_spec(name) is not None:
                return True
        except (ImportError, ValueError):
            pass
    return False


def missing_dependencies(labels=None):
    """[(label, pip package)] for formats whose packages aren't installed."""
    return [(label, pkg) for label, modules, pkg in DEPENDENCIES
            if (labels is None or label in labels)
            and not _has_module(modules)]


def _mayapy():
    exe = "mayapy.exe" if os.name == "nt" else "mayapy"
    path = os.path.join(os.environ.get("MAYA_LOCATION", ""), "bin", exe)
    return path if os.path.isfile(path) else None


def install_dependencies(packages=None):
    """pip-install the STEP/IGES and IFC packages for this Maya's Python.
    Returns True if everything needed is importable afterwards."""
    gui = not cmds.about(batch=True)
    if packages is None:
        packages = [pkg for _, pkg in missing_dependencies()]
    if not packages:
        msg = "STEP, IGES and IFC support are already installed."
        if gui:
            cmds.confirmDialog(title="cadMeshImport", message=msg,
                               button=["OK"])
        print("cadMeshImport: " + msg)
        return True
    mayapy = _mayapy()
    if mayapy is None:
        cmds.warning("cadMeshImport: can't find mayapy under MAYA_LOCATION; "
                     "install %s by hand (see README)." % ", ".join(packages))
        return False

    target = _deps_dir()
    if not os.path.isdir(target):
        os.makedirs(target)
    cmd = [mayapy, "-m", "pip", "install", "--upgrade", "--target", target]
    cmd += list(packages)
    print("cadMeshImport: running " + " ".join(cmd))
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" \
        else 0
    if gui:
        cmds.progressWindow(title="Installing STEP / IFC support",
                            status="Starting pip...", isInterruptable=True,
                            progress=0, maxValue=100)
    cancelled = False
    tail = []
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT,
                                universal_newlines=True, creationflags=flags)
        for line in proc.stdout:
            line = line.rstrip()
            print(line)
            tail = (tail + [line])[-8:]
            if gui:
                cmds.progressWindow(edit=True, status=line[-70:] or "...")
                if cmds.progressWindow(query=True, isCancelled=True):
                    proc.kill()
                    cancelled = True
                    break
        proc.wait()
    finally:
        if gui:
            cmds.progressWindow(endProgress=True)

    _add_deps_path()
    importlib.invalidate_caches()
    wanted = [(label, modules) for label, modules, pkg in DEPENDENCIES
              if pkg in packages]
    failed = [label for label, modules in wanted if not _has_module(modules)]
    if cancelled:
        msg = "Install cancelled."
    elif failed:
        msg = ("Couldn't install support for: %s.\n\nLast pip output:\n%s"
               "\n\nThere may be no package yet for this Maya's Python "
               "(%d.%d). See the README for manual options."
               % (", ".join(failed), "\n".join(tail),
                  sys.version_info[0], sys.version_info[1]))
    else:
        msg = ("Installed. %s import is ready to use."
               % " and ".join(label for label, _ in wanted))
    print("cadMeshImport: " + msg)
    if gui:
        cmds.confirmDialog(title="cadMeshImport", message=msg, button=["OK"])
    return not failed and not cancelled


def offer_dependency_install(labels=None):
    """Ask before installing missing packages. Returns True if nothing
    is missing afterwards."""
    missing = missing_dependencies(labels)
    if not missing:
        return True
    if cmds.about(batch=True):
        return False
    answer = cmds.confirmDialog(
        title="cadMeshImport",
        message=("%s import needs extra Python packages (%s).\n\n"
                 "Download and install them now? This can take a few "
                 "minutes; cadquery-ocp is a large download."
                 % (" and ".join(l for l, _ in missing),
                    ", ".join(p for _, p in missing))),
        button=["Install", "Not now"], defaultButton="Install",
        cancelButton="Not now", dismissString="Not now")
    if answer != "Install":
        return False
    return install_dependencies([p for _, p in missing])


_add_deps_path()


# --------------------------------------------------------------------------
# Importer dialog
# --------------------------------------------------------------------------
WINDOW = "cadMeshImportWindow"
MENU = "cadMeshImportMenu"
UNIT_PRESETS = [("Millimetres", 0.1), ("Centimetres", 1.0), ("Metres", 100.0),
                ("Inches", 2.54), ("Custom", None)]
_ui = {}


def _pref(key, default):
    var = "cadMeshImport_" + key
    return cmds.optionVar(query=var) if cmds.optionVar(exists=var) else default


def _set_pref(key, value):
    var = "cadMeshImport_" + key
    if isinstance(value, str):
        cmds.optionVar(stringValue=(var, value))
    elif isinstance(value, float):
        cmds.optionVar(floatValue=(var, value))
    else:
        cmds.optionVar(intValue=(var, int(value)))


def _selected_files():
    text = cmds.textField(_ui["files"], query=True, text=True)
    return [p.strip() for p in text.split(";") if p.strip()]


def _browse(*_):
    kwargs = dict(fileMode=4, caption="Choose STL / STEP / IGES / IFC files",
                  okCaption="Choose",
                  fileFilter="CAD / BIM files (*.stl *.step *.stp *.igs "
                             "*.iges *.ifc);;STL (*.stl);;STEP (*.step *.stp)"
                             ";;IGES (*.igs *.iges);;IFC (*.ifc)")
    last_dir = _pref("lastDir", "")
    if last_dir and os.path.isdir(last_dir):
        kwargs["startingDirectory"] = last_dir
    paths = cmds.fileDialog2(**kwargs)
    if paths:
        cmds.textField(_ui["files"], edit=True, text="; ".join(paths))
        _set_pref("lastDir", os.path.dirname(paths[0]))
        _update_step_state()


def _update_step_state(*_):
    has_step = any(p.lower().endswith(CAD_EXTENSIONS)
                   for p in _selected_files())
    cmds.frameLayout(_ui["stepFrame"], edit=True, enable=has_step)
    has_ifc = any(p.lower().endswith(".ifc") for p in _selected_files())
    cmds.frameLayout(_ui["ifcFrame"], edit=True, enable=has_ifc)


def _on_units(*_):
    label = cmds.optionMenuGrp(_ui["units"], query=True, value=True)
    scale = dict(UNIT_PRESETS)[label]
    if scale is not None:
        cmds.floatFieldGrp(_ui["scale"], edit=True, value1=scale)
    cmds.floatFieldGrp(_ui["scale"], edit=True, enable=scale is None)


def _on_quality(*_):
    label = cmds.optionMenuGrp(_ui["quality"], query=True, value=True)
    for name, linear, angular in QUALITY_PRESETS:
        if name == label and linear is not None:
            cmds.floatFieldGrp(_ui["linDef"], edit=True, value1=linear)
            cmds.floatFieldGrp(_ui["angDef"], edit=True, value1=angular)
    custom = label == "Custom"
    cmds.floatFieldGrp(_ui["linDef"], edit=True, enable=custom)
    cmds.floatFieldGrp(_ui["angDef"], edit=True, enable=custom)


def _on_hierarchy(*_):
    merged = cmds.optionMenuGrp(_ui["hierarchy"], query=True,
                                select=True) == 3
    nurbs = cmds.optionMenuGrp(_ui["geometry"], query=True, select=True) == 2
    cmds.checkBoxGrp(_ui["instances"], edit=True,
                     enable=nurbs or not merged)


def _on_geometry(*_):
    nurbs = cmds.optionMenuGrp(_ui["geometry"], query=True, select=True) == 2
    cmds.optionMenuGrp(_ui["quality"], edit=True, enable=not nurbs)
    cmds.checkBoxGrp(_ui["cadNormals"], edit=True, enable=not nurbs)
    if nurbs:
        cmds.floatFieldGrp(_ui["linDef"], edit=True, enable=False)
        cmds.floatFieldGrp(_ui["angDef"], edit=True, enable=False)
    else:
        _on_quality()
    _on_hierarchy()


def _on_soften(*_):
    on = cmds.checkBoxGrp(_ui["soften"], query=True, value1=True)
    cmds.floatSliderGrp(_ui["angle"], edit=True, enable=on)


def _gather_options():
    soften = cmds.checkBoxGrp(_ui["soften"], query=True, value1=True)
    angle = cmds.floatSliderGrp(_ui["angle"], query=True, value=True)
    return {
        "scale": cmds.floatFieldGrp(_ui["scale"], query=True, value1=True),
        "zUp": int(cmds.checkBoxGrp(_ui["zUp"], query=True, value1=True)),
        "weld": int(cmds.checkBoxGrp(_ui["weld"], query=True, value1=True)),
        "smoothAngle": angle if soften else -1.0,
        "linearDeflection": cmds.floatFieldGrp(_ui["linDef"], query=True,
                                               value1=True),
        "angularDeflection": cmds.floatFieldGrp(_ui["angDef"], query=True,
                                                value1=True),
        "geometry": cmds.optionMenuGrp(_ui["geometry"], query=True,
                                       select=True) - 1,
        "hierarchy": cmds.optionMenuGrp(_ui["hierarchy"], query=True,
                                        select=True) - 1,
        "instances": int(cmds.checkBoxGrp(_ui["instances"], query=True,
                                          value1=True)),
        "colors": int(cmds.checkBoxGrp(_ui["colors"], query=True,
                                       value1=True)),
        "cadNormals": int(cmds.checkBoxGrp(_ui["cadNormals"], query=True,
                                           value1=True)),
        "layers": int(cmds.checkBoxGrp(_ui["layers"], query=True,
                                       value1=True)),
        "skipHidden": int(cmds.checkBoxGrp(_ui["skipHidden"], query=True,
                                           value1=True)),
        "includeSpaces": int(cmds.checkBoxGrp(_ui["spaces"], query=True,
                                              value1=True)),
        "groupByStorey": int(cmds.checkBoxGrp(_ui["storeys"], query=True,
                                              value1=True)),
        "materials": int(cmds.checkBoxGrp(_ui["ifcMats"], query=True,
                                          value1=True)),
    }


def _save_prefs(opts):
    _set_pref("units", cmds.optionMenuGrp(_ui["units"], query=True,
                                          value=True))
    _set_pref("soften", int(opts["smoothAngle"] >= 0))
    _set_pref("quality", cmds.optionMenuGrp(_ui["quality"], query=True,
                                            value=True))
    _set_pref("angle", cmds.floatSliderGrp(_ui["angle"], query=True,
                                           value=True))
    for key in ("scale", "zUp", "weld", "linearDeflection",
                "angularDeflection", "geometry", "hierarchy", "instances",
                "colors",
                "cadNormals", "layers", "skipHidden", "includeSpaces",
                "groupByStorey", "materials"):
        _set_pref(key, opts[key])


def _do_import(*_):
    files = _selected_files()
    if not files:
        cmds.warning("cadMeshImport: choose at least one file.")
        return
    opts = _gather_options()
    _save_prefs(opts)
    # Close straight away so the dialog doesn't sit over the viewport
    # while a big file imports. Errors still go to the Script Editor.
    cmds.deleteUI(WINDOW)
    stl_opts = {k: opts[k] for k in STL_DEFAULTS}
    step_opts = {k: opts[k] for k in STEP_DEFAULTS}
    step_opts["scale"] = 0.1  # OpenCASCADE always delivers millimetres
    ifc_opts = {k: opts[k] for k in IFC_DEFAULTS if k != "scale"}

    needs = set()
    for path in files:
        ext = os.path.splitext(path)[1].lower()
        if ext in CAD_EXTENSIONS:
            needs.add("STEP / IGES")
        elif ext == ".ifc":
            needs.add("IFC")
    if needs:
        offer_dependency_install(needs)

    groups, failed = [], []
    cmds.waitCursor(state=True)
    try:
        for path in files:
            ext = os.path.splitext(path)[1].lower()
            try:
                if ext == ".stl":
                    groups.append(import_stl(path, **stl_opts))
                elif ext in CAD_EXTENSIONS:
                    groups.append(import_step(path, **step_opts))
                elif ext == ".ifc":
                    groups.append(import_ifc(path, **ifc_opts))
                else:
                    failed.append((path, "unsupported file type"))
            except Exception as e:
                failed.append((path, str(e)))
    finally:
        cmds.waitCursor(state=False)

    for path, err in failed:
        cmds.warning("cadMeshImport: %s: %s" % (os.path.basename(path), err))
    if groups:
        cmds.select(groups)
        cmds.inViewMessage(amg="Imported %d file(s)" % len(groups),
                           pos="topCenter", fade=True)


def show_dialog():
    """Open the STL / STEP / IGES / IFC importer window."""
    if cmds.window(WINDOW, exists=True):
        cmds.deleteUI(WINDOW)
    cmds.window(WINDOW, title="CAD Mesh Import %s (STL / STEP / IGES / IFC)"
                % PLUGIN_VERSION, sizeable=True)
    cmds.columnLayout(adjustableColumn=True, rowSpacing=6)

    cmds.frameLayout(label="Files", marginWidth=6, marginHeight=6)
    cmds.rowLayout(numberOfColumns=2, adjustableColumn=1)
    _ui["files"] = cmds.textField(changeCommand=_update_step_state)
    cmds.button(label="Browse...", width=80, command=_browse)
    cmds.setParent("..")
    cmds.setParent("..")

    cmds.frameLayout(label="General", marginWidth=6, marginHeight=6)
    _ui["units"] = cmds.optionMenuGrp(label="STL file units",
                                      changeCommand=_on_units)
    for label, _ in UNIT_PRESETS:
        cmds.menuItem(label=label)
    _ui["scale"] = cmds.floatFieldGrp(label="Scale", precision=4,
                                      value1=_pref("scale", 0.1))
    _ui["zUp"] = cmds.checkBoxGrp(label="", label1="Convert Z-up to Y-up",
                                  value1=bool(_pref("zUp", 1)))
    _ui["weld"] = cmds.checkBoxGrp(label="", label1="Weld coincident vertices",
                                   value1=bool(_pref("weld", 1)))
    _ui["soften"] = cmds.checkBoxGrp(label="", label1="Soften edges by angle",
                                     value1=bool(_pref("soften", 1)),
                                     changeCommand=_on_soften)
    _ui["angle"] = cmds.floatSliderGrp(label="Angle", field=True,
                                       minValue=0, maxValue=180,
                                       value=_pref("angle", 30.0))
    cmds.setParent("..")

    _ui["stepFrame"] = cmds.frameLayout(
        label="STEP / IGES (units read from the file)",
        marginWidth=6, marginHeight=6)
    _ui["geometry"] = cmds.optionMenuGrp(
        label="Import as", changeCommand=_on_geometry,
        annotation="NURBS keeps the exact CAD surfaces, one trimmed surface "
                   "per CAD face. Meshes are lighter and engine-ready.")
    for label in ("Polygon meshes", "NURBS surfaces"):
        cmds.menuItem(label=label)
    _ui["hierarchy"] = cmds.optionMenuGrp(
        label="Structure", changeCommand=_on_hierarchy,
        annotation="Single merged mesh applies to polygon meshes only; "
                   "NURBS use flat parts instead.")
    for label in HIERARCHY_MODES:
        cmds.menuItem(label=label)
    _ui["instances"] = cmds.checkBoxGrp(
        label="", label1="Instance repeated parts",
        value1=bool(_pref("instances", 1)),
        annotation="Bolts, screws etc. share one mesh. Much lighter scenes.")
    _ui["colors"] = cmds.checkBoxGrp(
        label="", label1="Create shaders from CAD colours",
        value1=bool(_pref("colors", 1)))
    _ui["cadNormals"] = cmds.checkBoxGrp(
        label="", label1="Exact CAD normals",
        value1=bool(_pref("cadNormals", 1)),
        annotation="Normals from the true surfaces: smooth curves, sharp "
                   "edges. Locks normals; turn off to use the soften angle.")
    _ui["layers"] = cmds.checkBoxGrp(
        label="", label1="CAD layers to display layers",
        value1=bool(_pref("layers", 1)))
    _ui["skipHidden"] = cmds.checkBoxGrp(
        label="", label1="Skip hidden parts",
        value1=bool(_pref("skipHidden", 1)))
    _ui["quality"] = cmds.optionMenuGrp(label="Mesh quality",
                                        changeCommand=_on_quality)
    for label, _, _ in QUALITY_PRESETS:
        cmds.menuItem(label=label)
    _ui["linDef"] = cmds.floatFieldGrp(
        label="Linear deflection", precision=3,
        value1=_pref("linearDeflection", 0.1),
        annotation="Max distance between mesh and true surface, in mm. "
                   "Smaller = denser mesh.")
    _ui["angDef"] = cmds.floatFieldGrp(
        label="Angular deflection", precision=3,
        value1=_pref("angularDeflection", 0.5),
        annotation="Max angle between adjacent facets, in radians. "
                   "Smaller = smoother curves.")
    cmds.setParent("..")

    _ui["ifcFrame"] = cmds.frameLayout(label="IFC (always metres; File units "
                                             "and Scale are ignored)",
                                       marginWidth=6, marginHeight=6)
    _ui["storeys"] = cmds.checkBoxGrp(
        label="", label1="Group by storey / space",
        value1=bool(_pref("groupByStorey", 1)))
    _ui["ifcMats"] = cmds.checkBoxGrp(
        label="", label1="Create shaders from IFC colours",
        value1=bool(_pref("materials", 1)))
    _ui["spaces"] = cmds.checkBoxGrp(
        label="", label1="Include IfcSpace volumes",
        value1=bool(_pref("includeSpaces", 0)),
        annotation="Room volumes. Usually off: they overlap walls and floors.")
    cmds.setParent("..")

    cmds.rowLayout(numberOfColumns=2, columnWidth2=(205, 205))
    cmds.button(label="Import", width=200, command=_do_import)
    cmds.button(label="Cancel", width=200,
                command=lambda *_: cmds.deleteUI(WINDOW))
    cmds.setParent("..")

    cmds.optionMenuGrp(_ui["units"], edit=True,
                       value=_pref("units", "Millimetres"))
    cmds.optionMenuGrp(_ui["hierarchy"], edit=True,
                       select=int(_pref("hierarchy", 0)) + 1)
    cmds.optionMenuGrp(_ui["quality"], edit=True,
                       value=_pref("quality", "Medium"))
    cmds.optionMenuGrp(_ui["geometry"], edit=True,
                       select=int(_pref("geometry", 0)) + 1)
    _on_units()
    _on_quality()
    _on_geometry()
    _on_soften()
    _update_step_state()
    cmds.showWindow(WINDOW)


def _add_menu():
    if cmds.about(batch=True):
        return
    if cmds.menu(MENU, exists=True):
        cmds.deleteUI(MENU)
    cmds.menu(MENU, label="CAD Import", parent="MayaWindow", tearOff=True)
    cmds.menuItem(label="Import STL / STEP / IGES / IFC...",
                  command=lambda *_: show_dialog())
    cmds.menuItem(divider=True)
    cmds.menuItem(label="Install STEP / IFC support...",
                  command=lambda *_: install_dependencies())


def _remove_menu():
    if not cmds.about(batch=True) and cmds.menu(MENU, exists=True):
        cmds.deleteUI(MENU)


# --------------------------------------------------------------------------
# File translators
# --------------------------------------------------------------------------
class _ImportTranslator(OpenMayaMPx.MPxFileTranslator):
    extensions = ()
    defaults = {}
    import_func = None

    def haveReadMethod(self):
        return True

    def haveWriteMethod(self):
        return False

    def canBeOpened(self):
        return False  # import only, never File > Open

    def defaultExtension(self):
        return self.extensions[0]

    def filter(self):
        return ";".join("*." + e for e in self.extensions)

    def identifyFile(self, file_object, buffer, size):
        ext = os.path.splitext(file_object.resolvedName())[1].lower()
        if ext.lstrip(".") in self.extensions:
            return OpenMayaMPx.MPxFileTranslator.kIsMyFileType
        return OpenMayaMPx.MPxFileTranslator.kNotMyFileType

    def reader(self, file_object, option_string, mode):
        path = file_object.expandedFullName()
        opts = _parse_options(option_string, self.defaults)
        type(self).import_func(path, **opts)


class STLTranslator(_ImportTranslator):
    extensions = ("stl",)
    defaults = STL_DEFAULTS
    import_func = staticmethod(import_stl)


class STEPTranslator(_ImportTranslator):
    extensions = ("step", "stp")
    defaults = STEP_DEFAULTS
    import_func = staticmethod(import_step)


class IGESTranslator(_ImportTranslator):
    extensions = ("igs", "iges")
    defaults = STEP_DEFAULTS
    import_func = staticmethod(import_step)


class IFCTranslator(_ImportTranslator):
    extensions = ("ifc",)
    defaults = IFC_DEFAULTS
    import_func = staticmethod(import_ifc)


def _options_string(defaults):
    return ";".join("%s=%s" % kv for kv in defaults.items())


def initializePlugin(mobject):
    plugin = OpenMayaMPx.MFnPlugin(mobject, "cadMeshImport",
                                   PLUGIN_VERSION, "Any")
    plugin.registerFileTranslator(
        STL_TRANSLATOR, None,
        lambda: OpenMayaMPx.asMPxPtr(STLTranslator()),
        None, _options_string(STL_DEFAULTS), False)
    plugin.registerFileTranslator(
        STEP_TRANSLATOR, None,
        lambda: OpenMayaMPx.asMPxPtr(STEPTranslator()),
        None, _options_string(STEP_DEFAULTS), False)
    plugin.registerFileTranslator(
        IGES_TRANSLATOR, None,
        lambda: OpenMayaMPx.asMPxPtr(IGESTranslator()),
        None, _options_string(STEP_DEFAULTS), False)
    plugin.registerFileTranslator(
        IFC_TRANSLATOR, None,
        lambda: OpenMayaMPx.asMPxPtr(IFCTranslator()),
        None, _options_string(IFC_DEFAULTS), False)
    _add_menu()


def uninitializePlugin(mobject):
    _remove_menu()
    plugin = OpenMayaMPx.MFnPlugin(mobject)
    plugin.deregisterFileTranslator(STL_TRANSLATOR)
    plugin.deregisterFileTranslator(STEP_TRANSLATOR)
    plugin.deregisterFileTranslator(IGES_TRANSLATOR)
    plugin.deregisterFileTranslator(IFC_TRANSLATOR)


# Running the file directly (Script Editor, shelf button) opens the dialog.
if __name__ == "__main__":
    show_dialog()
