import importlib.util
import os
import sys
import folder_paths as comfy_paths
import importlib

ROOT_PATH = os.path.join(comfy_paths.get_folder_paths("custom_nodes")[0], "ComfyUI_MeshAnythingV2")
MODULE_PATH = os.path.join(ROOT_PATH, "MeshAnything")

sys.path.append(ROOT_PATH)
sys.path.append(MODULE_PATH)

from .nodes import NODE_CLASS_MAPPINGS
# from . import MeshAnything

print('--------------')
print('*ComfyUI_MeshAnythingV2- nodes_loaded*')
print('--------------')

__ALL__ = ['NODE_CLASS_MAPPINGS']