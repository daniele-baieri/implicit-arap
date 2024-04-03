from __future__ import annotations

import torch
import vedo
import mcubes
import pathlib
import numpy as np
import polyscope as ps
import polyscope.imgui as psim
import torch.nn.functional as F

from typing import Tuple, Type, Literal
from dataclasses import dataclass, field
from pathlib import Path
from tqdm import tqdm

from iarap.config.base_config import InstantiateConfig
from iarap.model.neural_rtf import NeuralRTFConfig
from iarap.model.neural_sdf import NeuralSDFConfig
from iarap.utils import gradient, euler_to_rotation
from iarap.utils.misc import detach_model



class SDFRenderer:

    def __init__(self, config: SDFRendererConfig):
        self.config = config
        self.setup_model()

    def setup_model(self):
        self.shape_model = self.config.shape_model.setup().to(self.config.device)
        self.shape_model.load_state_dict(torch.load(self.config.load_shape))
        detach_model(self.shape_model)
        if self.config.load_deformation is not None:
            self.deformation_model = self.config.deformation_model.setup().to(self.config.device)
            self.deformation_model.load_state_dict(torch.load(self.config.load_deformation))
            detach_model(self.deformation_model)
        else:
            self.deformation_model = None
        vol = self.make_volume()
        self.cached_sdf = self.evaluate_model(vol).numpy()

    def make_volume(self):
        steps = torch.linspace(self.config.min_coord, 
                               self.config.max_coord, 
                               self.config.resolution,
                               device=self.config.device)
        xx, yy, zz = torch.meshgrid(steps, steps, steps, indexing="ij")
        return torch.vstack([xx.ravel(), yy.ravel(), zz.ravel()]).T.float()

    @torch.no_grad()
    def evaluate_model(self, pts_volume):    
        f_eval = []
        for sample in tqdm(torch.split(pts_volume, self.config.chunk, dim=0)):
            f_eval.append(self.sdf_functional(sample.contiguous()).cpu())
        f_volume = torch.cat(f_eval, dim=0).reshape(*([self.config.resolution] * 3))
        return f_volume
    
    def extract_mesh(self, level=0.0):
        try:
            verts, faces = mcubes.marching_cubes(self.cached_sdf, level)
            verts /= self.config.resolution // 2
            verts -= 1.0
        except:
            verts = np.empty([0, 3], dtype=np.float32)
            faces = np.empty([0, 3], dtype=np.int32)
        # if self.deformation_model is not None:
        #     verts = torch.from_numpy(verts).to(self.config.device, torch.float)
        #     out_verts = []
        #     for sample in torch.split(verts, self.config.chunk, dim=0):
        #         transformed = self.deformation_model.network(sample)[0]  # transform(sample)
        #         out_verts.append(transformed.cpu().detach().numpy())
        #     verts = np.concatenate(out_verts, axis=0)
        return verts, faces
        
    def sdf_functional(self, query):
        sample = query
        if self.deformation_model is not None:
            sample = self.deformation_model.transform(sample)  
        model_out = self.shape_model(sample)
        return model_out['dist']
    
    def project_nearest(self, query, n_its=5, level=0.0):
        query = torch.from_numpy(query).float().to(self.config.device).view(-1, 3).requires_grad_()
        for i in range(n_its):
            dist = self.sdf_functional(query) - level
            grad = F.normalize(gradient(dist, query), dim=-1)
            query = (query - dist * grad).detach().requires_grad_()
        return query.detach()

    def run(self):

        ps.set_ground_plane_mode("none")
        ps.set_window_size(*self.config.window_size)
        ps.set_window_resizable(True)

        live_picks, frozen_picks = [], []
        output_file = "path/to/point/selection/file.txt"
        input_select = "path/to/point/selection/file.txt"
        points_to_export = 'live'
        viewed_level_set = 0.0
        last_level_set = viewed_level_set
        verts, faces = self.extract_mesh(level=0)
        tx, ty, tz = 0.0, 0.0, 0.0
        rx, ry, rz = 0.0, 0.0, 0.0
        duplicate = False
        clear_only_frozen = True

        def custom_callback():
            io = psim.GetIO()
            nonlocal live_picks, frozen_picks, output_file, input_select, points_to_export
            nonlocal verts, faces
            nonlocal viewed_level_set, last_level_set
            nonlocal tx, ty, tz, rx, ry, rz, duplicate, clear_only_frozen

            if io.MouseClicked[0] and io.KeyCtrl:
                screen_coords = io.MousePos
                world_pos = ps.screen_coords_to_world_position(screen_coords)
                # print(world_pos)
                if np.abs(world_pos).max() <= 1.0 and not np.isinf(world_pos).any():
                    world_pos = self.project_nearest(world_pos, n_its=10, level=viewed_level_set
                                                     ).squeeze().cpu().numpy()
                    live_picks.append(world_pos)
                    ps.register_point_cloud("Live Picks", np.stack(live_picks, axis=0), enabled=True)
                    # self.set_picked(np.expand_dims(world_pos, axis=0))

            _, viewed_level_set = psim.SliderFloat("Level set", viewed_level_set, v_min=-1.0, v_max=1.0)

            if psim.Button("Zero"):
                viewed_level_set = 0.0

            psim.SameLine()

            if psim.Button("Render") and viewed_level_set != last_level_set:
                last_level_set = viewed_level_set
                verts, faces = self.extract_mesh(level=viewed_level_set)
                ps.register_surface_mesh("NeuralSDF", verts, faces, enabled=True)

                if len(live_picks) > 0:
                    live_picks = []
                if len(frozen_picks) > 0:
                    frozen_picks = []
                if ps.has_point_cloud("Live Picks"):
                    ps.remove_point_cloud("Live Picks")
                if ps.has_point_cloud("Frozen Picks"):
                    ps.remove_point_cloud("Frozen Picks")

            psim.Separator()
            _, input_select = psim.InputText("Load selection file", input_select)
            _, output_file = psim.InputText("Output file", output_file)

            changed = psim.BeginCombo("Which points to export", points_to_export)
            if changed:
                for val in ['live', 'frozen']:
                    _, selected = psim.Selectable(val, points_to_export==val)
                    if selected:
                        points_to_export = val
                psim.EndCombo()
                
            if psim.Button("Save points"):
                points = frozen_picks if points_to_export == 'frozen' else live_picks
                if len(points) > 0:
                    print(f"Saving {points_to_export} points at {output_file}")
                    pathlib.Path(output_file).parent.mkdir(parents=True, exist_ok=True)
                    try:
                        np.savetxt(output_file, np.stack(points, axis=0))
                    except:
                        print("Invalid output file location.")
                else:
                    print(f"No {points_to_export} points to save.")

            psim.SameLine()

            if psim.Button("Load points"):
                print(f"Loading points from {input_select}")
                try:
                    loaded = np.loadtxt(input_select)
                    live_picks += [x.squeeze() for x in np.split(loaded, loaded.shape[0], axis=0)]
                    ps.register_point_cloud("Live Picks", np.stack(live_picks, axis=0), enabled=True)
                except:
                    print("Invalid file.")

            psim.Separator()
            edit_transform = set()
            if psim.TreeNode("Translate"):
                ch_tx, tx = psim.InputFloat("x", tx)
                ch_ty, ty = psim.InputFloat("y", ty)
                ch_tz, tz = psim.InputFloat("z", tz)
                edit_transform.update({ch_tx, ch_ty, ch_tz})
                psim.TreePop()

            if psim.TreeNode("Rotate (euler)"):
                ch_rx, rx = psim.InputFloat("deg x", rx)
                ch_ry, ry = psim.InputFloat("deg y", ry)
                ch_rz, rz = psim.InputFloat("deg z", rz)
                edit_transform.update({ch_rx, ch_ry, ch_rz})
                psim.TreePop()

            if True in edit_transform and ps.has_point_cloud("Live Picks"):
                euler = torch.tensor([[np.deg2rad(rx), np.deg2rad(ry), np.deg2rad(rz)]])
                translate = torch.tensor([tx, ty, tz])
                rot = euler_to_rotation(euler).squeeze(0)
                transform = torch.eye(4)
                transform[:3, :3] = rot
                transform[:3,  3] = translate
                ps.get_point_cloud("Live Picks").set_transform(transform.numpy())

            _, duplicate = psim.Checkbox("Keep live set", duplicate)

            psim.SameLine()

            if psim.Button("Freeze transforms") and ps.has_point_cloud("Live Picks"):
                transform = ps.get_point_cloud("Live Picks").get_transform()
                points_to_transform = np.concatenate(
                    [np.stack(live_picks, axis=0), np.ones((len(live_picks), 1))], axis=-1)
                transformed = np.dot(points_to_transform, transform[:3, :].T)
                frozen_picks += [x.squeeze() for x in np.split(transformed, transformed.shape[0], axis=0)]
                ps.register_point_cloud("Frozen Picks", np.stack(frozen_picks, axis=0), enabled=True)
                
                ps.get_point_cloud("Live Picks").set_transform(np.eye(4))
                tx, ty, tz, rx, ry, rz = 0.0, 0.0, 0.0, 0.0, 0.0, 0.0
                if not duplicate:
                    live_picks = []
                    ps.remove_point_cloud("Live Picks")
            
            _, clear_only_frozen = psim.Checkbox("Only frozen", clear_only_frozen)

            psim.SameLine()

            if psim.Button("Clear points"):
                if len(live_picks) > 0 and not clear_only_frozen:
                    live_picks = []
                    ps.get_point_cloud("Live Picks").set_transform(np.eye(4))
                    ps.remove_point_cloud("Live Picks")
                    tx, ty, tz, rx, ry, rz = 0.0, 0.0, 0.0, 0.0, 0.0, 0.0
                    # self.shape_color[...] = 0.0
                if len(frozen_picks) > 0:
                    frozen_picks = []
                    ps.remove_point_cloud("Frozen Picks")

        ps.init()
        ps.register_surface_mesh("NeuralSDF", verts, faces, enabled=True)
        ps.set_user_callback(custom_callback)
        ps.show()
    
        


@dataclass
class SDFRendererConfig(InstantiateConfig):

    _target: Type = field(default_factory=lambda: SDFRenderer)
    load_shape: Path = Path('./assets/weights/armadillo.pt')
    load_deformation: Path = None
    min_coord: float = -1.0
    max_coord: float =  1.0
    resolution: int = 512
    chunk: int = 65536
    window_size: Tuple[int, int] = (1600, 1200)
    device: Literal['cpu', 'cuda'] = 'cuda'
    shape_model: NeuralSDFConfig = NeuralSDFConfig()
    deformation_model: NeuralRTFConfig = NeuralRTFConfig()
