"""
Installer for cadMeshImport. Run by dragging install.mel into a Maya viewport.

It writes a Maya module file (cadMeshImport.mod) into your user modules
folder pointing at this folder, so Maya finds the plugin on every start
without copying files around. Then it loads the plugin, sets it to
auto-load, adds a shelf button and offers to download the Python packages
that STEP, IGES and IFC import need.

Uninstall from the Script Editor:
    import installer; installer.uninstall()
or just delete cadMeshImport.mod from Documents/maya/modules.
"""

import importlib
import os
import sys

import maya.cmds as cmds
import maya.mel as mel

MODULE = "cadMeshImport"
VERSION = "1.4.4"
PLUGIN = "cadMeshImport.py"


def _modules_dir():
    path = os.path.join(cmds.internalVar(userAppDir=True), "modules")
    if not os.path.isdir(path):
        os.makedirs(path)
    return path


def _mod_path():
    return os.path.join(_modules_dir(), MODULE + ".mod")


def _write_mod(root):
    root = root.replace("\\", "/")
    text = ("+ {name} {ver} {root}\n"
            "PYTHONPATH +:= plug-ins\n").format(name=MODULE, ver=VERSION,
                                                 root=root)
    with open(_mod_path(), "w") as f:
        f.write(text)


def _add_shelf_button():
    shelf_top = mel.eval("$tmp = $gShelfTopLevel")
    shelf = cmds.tabLayout(shelf_top, query=True, selectTab=True)
    for btn in cmds.shelfLayout(shelf, query=True, childArray=True) or []:
        if cmds.shelfButton(btn, query=True, exists=True) and \
                cmds.shelfButton(btn, query=True, label=True) == MODULE:
            cmds.deleteUI(btn)
    cmds.shelfButton(
        parent=shelf, label=MODULE, annotation="Import STL / STEP / IFC",
        image="pythonFamily.png", imageOverlayLabel="CAD",
        sourceType="python",
        command=("import importlib, cadMeshImport\n"
                 "importlib.reload(cadMeshImport)\n"
                 "cadMeshImport.show_dialog()"))
    mel.eval('saveAllShelves $gShelfTopLevel')


def install(root):
    root = os.path.abspath(root)
    plugin_dir = os.path.join(root, "plug-ins")
    plugin_file = os.path.join(plugin_dir, PLUGIN)
    if not os.path.isfile(plugin_file):
        cmds.error("cadMeshImport: can't find %s" % plugin_file)
        return

    _write_mod(root)

    # Make it work in this session too, without a restart.
    if plugin_dir not in sys.path:
        sys.path.append(plugin_dir)
    if cmds.pluginInfo(PLUGIN, query=True, loaded=True):
        cmds.unloadPlugin(PLUGIN, force=True)
    cmds.loadPlugin(plugin_file)
    cmds.pluginInfo(plugin_file, edit=True, autoload=True)

    _add_shelf_button()

    ready = False
    try:
        plugin = importlib.import_module("cadMeshImport")
        ready = plugin.offer_dependency_install()
    except Exception as e:
        print("cadMeshImport: dependency check failed: %s" % e)

    message = ("cadMeshImport is installed.\n\n"
               "Open it from the CAD Import menu, the CAD shelf button, "
               "or File > Import.")
    if not ready:
        message += ("\n\nSTL works now. For STEP, IGES and IFC, use "
                    "CAD Import > Install STEP / IFC support when you're "
                    "ready.")
    cmds.confirmDialog(title="cadMeshImport", message=message, button=["OK"])
    print("cadMeshImport: module file written to %s" % _mod_path())


def uninstall():
    if cmds.pluginInfo(PLUGIN, query=True, loaded=True):
        cmds.pluginInfo(PLUGIN, edit=True, autoload=False)
        cmds.unloadPlugin(PLUGIN, force=True)
    if os.path.isfile(_mod_path()):
        os.remove(_mod_path())
    print("cadMeshImport: uninstalled. Remove the shelf button by hand "
          "if you no longer want it.")
