import os, argparse
import torch
import time
import trimesh
import numpy as np
import datetime
from accelerate import Accelerator
from accelerate.utils import set_seed
from accelerate.utils import DistributedDataParallelKwargs
from safetensors.torch import load_model

from mesh_to_pc import process_mesh_to_pc
from huggingface_hub import hf_hub_download
from .MeshAnything.models.meshanything_v2 import MeshAnythingV2

class Dataset:
    def __init__(self, input_list, mc=True, mc_level=7, pc=False, pc_out=8192):
        super().__init__()
        self.data = []
        if pc: #if Point cloud is enabled
            for input_path in input_list:
                # load npy
                cur_data = np.load(input_path)
                # sample 8192
                assert (cur_data.shape[0] >= 8192), "input pc_normal should have at least 8192 points"
                idx = np.random.choice(cur_data.shape[0], pc_out, replace=False)
                cur_data = cur_data[idx]
                self.data.append(
                    {
                        "pc_normal": cur_data,
                        "uid": input_path.split("/")[-1].split(".")[0],
                    }
                )

        else: #using mesh option
            mesh_list = []
            for input_path in input_list:
                # load ply
                cur_data = trimesh.load(input_path)
                mesh_list.append(cur_data)
            if mc:
                print("First Marching Cubes and then sample point cloud, need several minutes...")
            pc_list, _ = process_mesh_to_pc(mesh_list, marching_cubes=mc, mc_level=mc_level)
            for input_path, cur_data in zip(input_list, pc_list):
                self.data.append(
                    {
                        "pc_normal": cur_data,
                        "uid": input_path.split("/")[-1].split(".")[0],
                    }
                )
        print(f"dataset total data samples: {len(self.data)}")

    def __len__(self):
        return len(self.data)

    def __getitem__(self, idx):
        data_dict = {}
        data_dict["pc_normal"] = self.data[idx]["pc_normal"]
        # normalize pc coor
        pc_coor = data_dict["pc_normal"][:, :3]
        normals = data_dict["pc_normal"][:, 3:]
        bounds = np.array([pc_coor.min(axis=0), pc_coor.max(axis=0)])
        pc_coor = pc_coor - (bounds[0] + bounds[1])[None, :] / 2
        pc_coor = pc_coor / np.abs(pc_coor).max() * 0.9995
        assert (
            np.linalg.norm(normals, axis=-1) > 0.99
        ).all(), "normals should be unit vectors, something wrong"
        data_dict["pc_normal"] = np.concatenate(
            [pc_coor, normals], axis=-1, dtype=np.float16
        )
        data_dict["uid"] = self.data[idx]["uid"]

        return data_dict

class MeshAnything3D:

    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {
                "mesh_file_path": ("STRING", {"default": '', "multiline": False}),
                "mc_level": ("INT",{"default": 7,"min": 0,"max": 20}),
                "mc": ("BOOLEAN",{"default": True, "label_on": "max","label_off": "min"}),
                "no_pc_vertices": ("INT",{"default": 8192,"min": 8192,"max": 100000}),
                "pc": ("BOOLEAN",{"default": True, "label_on": "max","label_off": "min"}),
                "batchsize_per_gpu": ("INT",{"default": 1, "min": 1, "max": 5}),
                "seed": ("INT",{"default": 29,"min": 0, "max": 10000000}),
                "sampling": ("BOOLEAN",{"default": False,"label_on": "max","label_off": "min"}),
            },
        }

    RETURN_TYPES = ("MESH",)
    RETURN_NAMES = ("mesh",)

    FUNCTION = "mesh_anything"
    CATEGORY = "ComfyMeshAnything3D/Algorithm"

    def mesh_anything(self, mesh_file_path,mc_level,mc,no_pc_vertices,pc,batchsize_per_gpu,seed,sampling):

        cur_time = datetime.datetime.now().strftime("%d_%H-%M-%S")
        checkpoint_dir = os.path.join("mesh_output", cur_time)
        os.makedirs(checkpoint_dir, exist_ok=True)

        kwargs = DistributedDataParallelKwargs(find_unused_parameters=True)
        accelerator = Accelerator(
            mixed_precision="fp16",
            project_dir=checkpoint_dir,
            kwargs_handlers=[kwargs]
        )

        model = MeshAnythingV2.from_pretrained("Yiwen-ntu/meshanythingv2")
        
        if mesh_file_path:
            set_seed(seed)
            dataset = Dataset([mesh_file_path], mc, mc_level, pc, no_pc_vertices)
        else:
            raise ValueError("input_path must be provided.")

        dataloader = torch.utils.data.DataLoader(
            dataset,
            batch_size=batchsize_per_gpu,
            drop_last = False,
            shuffle = False,
        )

        if accelerator.state.num_processes > 1:
            model = torch.nn.SyncBatchNorm.convert_sync_batchnorm(model)
        dataloader, model = accelerator.prepare(dataloader, model)
        begin_time = time.time()
        print("Generation Start!!!")
        with accelerator.autocast():
            for curr_iter, batch_data_label in enumerate(dataloader):
                curr_time = time.time()
                outputs = model(batch_data_label['pc_normal'], sampling=sampling)
                batch_size = outputs.shape[0]
                device = outputs.device

                for batch_id in range(batch_size):
                    recon_mesh = outputs[batch_id]
                    valid_mask = torch.all(~torch.isnan(recon_mesh.reshape((-1, 9))), dim=1)
                    recon_mesh = recon_mesh[valid_mask]  # nvalid_face x 3 x 3

                    vertices = recon_mesh.reshape(-1, 3).cpu()
                    vertices_index = np.arange(len(vertices))  # 0, 1, ..., 3 x face
                    triangles = vertices_index.reshape(-1, 3)

                    scene_mesh = trimesh.Trimesh(vertices=vertices, faces=triangles, force="mesh",
                                                merge_primitives=True)
                    scene_mesh.merge_vertices()
                    scene_mesh.update_faces(scene_mesh.nondegenerate_faces())
                    scene_mesh.update_faces(scene_mesh.unique_faces())
                    scene_mesh.remove_unreferenced_vertices()
                    scene_mesh.fix_normals()
                    save_path = os.path.join(checkpoint_dir, f'{batch_data_label["uid"][batch_id]}_gen.obj')
                    num_faces = len(scene_mesh.faces)
                    brown_color = np.array([255, 165, 0, 255], dtype=np.uint8)
                    face_colors = np.tile(brown_color, (num_faces, 1))

                    scene_mesh.visual.face_colors = face_colors
                    result = scene_mesh
                    scene_mesh.export(save_path)
                    print(f"{save_path} Over!!")
        
        end_time = time.time()
        print(f"Total time: {end_time - begin_time}")
        
        return ([result],)

NODE_CLASS_MAPPINGS = {
    "ComfyMeshAnything3D": MeshAnything3D
}