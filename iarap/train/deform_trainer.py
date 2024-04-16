from __future__ import annotations
import os

import vedo
import random
import torch
import numpy as np
import torch.nn.functional as F
import yaml

import iarap.data as data
import iarap.model.nn as nn

from typing import Dict, List, Literal, Tuple, Type
from dataclasses import dataclass, field
from tqdm import tqdm
from pathlib import Path

from iarap.config.base_config import InstantiateConfig
from iarap.model.nn.loss import DeformationLossConfig, PatchARAPLoss
from iarap.model.neural_rtf import NeuralRTF, NeuralRTFConfig
from iarap.model.neural_sdf import NeuralSDF, NeuralSDFConfig
from iarap.model.arap import ARAPMesh
from iarap.train.optim import AdamConfig, MultiStepSchedulerConfig
from iarap.train.trainer import Trainer
from iarap.utils import delaunay, detach_model
from iarap.utils.meshing import get_patch_mesh, sphere_random_uniform, sphere_sunflower


DEBUG = False



class DeformTrainer(Trainer):

    def __init__(self, config: DeformTrainerConfig):
        super(DeformTrainer, self).__init__(config)
        self.device = self.config.device
        random.seed(config.seed)
        np.random.seed(config.seed)
        torch.manual_seed(config.seed)

    def setup_data(self):
        # Avoids rewriting training function, does a single iteration per epoch
        self.loader = [0]  
        # Configure handles
        handle_cfg = yaml.load(open(self.config.handles_spec, 'r'), yaml.Loader)
        handle_dir = self.config.handles_spec.parent
        assert 'handles' in handle_cfg.keys(), f"Handle specification not found in file {self.config.handles_spec}"
        if 'static' in handle_cfg['handles'].keys() and len(handle_cfg['handles']['static']['positions']) > 0:
            static = [np.loadtxt(handle_dir / "parts" / f"{f}.txt") for f in handle_cfg['handles']['static']['positions']]
            static = np.concatenate(static, axis=0)
            if len(static.shape) < 2:
                static = np.expand_dims(static, 0)
            self.handles_static = torch.from_numpy(static).to(self.config.device, torch.float)
        else:
            self.handles_static = torch.empty((0, 3), device=self.config.device, dtype=torch.float)
        if 'moving' in handle_cfg['handles'].keys() and len(handle_cfg['handles']['moving']['positions']) > 0:
            assert len(handle_cfg['handles']['moving']['positions']) == len(handle_cfg['handles']['moving']['transform']),\
                "It is required to specify one transform for each handle set"
            moving = [np.loadtxt(handle_dir / "parts" / f"{f}.txt") for f in handle_cfg['handles']['moving']['positions']]
            transforms = [np.loadtxt(handle_dir / "transforms" / f"{f}.txt") for f in  handle_cfg['handles']['moving']['transform']]
            moving = np.concatenate(moving, axis=0)
            transforms = np.concatenate(transforms, axis=0)
            if len(moving.shape) < 2:
                moving = np.expand_dims(moving, axis=0)
                transforms = np.expand_dims(transforms, axis=0)
            moving_pos = torch.from_numpy(moving).to(self.config.device, torch.float)
            moving_trans = torch.from_numpy(transforms).to(self.config.device, torch.float)
            self.handles_moving = torch.cat([moving_pos, moving_trans], dim=-1)
        else:
            self.handles_moving = torch.empty((0, 6), device=self.config.device, dtype=torch.float)

    def setup_model(self):
        self.source: NeuralSDF = self.config.shape_model.setup().to(self.config.device).eval()
        self.source.load_state_dict(torch.load(self.config.pretrained_shape))
        detach_model(self.source)
        self.model: NeuralRTF = self.config.rotation_model.setup().to(self.config.device).train()
        self.model.set_sdf_callable(self.source.distance)
        self.loss = self.config.loss.setup()

    def sample_domain(self, nsamples):
        scale = self.config.domain_bounds[1] - self.config.domain_bounds[0]
        return torch.rand(nsamples, 3, device=self.device) * scale + self.config.domain_bounds[0]

    def train_step(self, batch):

        handles = torch.cat([self.handles_moving[:, :3], self.handles_static], dim=0)
        for it in range(self.config.num_projections):
            handles = self.source.project_nearest(handles).detach()
        handle_values = torch.cat(
            [self.handles_moving[:, 3:], handles[self.handles_moving.shape[0]:, :]], dim=0
        ).detach()

        surf_sample = self.source.sample_zero_level_set(self.config.zero_samples - handles.shape[0],
                                                        self.config.near_surface_threshold,
                                                        self.config.attempts_per_step,
                                                        self.config.domain_bounds,
                                                        self.config.num_projections).detach()
        surf_sample = torch.cat([handles, surf_sample], dim=0)
        space_sample = self.sample_domain(self.config.space_samples)
        samples = torch.cat([surf_sample, space_sample], dim=0)
        
        sdf_outs = self.source(samples, with_grad=True)
        sample_dist, patch_normals = sdf_outs['dist'], F.normalize(sdf_outs['grad'], dim=-1)
        tangent_planes = self.source.tangent_plane(samples)

        plane_coords, triangles = get_patch_mesh(sphere_random_uniform, 
                                                 delaunay,
                                                 self.config.delaunay_sample,
                                                 self.config.plane_coords_scale,
                                                 self.device)

        tangent_coords = (tangent_planes.unsqueeze(1) @ plane_coords.view(1, -1, 3, 1)).squeeze() 
        tangent_pts = tangent_coords + samples.unsqueeze(1) 
        level_set_verts = tangent_pts
        for it in range(self.config.num_projections):
            level_set_verts = self.source.project_level_sets(level_set_verts, sample_dist)  # n m 3
        
        if DEBUG:
            # Apply triangulation to each set of m points in surface_verts
            triangles_all = triangles.unsqueeze(0) + (level_set_verts.shape[1] * torch.arange(0, level_set_verts.shape[0], device=self.device).view(-1, 1, 1))

            surface_verts_flat = level_set_verts.view(-1, 3)
            triangles_flat = triangles_all.view(-1, 3)           

            vis_mesh = vedo.Mesh([surface_verts_flat.cpu().detach(), triangles_flat.cpu().long()]).wireframe()
            # vis_mesh.pointcolors = cmap
            # tangents = vedo.Mesh([tangent_pts.cpu().detach().view(-1, 3), triangles_flat.cpu().long()], c='black').wireframe()
            normals = vedo.Arrows(samples.detach().cpu().view(-1, 3), (samples + patch_normals * 0.02).cpu().detach().view(-1, 3))
            vedo.show(vis_mesh, normals).close()  
                    
        rtf_out = self.model(level_set_verts.detach())
        rotations = rtf_out['rot']
        translations = rtf_out['transl']
        # test_invert = self.model.inverse((rotations @ level_set_verts[..., None]).squeeze(-1) + translations)

        handle_idx = torch.stack([
            torch.arange(0, handles.shape[0], device=self.device, dtype=torch.long),
            torch.zeros(handles.shape[0], device=self.device, dtype=torch.long)
        ], dim=-1)
        moving_idx = handle_idx[:self.handles_moving.shape[0], :]
        static_idx = handle_idx[self.handles_moving.shape[0]:, :]
        loss_dict = self.loss(level_set_verts.detach(), 
                              triangles, rotations, translations,
                              moving_idx, static_idx, handle_values)
        
        return loss_dict
    
    def postprocess(self):
        ckpt_dir = self.logger.dir + '/checkpoints/'
        print("Saving model weights in: {}".format(ckpt_dir))
        os.makedirs(ckpt_dir, exist_ok=True)
        torch.save(self.model.state_dict(), ckpt_dir + '/neural_rotation.pt')


@dataclass
class DeformTrainerConfig(InstantiateConfig):

    _target: Type = field(default_factory=lambda: DeformTrainer)

    num_steps: int = 1
    pretrained_shape: Path = None
    handles_spec: Path = None
    delaunay_sample: int = 100
    zero_samples: int = 1000
    space_samples: int = 1000
    attempts_per_step: int = 10000
    near_surface_threshold: float = 0.05
    domain_bounds: Tuple[float, float] = (-1, 1)
    num_projections: int = 5
    plane_coords_scale: float = 0.02
    device: Literal['cpu', 'cuda'] = 'cuda'
    seed: int = 123456

    shape_model: NeuralSDFConfig = NeuralSDFConfig()
    rotation_model: NeuralRTFConfig = NeuralRTFConfig()
    loss: DeformationLossConfig = DeformationLossConfig()
    optimizer: AdamConfig = AdamConfig()
    scheduler: MultiStepSchedulerConfig = MultiStepSchedulerConfig()