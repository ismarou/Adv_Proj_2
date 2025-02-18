# Copyright (c) 2021-2023, NVIDIA Corporation
# All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are met:
#
# 1. Redistributions of source code must retain the above copyright notice, this
#    list of conditions and the following disclaimer.
#
# 2. Redistributions in binary form must reproduce the above copyright notice,
#    this list of conditions and the following disclaimer in the documentation
#    and/or other materials provided with the distribution.
#
# 3. Neither the name of the copyright holder nor the names of its
#    contributors may be used to endorse or promote products derived from
#    this software without specific prior written permission.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
# AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
# IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
# DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE
# FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL
# DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR
# SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER
# CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY,
# OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
# OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.

"""Factory: class for insertion env.

** Modified version of the insertion env, including the kuka and the Openhand **

Inherits base class and abstract environment class. Inherited by insertion task class. Not directly executed.

Configuration defined in FactoryEnvInsertionTactile.yaml. Asset info defined in factory_asset_info_insertion.yaml.
"""
import random

import hydra
import numpy as np
import os
import torch
from tqdm import tqdm

from isaacgym import gymapi
from isaacgyminsertion.tasks.factory_tactile.factory_base import FactoryBaseTactile
from isaacgyminsertion.tasks.factory_tactile.schema.factory_schema_class_env import FactoryABCEnv
from isaacgyminsertion.tasks.factory_tactile.schema.factory_schema_config_env import FactorySchemaConfigEnv
from isaacgyminsertion.allsight.experiments.allsight_render import AllSightRenderer
from isaacgyminsertion.tasks.utils.pcl_utils import CameraPointCloud
import isaacgyminsertion.tasks.factory_tactile.factory_control as fc
import trimesh
import open3d as o3d
from scipy.spatial.transform import Rotation as R
import omegaconf
import matplotlib.pyplot as plt
from isaacgyminsertion.utils import torch_jit_utils


class ExtrinsicContact:
    def __init__(
            self,
            mesh_obj,
            mesh_socket,
            obj_scale,
            socket_scale,
            socket_pos,
            num_envs,
            num_points=50,
            device='cuda:0',
            calc_contact=False,
    ) -> None:

        self.calc_contact = calc_contact

        # T = np.eye(4)
        # T[0:3, 0:3] = R.from_euler("xyz", [0, 0, 90], degrees=True).as_matrix()
        # self.object_trimesh = self.object_trimesh.apply_transform(T)

        self.socket_trimesh = trimesh.load(mesh_socket)
        self.reset_socket_trimesh = self.socket_trimesh.copy()
        self.socket_trimesh = self.socket_trimesh.apply_scale(socket_scale)
        # T = np.eye(4)
        # T[0:3, -1] = socket_pos
        # self.socket_trimesh.apply_transform(T)
        self.socket_pos = socket_pos
        #self.socket_pcl = trimesh.sample.sample_surface_even(self.socket_trimesh, num_points, seed=42)[0]
        self.socket_pcl = trimesh.sample.sample_surface_even(self.socket_trimesh, num_points)[0]
        self.socket_pc = trimesh.points.PointCloud(self.socket_pcl.copy())

        if calc_contact:
            self.socket = o3d.t.geometry.RaycastingScene()
            self.socket.add_triangles(
                o3d.t.geometry.TriangleMesh.from_legacy(self.socket_trimesh.as_open3d)
            )

        self.object_trimesh = trimesh.load(mesh_obj)
        self.object_trimesh = self.object_trimesh.apply_scale(obj_scale)
        #self.pointcloud_obj = trimesh.sample.sample_surface_even(self.object_trimesh, num_points, seed=42)[0]
        self.pointcloud_obj = trimesh.sample.sample_surface_even(self.object_trimesh, num_points)[0]
        self.object_pc = trimesh.points.PointCloud(self.pointcloud_obj.copy())

        self.n_points = num_points
        self.gt_extrinsic_contact = torch.zeros((num_envs, self.n_points))
        self.first_init = True
        self.num_envs = num_envs
        self.device = device
        self.plug_pose_no_rot = np.repeat(np.eye(4)[np.newaxis, :, :], num_envs, axis=0)

    def _xyzquat_to_tf_numpy(self, position_quat: np.ndarray) -> np.ndarray:
        """
        convert [x, y, z, qx, qy, qz, qw] to 4 x 4 transformation matrices
        """
        position_quat = np.atleast_2d(position_quat)  # (N, 7)
        N = position_quat.shape[0]
        T = np.zeros((N, 4, 4))
        T[:, 0:3, 0:3] = R.from_quat(position_quat[:, 3:]).as_matrix()
        T[:, :3, 3] = position_quat[:, :3]
        T[:, 3, 3] = 1
        return T.squeeze()

    def reset_extrinsic_contact(self):
        self.gt_extrinsic_contact *= 0
        self.step = 0

    def apply_transform(self, poses, pc_vertices):
        count, dim = pc_vertices.shape
        pc_vertices_wtrans = np.column_stack((pc_vertices, np.ones(count)))
        stack = np.repeat(pc_vertices_wtrans[np.newaxis, ...], poses.shape[0], axis=0)
        transformed = np.matmul(poses, np.transpose(stack, (0, 2, 1)))
        transformed = np.transpose(transformed, (0, 2, 1))[..., :3]
        return transformed

    def reset_socket_pos(self, socket_pos):
        self.socket_trimesh = self.reset_socket_trimesh.copy()
        self.socket_trimesh = self.socket_trimesh.apply_scale(1.0)
        self.socket_pos = socket_pos
        T = np.eye(4)
        T[0:3, -1] = self.socket_pos
        self.socket_trimesh.apply_transform(T)
        
        # Create Open3D mesh from Trimesh
        vertices = np.asarray(self.socket_trimesh.vertices)
        faces = np.asarray(self.socket_trimesh.faces)
        o3d_mesh = o3d.geometry.TriangleMesh()
        o3d_mesh.vertices = o3d.utility.Vector3dVector(vertices)
        o3d_mesh.triangles = o3d.utility.Vector3iVector(faces)
        
        # Create raycasting scene
        self.socket = o3d.t.geometry.RaycastingScene()
        self.socket.add_triangles(o3d.t.geometry.TriangleMesh.from_legacy(o3d_mesh))
        
        self.socket_pcl = trimesh.sample.sample_surface_even(self.socket_trimesh, self.n_points)[0]
        self.plug_pose_no_rot = np.repeat(np.eye(4)[np.newaxis, :, :], self.num_envs, axis=0)

    def estimate_pose(self, curr_pose, prev_pose=None):
        '''
        Make the pose invariant to z-axis rotation
        Source: https://github.com/shiyoung77/tensegrity_perception/blob/main/tracking.py#L570
        Credit: Shiyang Lu
        '''

        curr_pos = curr_pose[:3, 3]
        curr_rot = curr_pose[:3, :3]

        curr_z_dir = curr_rot[:, 2]
        curr_z_dir /= np.linalg.norm(curr_z_dir)

        if prev_pose is None:
            prev_pose = np.eye(4)

        prev_rot = prev_pose[:3, :3]
        prev_z_dir = prev_rot[:, 2]

        delta_rot = np.eye(3)
        cos_dist = prev_z_dir @ curr_z_dir
        if not np.allclose(cos_dist, 1):
            axis = np.cross(prev_z_dir, curr_z_dir)
            axis = axis / np.linalg.norm(axis)
            angle = np.arccos(cos_dist)
            delta_rot = R.from_rotvec(angle * axis).as_matrix()

        tf_curr_pose = np.eye(4)
        tf_curr_pose[:3, :3] = delta_rot @ prev_rot
        tf_curr_pose[:3, 3] = curr_pos
        return tf_curr_pose

    def estimate_pose_batch(self, curr_poses, prev_poses):
        '''
        Make the pose invariant to z-axis rotation - batched version
        Source: https://github.com/shiyoung77/tensegrity_perception/blob/main/tracking.py#L570
        Credit: Shiyang Lu
        '''
        # Determine the batch size based on the first dimension of curr_poses
        batch_size = curr_poses.shape[0]

        # Extract the position and rotation components from the current poses
        curr_pos = curr_poses[:, :3, 3]
        curr_rots = curr_poses[:, :3, :3]

        # Normalize the z-direction vectors of the current rotations
        curr_z_dirs = curr_rots[:, :, 2]
        curr_z_dirs /= np.linalg.norm(curr_z_dirs, axis=1, keepdims=True)

        # Initialize previous poses to identity matrices if None are provided
        # if prev_poses is None:
        #     prev_poses = np.repeat(np.eye(4)[np.newaxis, :, :], batch_size, axis=0)

        # Extract the rotation components from the previous poses
        prev_rots = prev_poses[:, :3, :3]
        prev_z_dirs = prev_rots[:, :, 2]

        # Compute the cosine distances between current and previous z-direction vectors
        cos_dists = np.einsum('ij,ij->i', prev_z_dirs, curr_z_dirs)

        # Determine where the rotation is negligible (cosine of angle close to 1)
        no_rotation_needed = np.isclose(cos_dists, 1.0)

        # Compute the axes of rotation as the cross product of z-direction vectors
        axes = np.cross(prev_z_dirs, curr_z_dirs)

        # Normalize the axes and handle divide-by-zero issues
        norms = np.linalg.norm(axes, axis=1, keepdims=True)
        axes = np.where(norms > 0.0, axes / norms, np.zeros_like(axes))

        # Compute the angles for rotation
        angles = np.arccos(np.clip(cos_dists, -1.0, 1.0))
        angles = angles[:, np.newaxis]

        # Calculate rotation vectors and create rotation matrices
        rotation_vectors = angles * axes
        delta_rots = R.from_rotvec(rotation_vectors.reshape(-1, 3)).as_matrix()
        delta_rots = delta_rots.reshape(batch_size, 3, 3)

        # For cases where no rotation is needed, replace with identity matrices
        delta_rots[no_rotation_needed] = np.eye(3)

        # Replace rotation matrices corresponding to zero angles with identity matrices
        # zero_angle_indices = np.isclose(angles.flatten(), 0)
        # delta_rots[zero_angle_indices] = np.eye(3)

        # Initialize the transformed current poses array
        tf_curr_poses = np.empty((batch_size, 4, 4))

        # Combine the delta rotations with the previous rotations and positions
        tf_curr_poses[:, :3, :3] = np.matmul(delta_rots, prev_rots)
        tf_curr_poses[:, :3, 3] = curr_pos

        # Set the last row to [0, 0, 0, 1] for each pose
        tf_curr_poses[:, 3, :] = np.array([0, 0, 0, 1])

        return tf_curr_poses

    def get_extrinsic_contact(self, obj_pos, obj_quat, socket_pos, socket_quat, plug_scale,
                              threshold=0.002, display=False):

        object_poses = torch.cat((obj_pos, obj_quat), dim=1)
        object_poses = self._xyzquat_to_tf_numpy(object_poses.cpu().numpy())

        socket_poses = torch.cat((socket_pos, socket_quat), dim=1)
        socket_poses = self._xyzquat_to_tf_numpy(socket_poses.cpu().numpy())

        if len(object_poses.shape) == 2:
            object_poses = object_poses[None, ...]
        if len(socket_poses.shape) == 2:
            socket_poses = socket_poses[None, ...]

        # query_points = self.apply_transform(self.plug_pose_no_rot, self.object_pc.copy().vertices)
        query_points = self.apply_transform(object_poses, self.object_pc.copy().vertices)

        di = self.socket.compute_distance(o3d.core.Tensor.from_numpy(query_points.astype(np.float32))).numpy()

        d = di.copy().flatten()
        idx_2 = np.where(d > threshold)[0]
        d[idx_2] = threshold
        d = np.clip(d, 0.0, threshold)

        # TODO convert to Neural implicit representations https://arxiv.org/pdf/1812.03828.pdf?
        d = 1.0 - d / threshold
        d = np.clip(d, 0.0, 1.0)
        d[d > 0.1] = 1.0
        #
        indices = np.where(d == 1.0)[0]
        if len(indices) > 0:
            np.random.shuffle(indices)
            num_idx = int(d[indices].sum() * np.random.uniform(0.0, 0.1))
            indices = indices[:num_idx]
            d[indices] = 0.0

        # Display
        if display:
            if self.first_init:
                self.ax = plt.axes(projection='3d')
                self.first_init = False

            display_id = 0
            self.ax.plot(self.socket_pcl[:, 0], self.socket_pcl[:, 1], self.socket_pcl[:, 2], 'yo')
            self.ax.plot(query_points[display_id, :, 0], query_points[display_id, :, 1], query_points[display_id, :, 2],
                         'ko')
            self.ax.set_xlabel('X')
            self.ax.set_ylabel('Y')

            # intersecting_indices = d < threshold
            intersecting_indices = (d == 1.0).reshape(-1, self.n_points)
            contacts = np.zeros_like(query_points)
            contacts[intersecting_indices] = query_points[intersecting_indices]
            for c in contacts[display_id]:
                if np.linalg.norm(c, axis=0):
                    self.ax.plot(c[0], c[1], c[2], 'ro')

            plt.pause(0.0001)
            self.ax.cla()

        return torch.from_numpy(np.array(d)).view(-1, self.n_points)

    def get_pcl(self, obj_pos, obj_quat, socket_pos, socket_quat, display=True):

        object_poses = torch.cat((obj_pos, obj_quat), dim=1)
        object_poses = self._xyzquat_to_tf_numpy(object_poses.cpu().numpy())

        socket_poses = torch.cat((socket_pos, socket_quat), dim=1)
        socket_poses = self._xyzquat_to_tf_numpy(socket_poses.cpu().numpy())

        if len(object_poses.shape) == 2:
            object_poses = object_poses[None, ...]
        if len(socket_poses.shape) == 2:
            socket_poses = socket_poses[None, ...]

        query_points_plug = self.apply_transform(object_poses, self.object_pc.copy().vertices)
        query_points_plug_goal = self.apply_transform(socket_poses, self.object_pc.copy().vertices)
        query_points_socket = self.apply_transform(socket_poses, self.socket_pc.copy().vertices)

        # Display
        if display:
            if self.first_init:
                self.ax = plt.axes(projection='3d')
                self.first_init = False

            display_id = 1
            self.ax.plot(query_points_plug[display_id, :, 0],
                         query_points_plug[display_id, :, 1],
                         query_points_plug[display_id, :, 2], 'ko')

            self.ax.plot(query_points_plug_goal[display_id, :, 0],
                         query_points_plug_goal[display_id, :, 1],
                         query_points_plug_goal[display_id, :, 2], 'ro')
            #
            self.ax.plot(query_points_socket[display_id, :, 0],
                         query_points_socket[display_id, :, 1],
                         query_points_socket[display_id, :, 2], 'go')

            self.ax.set_xlabel('X')
            self.ax.set_ylabel('Y')
            plt.pause(0.0001)
            self.ax.cla()

        merged_point_cloud = np.concatenate(
            [query_points_plug, query_points_plug_goal, query_points_socket], axis=1
        )

        num_points = query_points_plug.shape[1]
        sampled_indices = np.random.choice(merged_point_cloud.shape[1], num_points, replace=False)
        sampled_point_cloud = merged_point_cloud[:, sampled_indices, :]

        return torch.from_numpy(sampled_point_cloud).flatten(start_dim=1).float()

    def merge_goal_pcl(self, pcl, socket_pos, socket_quat, plug_scale, display=False):

        socket_poses = torch.cat((socket_pos, socket_quat), dim=1)
        socket_poses = self._xyzquat_to_tf_numpy(socket_poses.cpu().numpy())

        if len(socket_poses.shape) == 2:
            socket_poses = socket_poses[None, ...]

        object_pc_vertices = self.object_pc.copy().vertices

        scaled_query_points = []
        for i in range(plug_scale.shape[0]):
            scaled_pc_vertices = object_pc_vertices * plug_scale[i].item()
            transformed_pc = self.apply_transform(socket_poses[i][None, ...], scaled_pc_vertices)
            scaled_query_points.append(transformed_pc)

        query_points_plug_goal = torch.from_numpy(np.stack(scaled_query_points)).to(self.device).squeeze(1)

        # query_points_plug_goal = self.apply_transform(socket_poses, object_pc_vertices)
        # query_points_plug_goal = torch.from_numpy(query_points_plug_goal).float().to(self.device)

        merged_point_cloud = torch.cat([pcl, query_points_plug_goal], dim=1)
        sampled_indices = torch.randperm(merged_point_cloud.size(1))[:pcl.size(1)]
        sampled_point_cloud = merged_point_cloud[:, sampled_indices, :]

        # Display
        if display:
            if self.first_init:
                self.ax = plt.axes(projection='3d')
                self.first_init = False

            display_id = 1
            query_points_plug_goal = query_points_plug_goal.cpu().detach().numpy()
            pcl = pcl.cpu().detach().numpy()

            self.ax.plot(query_points_plug_goal[display_id, :, 0],
                         query_points_plug_goal[display_id, :, 1],
                         query_points_plug_goal[display_id, :, 2], 'ro')

            self.ax.plot(pcl[display_id, :, 0],
                         pcl[display_id, :, 1],
                         pcl[display_id, :, 2], 'ko')

            self.ax.set_xlabel('X')
            self.ax.set_ylabel('Y')
            plt.pause(0.0001)
            self.ax.cla()

        return sampled_point_cloud.flatten(start_dim=1).float()

    def get_goal_pcl(self, socket_pos, socket_quat, plug_scale, display=False):

        socket_poses = torch.cat((socket_pos, socket_quat), dim=1)
        socket_poses = self._xyzquat_to_tf_numpy(socket_poses.cpu().numpy())

        if len(socket_poses.shape) == 2:
            socket_poses = socket_poses[None, ...]

        object_pc_vertices = self.object_pc.copy().vertices

        scaled_query_points = []
        for i in range(plug_scale.shape[0]):
            scaled_pc_vertices = object_pc_vertices * plug_scale[i].item()
            transformed_pc = self.apply_transform(socket_poses[i][None, ...], scaled_pc_vertices)
            scaled_query_points.append(transformed_pc)

        query_points_plug_goal = torch.from_numpy(np.stack(scaled_query_points)).to(self.device).squeeze(1)
        # sampled_indices = torch.randperm(query_points_plug_goal.size(1))[:query_points_plug_goal.size(1)]
        # query_points_plug_goal = query_points_plug_goal[:, sampled_indices, :]

        # Display
        if display:
            if self.first_init:
                self.ax = plt.axes(projection='3d')
                self.first_init = False

            display_id = 0
            query_points_plug_goal_dsp = query_points_plug_goal.cpu().detach().numpy()

            self.ax.plot(query_points_plug_goal_dsp[display_id, :, 0],
                         query_points_plug_goal_dsp[display_id, :, 1],
                         query_points_plug_goal_dsp[display_id, :, 2], 'ro')

            self.ax.set_xlabel('X')
            self.ax.set_ylabel('Y')
            plt.pause(0.0001)
            self.ax.cla()

        return query_points_plug_goal.float()


class FactoryEnvInsertionTactile(FactoryBaseTactile, FactoryABCEnv):

    def __init__(self, cfg, rl_device, sim_device, graphics_device_id, headless, virtual_screen_capture, force_render):
        """Initialize instance variables. Initialize environment superclass. Acquire tensors."""
        self.cfg = cfg
        self.video_frames = []
        self.ft_frames = []
        self.initial_grasp_poses = {}
        self.total_init_poses = {}
        self.init_socket_pos = {}
        self.init_socket_quat = {}
        self.init_plug_pos = {}
        self.init_plug_quat = {}
        self.init_dof_pos = {}
        self._get_env_yaml_params()

        super().__init__(cfg, rl_device, sim_device, graphics_device_id, headless, virtual_screen_capture, force_render)

        self.acquire_base_tensors()  # defined in superclass
        self._acquire_env_tensors()
        self.refresh_base_tensors()  # defined in superclass
        self.refresh_env_tensors()

        # defining video recording params, todo: where do we put this?
        self.record_now = False
        self.record_now_ft = False
        self.complete_video_frames = None
        self.complete_ft_frames = None

    def _get_env_yaml_params(self):
        """Initialize instance variables from YAML files."""

        cs = hydra.core.config_store.ConfigStore.instance()
        cs.store(name='factory_schema_config_env', node=FactorySchemaConfigEnv)

        config_path = 'task/FactoryEnvInsertionTactile.yaml'  # relative to Gym's Hydra search path (cfg dir)
        self.cfg_env = hydra.compose(config_name=config_path)
        self.cfg_env = self.cfg_env['task']  # strip superfluous nesting

        asset_info_path = '../../assets/factory/yaml/factory_asset_info_insertion.yaml'  # relative to Gym's Hydra search path (cfg dir)
        self.asset_info_insertion = hydra.compose(config_name=asset_info_path)
        self.asset_info_insertion = self.asset_info_insertion['']['']['']['']['']['']['assets']['factory'][
            'yaml']  # strip superfluous nesting

        self.cfg_tactile = omegaconf.OmegaConf.create(self.cfg['tactile'])

        self.external_cam = self.cfg['external_cam']['external_cam']
        self.pcl_cam = self.cfg['external_cam']['pcl_cam']
        self.seg_cam = self.cfg['external_cam']['seg_cam']
        self.depth_cam = self.cfg['external_cam']['depth_cam']

        self.res = [self.cfg['external_cam']['cam_res']['w'], self.cfg['external_cam']['cam_res']['h']]
        self.cam_type = self.cfg['external_cam']['cam_type']
        self.save_im = self.cfg['external_cam']['save_im']
        self.near_clip = self.cfg['external_cam']['near_clip']
        self.far_clip = self.cfg['external_cam']['far_clip']
        self.dis_noise = self.cfg['external_cam']['dis_noise']

        self.randomize = self.cfg_env.randomize.domain_randomize
        self.randomization_params = self.cfg_env.randomize.randomization_params

    def make_handle_trans(self, width, height, env_idx, trans, rot, hfov=None):
        camera_props = gymapi.CameraProperties()
        camera_props.width = width
        camera_props.height = height
        camera_props.enable_tensors = True
        hfov = 70 + random.randint(-5, 5)
        if hfov is not None:
            camera_props.horizontal_fov = hfov
        camera_handle = self.gym.create_camera_sensor(self.envs[env_idx], camera_props)
        local_transform = gymapi.Transform()
        local_transform.p = gymapi.Vec3(*trans)
        local_transform.r = gymapi.Quat.from_euler_zyx(*rot)
        return camera_handle, local_transform, camera_props

    def create_envs(self):
        """Set env options. Import assets. Create actors."""

        lower = gymapi.Vec3(-self.cfg_base.env.env_spacing, -self.cfg_base.env.env_spacing, 0.0)
        upper = gymapi.Vec3(self.cfg_base.env.env_spacing, self.cfg_base.env.env_spacing, self.cfg_base.env.env_spacing)
        num_per_row = int(np.sqrt(self.num_envs))

        self.print_sdf_warning()
        kuka_asset, table_asset = self.import_kuka_assets()
        plug_assets, socket_assets = self._import_env_assets()
        self._create_actors(lower, upper, num_per_row, kuka_asset, plug_assets, socket_assets, table_asset)
        self.print_sdf_finish()

        for subassembly in self.cfg_env.env.desired_subassemblies:
            self._initialize_grasp_poses(subassembly)

    def _initialize_grasp_poses(self, subassembly, pre_noise=True, add_noise=False):
        try:
            sf = subassembly + '_noise' if pre_noise else subassembly
            self.initial_grasp_poses[subassembly] = np.load(f'initial_grasp_data/{sf}.npz')
        except:
            print('Failed to load initial grasp data for, ', subassembly)
            return

        self.total_init_poses[subassembly] = self.initial_grasp_poses[subassembly]['socket_pos'].shape[0]
        self.init_socket_pos[subassembly] = torch.zeros((self.total_init_poses[subassembly], 3))
        self.init_socket_quat[subassembly] = torch.zeros((self.total_init_poses[subassembly], 4))
        self.init_plug_pos[subassembly] = torch.zeros((self.total_init_poses[subassembly], 3))
        self.init_plug_quat[subassembly] = torch.zeros((self.total_init_poses[subassembly], 4))
        self.init_dof_pos[subassembly] = torch.zeros((self.total_init_poses[subassembly], 15))

        socket_pos = self.initial_grasp_poses[subassembly]['socket_pos']
        socket_quat = self.initial_grasp_poses[subassembly]['socket_quat']

        if add_noise:
            socket_pos = self.add_socket_noise(socket_pos)
            socket_quat = self.add_socket_quat_noise(socket_quat)

        plug_pos = self.initial_grasp_poses[subassembly]['plug_pos']
        plug_quat = self.initial_grasp_poses[subassembly]['plug_quat']
        dof_pos = self.initial_grasp_poses[subassembly]['dof_pos']

        # Convert quaternions to tensors
        plug_quat_tensor = torch.from_numpy(plug_quat).clone()

        from isaacgyminsertion.utils.torch_jit_utils import get_euler_xyz
        # Calculate roll, pitch, yaw from quaternions
        roll, pitch, yaw = get_euler_xyz(plug_quat_tensor)
        roll[roll > np.pi] -= 2 * np.pi
        pitch[pitch > np.pi] -= 2 * np.pi
        yaw[yaw > np.pi] -= 2 * np.pi

        # Set a threshold for the maximum allowable angle (in radians)
        max_ang = 0.4

        # Identify the extreme cases based on roll, pitch, and yaw
        degrasp_buf = (torch.abs(roll) > max_ang) | (torch.abs(pitch) > max_ang) | (torch.abs(yaw) > max_ang)

        # Filter out extreme cases
        valid_indices = ~degrasp_buf
        print('removed:', len(degrasp_buf.nonzero()))
        socket_pos = socket_pos[valid_indices]
        socket_quat = socket_quat[valid_indices]
        plug_pos = plug_pos[valid_indices]
        plug_quat = plug_quat[valid_indices]
        dof_pos = dof_pos[valid_indices]

        # Update the total number of valid initial poses after filtering
        self.total_init_poses[subassembly] = valid_indices.sum().item()

        self.init_socket_pos[subassembly] = torch.zeros((self.total_init_poses[subassembly], 3))
        self.init_socket_quat[subassembly] = torch.zeros((self.total_init_poses[subassembly], 4))
        self.init_plug_pos[subassembly] = torch.zeros((self.total_init_poses[subassembly], 3))
        self.init_plug_quat[subassembly] = torch.zeros((self.total_init_poses[subassembly], 4))
        self.init_dof_pos[subassembly] = torch.zeros((self.total_init_poses[subassembly], 15))

        print("Loading Grasping poses for:", subassembly)
        for i in tqdm(range(self.total_init_poses[subassembly])):
            self.init_socket_pos[subassembly][i] = torch.from_numpy(socket_pos[i])
            self.init_socket_quat[subassembly][i] = torch.from_numpy(socket_quat[i])
            self.init_plug_pos[subassembly][i] = torch.from_numpy(plug_pos[i])
            self.init_plug_quat[subassembly][i] = torch.from_numpy(plug_quat[i])
            self.init_dof_pos[subassembly][i] = torch.from_numpy(dof_pos[i])

    def add_socket_noise(self, socket_pos):
        num_positions = len(socket_pos)

        # Randomize socket pos
        socket_noise_xy = 2 * (np.random.rand(num_positions, 2) - 0.5)
        noise_scale = np.diag(self.cfg['randomize']['socket_pos_xy_noise'])
        socket_noise_xy = socket_noise_xy @ noise_scale

        socket_noise_z_mag = (
                self.cfg['randomize']['socket_pos_z_noise_bounds'][1]
                - self.cfg['randomize']['socket_pos_z_noise_bounds'][0]
        )
        socket_noise_z = (
                socket_noise_z_mag * np.random.rand(num_positions)
                + self.cfg['randomize']['socket_pos_z_noise_bounds'][0]
        )

        socket_pos[:, 0] = (
                self.cfg['randomize']['socket_pos_xy_initial'][0]
                + socket_noise_xy[:, 0]
        )
        socket_pos[:, 1] = (
                self.cfg['randomize']['socket_pos_xy_initial'][1]
                + socket_noise_xy[:, 1]
        )
        socket_pos[:, 2] = self.cfg_base.env.table_height + socket_noise_z

        return socket_pos

    def add_socket_quat_noise(self, socket_quat):

        num_positions = len(socket_quat)

        # Randomize socket quat
        socket_rot_noise = 2 * (np.random.rand(num_positions, 3) - 0.5)
        noise_scale = np.diag(self.cfg['randomize']['socket_rot_euler_noise'])
        socket_rot_noise = socket_rot_noise @ noise_scale
        socket_quat[:, :] = R.from_euler('xyz', socket_rot_noise).as_quat()

        return socket_quat

    def _import_env_assets(self):
        """Set plug and socket asset options. Import assets."""
        self.plug_files, self.socket_files = [], []
        urdf_root = os.path.join(os.path.dirname(__file__), '..', '..', '..', 'assets', 'factory', 'urdf')

        plug_options = gymapi.AssetOptions()
        plug_options.flip_visual_attachments = False
        plug_options.fix_base_link = False
        plug_options.thickness = 0.0  # default = 0.02
        plug_options.armature = 0.0  # default = 0.0
        plug_options.use_physx_armature = True
        plug_options.linear_damping = 0.5  # default = 0.0  TODO
        plug_options.max_linear_velocity = 1000.0  # default = 1000.0
        plug_options.angular_damping = 0.5  # default = 0.5
        plug_options.max_angular_velocity = 64.0  # default = 64.0
        plug_options.disable_gravity = True
        plug_options.enable_gyroscopic_forces = True
        plug_options.default_dof_drive_mode = gymapi.DOF_MODE_NONE
        # plug_options.vhacd_enabled = True  # convex decomposition
        plug_options.use_mesh_materials = False
        if self.cfg_base.mode.export_scene:
            plug_options.mesh_normal_mode = gymapi.COMPUTE_PER_FACE

        socket_options = gymapi.AssetOptions()
        socket_options.flip_visual_attachments = False
        socket_options.fix_base_link = True
        socket_options.thickness = 0.0  # default = 0.02
        socket_options.armature = 0.0  # default = 0.0
        socket_options.use_physx_armature = True
        socket_options.linear_damping = 0.0  # default = 0.0
        socket_options.max_linear_velocity = 1.0  # default = 1000.0
        socket_options.angular_damping = 0.0  # default = 0.5
        socket_options.max_angular_velocity = 64.0  # default = 64.0
        socket_options.disable_gravity = False
        socket_options.enable_gyroscopic_forces = True
        socket_options.default_dof_drive_mode = gymapi.DOF_MODE_NONE
        socket_options.use_mesh_materials = False
        if self.cfg_base.mode.export_scene:
            socket_options.mesh_normal_mode = gymapi.COMPUTE_PER_FACE

        plug_assets = []
        socket_assets = []
        for subassembly in self.cfg_env.env.desired_subassemblies:
            components = list(self.asset_info_insertion[subassembly])
            plug_file = self.asset_info_insertion[subassembly][components[0]]['urdf_path'] + '.urdf'
            socket_file = self.asset_info_insertion[subassembly][components[1]]['urdf_path'] + '.urdf'
            plug_options.density = self.asset_info_insertion[subassembly][components[0]]['density']
            socket_options.density = self.asset_info_insertion[subassembly][components[1]]['density'] * 1000
            plug_asset = self.gym.load_asset(self.sim, urdf_root, plug_file, plug_options)
            socket_asset = self.gym.load_asset(self.sim, urdf_root, socket_file, socket_options)
            plug_assets.append(plug_asset)
            socket_assets.append(socket_asset)

            # Save URDF file paths (for loading appropriate meshes during SAPU and SDF-Based Reward calculations)
            self.plug_files.append(os.path.join(urdf_root, plug_file))
            self.socket_files.append(os.path.join(urdf_root, socket_file))

        return plug_assets, socket_assets

    def get_real_camera_pose(self):
        cam_pos = np.array(self.cfg_env.external_cam.real_cam_pos)
        cam_ori = np.array(self.cfg_env.external_cam.real_cam_ori)
        cam_ori = R.from_quat(cam_ori).as_matrix()
        cam_T = np.eye(4)
        cam_T[:3, :3] = cam_ori
        cam_T[:3, 3] = cam_pos
        return cam_T

    def _create_actors(self, lower, upper, num_per_row, kuka_asset, plug_assets, socket_assets, table_asset):
        """Set initial actor poses. Create actors. Set shape and DOF properties."""

        kuka_pose = gymapi.Transform()
        kuka_pose.p.x = 0
        kuka_pose.p.y = 0.0
        kuka_pose.p.z = 0.0
        kuka_pose.r = gymapi.Quat(0.0, 0.0, 0.0, 1.0)

        table_pose = gymapi.Transform()
        table_pose.p.x = self.cfg_base.env.kuka_depth
        table_pose.p.y = 0.0
        table_pose.p.z = self.cfg_base.env.table_height * 0.5
        table_pose.r = gymapi.Quat(0.0, 0.0, 0.0, 1.0)

        self.envs_asset = {}
        self.envs = []
        self.kuka_handles = []
        self.plug_handles = []
        self.socket_handles = []
        self.table_handles = []
        self.shape_ids = []

        self.camera_handles = []
        self.camera_props = []
        self.kuka_actor_ids_sim = []  # within-sim indices
        self.plug_actor_ids_sim = []  # within-sim indices
        self.socket_actor_ids_sim = []  # within-sim indices
        self.table_actor_ids_sim = []  # within-sim indices

        self.fingertips = ['finger_1_3', 'finger_2_3', 'finger_3_3']  # left, right, bottom. same for all envs
        self.fingertip_handles = [self.gym.find_asset_rigid_body_index(kuka_asset, name) for name in self.fingertips]
        self.left_fingertip_handle = []
        self.right_fingertip_handle = []
        self.middle_fingertip_handle = []
        self.tactile_handles = []  # [num_envs , 3]

        actor_count = 0

        self.plug_heights = []
        self.plug_widths = []
        self.plug_depths = []
        self.plug_scale = []

        self.socket_heights = []
        self.socket_widths = []
        self.socket_depths = []
        self.socket_scale = []

        self.asset_indices = []

        self.all_rendering_camera = {}

        self.subassembly_extrinsic_contact = {}
        self.subassembly_pcd = {}

        # self.plug_pcd = torch.zeros((self.num_envs, self.cfg['env']['num_points'], 3), device=self.device)

        self.subassembly_to_env_ids = {}
        # Create wrist and fingertip force sensors
        sensor_pose = gymapi.Transform()
        for ft_handle in self.fingertip_handles:
            sensor_options = gymapi.ForceSensorProperties()
            sensor_options.enable_forward_dynamics_forces = False  # for example gravity
            sensor_options.enable_constraint_solver_forces = True  # for example contacts
            self.gym.create_asset_force_sensor(kuka_asset, ft_handle, sensor_pose, sensor_options)
        # wrist_ft_handle = self.gym.find_asset_rigid_body_index(kuka_asset, 'iiwa7_link_7')
        # self.gym.create_asset_force_sensor(kuka_asset, wrist_ft_handle, sensor_pose)

        from tqdm import tqdm

        for i in tqdm(range(self.num_envs)):

            # sample random subassemblies
            j = np.random.randint(0, len(self.cfg_env.env.desired_subassemblies))

            env_ptr = self.gym.create_env(self.sim, lower, upper, num_per_row)

            # compute aggregate size
            num_kuka_bodies = self.gym.get_asset_rigid_body_count(kuka_asset)
            num_kuka_shapes = self.gym.get_asset_rigid_shape_count(kuka_asset)
            num_plug_bodies = self.gym.get_asset_rigid_body_count(plug_assets[j])
            num_plug_shapes = self.gym.get_asset_rigid_shape_count(plug_assets[j])
            num_socket_bodies = self.gym.get_asset_rigid_body_count(socket_assets[j])
            num_socket_shapes = self.gym.get_asset_rigid_shape_count(socket_assets[j])
            num_table_bodies = self.gym.get_asset_rigid_body_count(table_asset)
            num_table_shapes = self.gym.get_asset_rigid_shape_count(table_asset)

            max_agg_bodies = num_kuka_bodies + num_plug_bodies + num_socket_bodies + num_table_bodies
            max_agg_shapes = num_kuka_shapes + num_plug_shapes + num_socket_shapes + num_table_shapes
            self.object_rb_handles = list(range(num_kuka_bodies, num_kuka_bodies + num_plug_bodies))
            # begin aggregation mode if enabled - this can improve simulation performance
            if self.cfg_env.env.aggregate_mode:
                self.gym.begin_aggregate(env_ptr, max_agg_bodies, max_agg_shapes, True)

            if self.cfg_env.sim.disable_kuka_collisions:
                kuka_handle = self.gym.create_actor(env_ptr, kuka_asset, kuka_pose, 'kuka', i + self.num_envs, 0, 1)
            else:
                kuka_handle = self.gym.create_actor(env_ptr, kuka_asset, kuka_pose, 'kuka', i, 0, 1)
            self.kuka_actor_ids_sim.append(actor_count)
            actor_count += 1

            subassembly = self.cfg_env.env.desired_subassemblies[j]
            components = list(self.asset_info_insertion[subassembly])
            # self.assembly_one_hot[i, j] = 1

            plug_pose = gymapi.Transform()
            # plug_pose.p.x = 0.0
            # plug_pose.p.y = self.cfg_env.env.plug_lateral_offset
            # plug_pose.p.z = self.cfg_base.env.table_height
            # plug_pose.r = gymapi.Quat(0.0, 0.0, 0.0, 1.0)
            plug_pose.p.x = self.cfg_base.env.kuka_depth
            plug_pose.p.y = self.cfg_env.env.plug_lateral_offset
            plug_pose.p.z = self.cfg_base.env.table_height
            plug_pose.r = gymapi.Quat(0.0, 0.0, 0.0, 1.0)

            plug_handle = self.gym.create_actor(env_ptr, plug_assets[j], plug_pose, 'plug', i, 0, 2)
            self.plug_actor_ids_sim.append(actor_count)
            actor_count += 1

            socket_pose = gymapi.Transform()
            socket_pose.p.x = self.cfg_base.env.kuka_depth
            socket_pose.p.y = 0.0
            socket_pose.p.z = self.cfg_base.env.table_height
            socket_pose.r = gymapi.Quat(0.0, 0.0, 0.0, 1.0)

            socket_handle = self.gym.create_actor(env_ptr, socket_assets[j], socket_pose, 'socket', i, 0, 3)
            self.socket_actor_ids_sim.append(actor_count)
            actor_count += 1

            table_handle = self.gym.create_actor(env_ptr, table_asset, table_pose, 'table', i, 0, 0)
            self.table_actor_ids_sim.append(actor_count)
            actor_count += 1

            link7_id = self.gym.find_actor_rigid_body_index(env_ptr, kuka_handle, 'iiwa7_link_7', gymapi.DOMAIN_ACTOR)
            hand_id = self.gym.find_actor_rigid_body_index(env_ptr, kuka_handle, 'gripper_base_link',
                                                           gymapi.DOMAIN_ACTOR)

            left_finger_id = self.gym.find_actor_rigid_body_index(env_ptr, kuka_handle, 'finger_1_3',
                                                                  gymapi.DOMAIN_ACTOR)
            right_finger_id = self.gym.find_actor_rigid_body_index(env_ptr, kuka_handle, 'finger_2_3',
                                                                   gymapi.DOMAIN_ACTOR)
            middle_finger_id = self.gym.find_actor_rigid_body_index(env_ptr, kuka_handle, 'finger_3_3',
                                                                    gymapi.DOMAIN_ACTOR)

            # useful for measuring the friction parameters (privileged information)
            self.left_finger_id = left_finger_id - 1
            self.right_finger_id = right_finger_id - 1
            self.middle_finger_id = middle_finger_id - 1

            self.shape_ids = [link7_id, hand_id, left_finger_id - 1, right_finger_id - 1, middle_finger_id - 1]

            kuka_shape_props = self.gym.get_actor_rigid_shape_properties(env_ptr, kuka_handle)

            for shape_id in self.shape_ids:
                kuka_shape_props[shape_id].friction = self.cfg_base.env.kuka_friction
                kuka_shape_props[shape_id].rolling_friction = 0.0  # default = 0.0
                kuka_shape_props[shape_id].torsion_friction = 0.0  # default = 0.0
                kuka_shape_props[shape_id].restitution = 0.0  # default = 0.0
                kuka_shape_props[shape_id].compliance = 0.0  # default = 0.0
                kuka_shape_props[shape_id].thickness = 0.0  # default = 0.0
            self.gym.set_actor_rigid_shape_properties(env_ptr, kuka_handle, kuka_shape_props)

            plug_shape_props = self.gym.get_actor_rigid_shape_properties(env_ptr, plug_handle)
            plug_shape_props[0].friction = self.cfg_env.env.plug_friction
            plug_shape_props[0].rolling_friction = 0.0  # default = 0.0
            plug_shape_props[0].torsion_friction = 0.0  # default = 0.0
            plug_shape_props[0].restitution = 0.0  # default = 0.0
            plug_shape_props[0].compliance = 0.0  # default = 0.0
            plug_shape_props[0].thickness = 0.0  # default = 0.0
            self.gym.set_actor_rigid_shape_properties(env_ptr, plug_handle, plug_shape_props)
            self.plug_scale.append(self.gym.get_actor_scale(env_ptr, plug_handle))

            socket_shape_props = self.gym.get_actor_rigid_shape_properties(env_ptr, socket_handle)
            socket_shape_props[0].friction = self.asset_info_insertion[subassembly][components[1]]['friction']
            socket_shape_props[0].rolling_friction = 0.0  # default = 0.0
            socket_shape_props[0].torsion_friction = 0.0  # default = 0.0
            socket_shape_props[0].restitution = 0.0  # default = 0.0
            socket_shape_props[0].compliance = 0.0  # default = 0.0
            socket_shape_props[0].thickness = 0.0  # default = 0.0
            self.gym.set_actor_rigid_shape_properties(env_ptr, socket_handle, socket_shape_props)
            self.socket_scale.append(self.gym.get_actor_scale(env_ptr, socket_handle))

            table_shape_props = self.gym.get_actor_rigid_shape_properties(env_ptr, table_handle)
            table_shape_props[0].friction = self.cfg_base.env.table_friction
            table_shape_props[0].rolling_friction = 0.0  # default = 0.0
            table_shape_props[0].torsion_friction = 0.0  # default = 0.0
            table_shape_props[0].restitution = 0.0  # default = 0.0
            table_shape_props[0].compliance = 0.0  # default = 0.0
            table_shape_props[0].thickness = 0.0  # default = 0.0
            self.gym.set_actor_rigid_shape_properties(env_ptr, table_handle, table_shape_props)

            self.kuka_num_dofs = self.gym.get_actor_dof_count(env_ptr, kuka_handle)

            self.gym.enable_actor_dof_force_sensors(env_ptr, kuka_handle)

            self.plug_heights.append(self.asset_info_insertion[subassembly][components[0]]['length'])
            self.socket_heights.append(self.asset_info_insertion[subassembly][components[1]]['height'])
            if (any('rectangular' in sub for sub in components) or
                    any('square' in sub for sub in components) or
                    any('triangle' in sub for sub in components) or
                    any('hexagon' in sub for sub in components) or
                    any('ellipse' in sub for sub in components) or
                    any('trapez' in sub for sub in components)
            ):
                self.plug_widths.append(self.asset_info_insertion[subassembly][components[0]]['width'])
                self.plug_depths.append(self.asset_info_insertion[subassembly][components[0]]['depth'])
                self.socket_widths.append(self.asset_info_insertion[subassembly][components[1]]['width'])
                self.socket_depths.append(self.asset_info_insertion[subassembly][components[1]]['depth'])
            else:
                self.plug_widths.append(self.asset_info_insertion[subassembly][components[0]]['diameter'])
                self.socket_widths.append(self.asset_info_insertion[subassembly][components[1]]['diameter'])
                self.plug_depths.append(self.asset_info_insertion[subassembly][components[0]]['diameter'])
                self.socket_depths.append(self.asset_info_insertion[subassembly][components[0]]['diameter'])

            self.asset_indices.append(j)
            self.envs.append(env_ptr)
            self.kuka_handles.append(kuka_handle)
            self.plug_handles.append(plug_handle)
            self.socket_handles.append(socket_handle)
            self.table_handles.append(table_handle)

            if self.external_cam:
                # add external cam
                self.pos_error_std = self.cfg_env.external_cam.cam_pos_noise
                self.point_error_std = self.cfg_env.external_cam.cam_point_noise
                self.ori_error_std = self.cfg_env.external_cam.cam_ori_error

                if self.cfg_env.external_cam.use_point:
                    self.init_camera_pos = (self.cfg_env.external_cam.x_init,
                                            self.cfg_env.external_cam.y_init,
                                            self.cfg_env.external_cam.z_init)

                    self.init_camera_point = (self.cfg_env.external_cam.x_point_init,
                                              self.cfg_env.external_cam.y_point_init,
                                              self.cfg_env.external_cam.z_point_init)

                    random_pos_error = np.random.normal(0, self.pos_error_std, 3)
                    random_point_error = np.random.normal(0, self.point_error_std, 3)

                    perturbed_position = np.array(self.init_camera_pos) + random_pos_error
                    perturbed_point = np.array(self.init_camera_point) + random_point_error

                    cam, _, props = self.make_handle_trans(self.res[0], self.res[1], i,
                                                           perturbed_position, perturbed_point)

                    self.gym.set_camera_location(cam, self.envs[i],
                                                 gymapi.Vec3(perturbed_position[0],
                                                             perturbed_position[1],
                                                             perturbed_position[2]),
                                                 gymapi.Vec3(perturbed_point[0],
                                                             perturbed_point[1],
                                                             perturbed_point[2]))

                if self.cfg_env.external_cam.use_real:

                    random_axis = np.random.normal(size=3)
                    random_axis /= np.linalg.norm(random_axis)
                    error_quat = R.from_rotvec(random_axis * np.radians(self.ori_error_std)).as_quat()

                    cam_T = self.get_real_camera_pose()
                    offset = np.array([
                        [0.0, -1.0, 0.0, 0.0],
                        [0.0, 0.0, -1.0, 0.0],
                        [1.0, 0.0, 0.0, 0.0],
                        [0.0, 0.0, 0.0, 1.0],
                    ])

                    cam_T = cam_T @ offset
                    cam_pos = cam_T[:3, 3].flatten()

                    cam_pos += np.random.normal(0, self.pos_error_std, 3)
                    cam_pose = gymapi.Transform()
                    cam_pose.p = gymapi.Vec3(*cam_pos)
                    cam_quat = R.from_matrix(cam_T[:3, :3]).as_quat()

                    cam_quat_with_error = R.from_quat(cam_quat) * R.from_quat(error_quat)
                    cam_pose.r = gymapi.Quat(*cam_quat_with_error.as_quat())

                    cam, _, props = self.make_handle_trans(self.res[0], self.res[1], i,
                                                           cam_pos, cam_pos)

                    self.gym.set_camera_transform(cam, self.envs[i], cam_pose)

                self.camera_handles.append(cam)
                self.camera_props.append(props)

            if subassembly not in self.all_rendering_camera:
                self.camera_props_viz = gymapi.CameraProperties()
                self.camera_props_viz.width = 1280
                self.camera_props_viz.height = 720
                self.all_rendering_camera[subassembly] = []
                self.all_rendering_camera[subassembly].append(i)

                cam1, trans1, _ = self.make_handle_trans(1280, 720, i, (0.8, 0.0, 0.3),
                                                         (np.deg2rad(0), np.deg2rad(40), np.deg2rad(180)))
                self.gym.attach_camera_to_body(
                    cam1,
                    self.envs[i],
                    kuka_handle,
                    trans1,
                    gymapi.FOLLOW_TRANSFORM,
                )

                self.all_rendering_camera[subassembly].append(cam1)

                cam2, trans2, _ = self.make_handle_trans(1280, 720, i, (0.7, 0.0, 0.1),
                                                         (np.deg2rad(0), np.deg2rad(30), np.deg2rad(180)))
                self.gym.attach_camera_to_body(
                    cam2,
                    self.envs[i],
                    kuka_handle,
                    trans2,
                    gymapi.FOLLOW_TRANSFORM,
                )

                self.all_rendering_camera[subassembly].append(cam2)

            # add Tactile modules for the tips
            self.envs_asset[i] = subassembly
            plug_file = self.asset_info_insertion[subassembly][components[0]]['urdf_path']
            plug_file += '_subdiv_3x.obj' if (('rectangular' in plug_file) or ('square' in plug_file)) else '.obj'
            socket_file = self.asset_info_insertion[subassembly][components[1]]['urdf_path']
            socket_file += '_subdiv_3x.obj' if 'factory' in plug_file else '.obj'

            mesh_root = os.path.join(os.path.dirname(__file__), '..', '..', '..', 'assets', 'factory', 'mesh',
                                     'factory_insertion')
            fix_scale = self.asset_info_insertion[subassembly][components[0]]['scale']

            if self.cfg['env']['tactile']:
                self.tactile_handles.append([AllSightRenderer(self.cfg_tactile,
                                                              os.path.join(mesh_root, plug_file),
                                                              randomize=True,
                                                              finger_idx=i,
                                                              scale=fix_scale)
                                             for i in range(len(self.fingertips))])

            if self.cfg['env']['compute_contact_gt'] or self.cfg['external_cam']['pcl_cam']:
                init_socket_pos = [0.5, 0, 0.001]
                if subassembly not in self.subassembly_extrinsic_contact:
                    self.subassembly_extrinsic_contact[subassembly] = ExtrinsicContact(
                        mesh_obj=os.path.join(mesh_root, plug_file),
                        mesh_socket=os.path.join(mesh_root, socket_file),
                        obj_scale=1.0,
                        socket_scale=1.0,
                        socket_pos=init_socket_pos,
                        num_envs=self.num_envs,
                        num_points=self.cfg['env']['num_points_goal'])

            # loading plug pcd
            if subassembly not in self.subassembly_pcd and False:
                object_trimesh = trimesh.load(os.path.join(mesh_root, plug_file))
                # object_trimesh = object_trimesh.apply_scale(object_trimesh)

                #pointcloud_obj = trimesh.sample.sample_surface(object_trimesh, self.cfg['env']['num_points'], seed=42)[0]
                pointcloud_obj = trimesh.sample.sample_surface(object_trimesh, self.cfg['env']['num_points'])[0]
                object_pc = trimesh.points.PointCloud(pointcloud_obj)
                self.subassembly_pcd[subassembly] = torch.from_numpy(object_pc.vertices).to(self.device).float()

            # self.plug_pcd[i, ...] = self.subassembly_pcd[subassembly]

            if subassembly not in self.subassembly_to_env_ids:
                self.subassembly_to_env_ids[subassembly] = []
            self.subassembly_to_env_ids[subassembly].append(i)

            if self.cfg_env.env.aggregate_mode:
                self.gym.end_aggregate(env_ptr)

        if self.external_cam and self.pcl_cam:
            self.pcl_generator = CameraPointCloud(isc_sim=self.sim,
                                                  isc_gym=self.gym,
                                                  envs=self.envs,
                                                  camera_handles=self.camera_handles,
                                                  camera_props=self.camera_props,
                                                  sample_num=self.cfg['env']['num_points'],
                                                  compute_device=self.device,
                                                  graphics_device=self.device,
                                                  pt_in_local=True)

        # Get indices
        self.num_actors = int(actor_count / self.num_envs)  # per env
        self.num_bodies = self.gym.get_env_rigid_body_count(env_ptr)  # per env
        self.num_dofs = self.gym.get_env_dof_count(env_ptr)  # per env

        # For setting targets
        self.kuka_actor_ids_sim = torch.tensor(self.kuka_actor_ids_sim, dtype=torch.int32, device=self.device)
        self.plug_actor_ids_sim = torch.tensor(self.plug_actor_ids_sim, dtype=torch.int32, device=self.device)
        self.socket_actor_ids_sim = torch.tensor(self.socket_actor_ids_sim, dtype=torch.int32, device=self.device)

        # For extracting root pos/quat
        self.plug_actor_id_env = self.gym.find_actor_index(env_ptr, 'plug', gymapi.DOMAIN_ENV)
        self.socket_actor_id_env = self.gym.find_actor_index(env_ptr, 'socket', gymapi.DOMAIN_ENV)

        # For extracting body pos/quat, force, and Jacobian
        self.robot_base_body_id_env = self.gym.find_actor_rigid_body_index(env_ptr, kuka_handle, "iiwa7_link_0",
                                                                           gymapi.DOMAIN_ENV)
        self.plug_body_id_env = self.gym.find_actor_rigid_body_index(env_ptr, plug_handle, 'plug', gymapi.DOMAIN_ENV)
        self.socket_body_id_env = self.gym.find_actor_rigid_body_index(env_ptr, socket_handle, 'socket',
                                                                       gymapi.DOMAIN_ENV)
        self.hand_body_id_env = self.gym.find_actor_rigid_body_index(env_ptr, kuka_handle, 'gripper_base_link',
                                                                     gymapi.DOMAIN_ENV)
        self.wrist_body_id_env = self.gym.find_actor_rigid_body_index(env_ptr, kuka_handle, 'dummy_ft_link',
                                                                      gymapi.DOMAIN_ENV)

        self.left_finger_body_id_env = self.gym.find_actor_rigid_body_index(env_ptr, kuka_handle, 'finger_1_3',
                                                                            gymapi.DOMAIN_ENV)
        self.right_finger_body_id_env = self.gym.find_actor_rigid_body_index(env_ptr, kuka_handle, 'finger_2_3',
                                                                             gymapi.DOMAIN_ENV)
        self.middle_finger_body_id_env = self.gym.find_actor_rigid_body_index(env_ptr, kuka_handle, 'finger_3_3',
                                                                              gymapi.DOMAIN_ENV)
        # Robot motion will be w.r.t this tf.
        self.fingertip_centered_body_id_env = self.gym.find_actor_rigid_body_index(env_ptr, kuka_handle,
                                                                                   'kuka_fingertip_centered',
                                                                                   gymapi.DOMAIN_ENV)

        self.kuka_joints_names = self.gym.get_asset_dof_names(kuka_asset)

        # For computing body COM pos
        self.plug_heights = torch.tensor(self.plug_heights, device=self.device).unsqueeze(-1)
        self.socket_heights = torch.tensor(self.socket_heights, device=self.device).unsqueeze(-1)

        # For setting initial state

        # For defining success or failure
        self.plug_widths = torch.tensor(self.plug_widths, device=self.device).unsqueeze(-1)

        # for extrinsic contact
        self.plug_scale = torch.tensor(self.plug_scale, device=self.device)
        self.socket_scale = torch.tensor(self.socket_scale, device=self.device)

        self.subassembly_to_env_ids = {k: torch.tensor(v, dtype=torch.long, device=self.device) for k, v in
                                       self.subassembly_to_env_ids.items()}

        self.object_rb_masses = [prop.mass for prop in self.gym.get_actor_rigid_body_properties(env_ptr, plug_handle)]
        self.object_rb_masses = torch_jit_utils.to_torch(self.object_rb_masses, dtype=torch.float, device=self.device)

    def _acquire_env_tensors(self):
        """Acquire and wrap tensors. Create views."""

        self.plug_pos = self.root_pos[:, self.plug_actor_id_env, 0:3]
        self.plug_quat = self.root_quat[:, self.plug_actor_id_env, 0:4]
        self.plug_linvel = self.root_linvel[:, self.plug_actor_id_env, 0:3]
        self.plug_angvel = self.root_angvel[:, self.plug_actor_id_env, 0:3]

        self.socket_pos = self.root_pos[:, self.socket_actor_id_env, 0:3]
        self.socket_quat = self.root_quat[:, self.socket_actor_id_env, 0:4]

        # TODO: Define socket height and plug height params in asset info YAML.
        self.plug_com_pos = fc.translate_along_local_z(pos=self.plug_pos,
                                                       quat=self.plug_quat,
                                                       offset=self.socket_heights + self.plug_heights * 1.0,
                                                       device=self.device)

        self.above_socket_pos = fc.translate_along_local_z(pos=self.socket_pos,
                                                           quat=self.socket_quat,
                                                           offset=self.socket_heights + self.plug_heights,
                                                           device=self.device)

        self.plug_com_quat = self.plug_quat  # always equal
        self.plug_com_linvel = self.plug_linvel + torch.cross(self.plug_angvel,
                                                              (self.plug_com_pos - self.plug_pos),
                                                              dim=1)
        self.plug_com_angvel = self.plug_angvel  # always equal

        self.socket_contact_force = self.contact_force[:, self.socket_actor_id_env, :3]

    def refresh_env_tensors(self):
        """Refresh tensors."""
        # NOTE: Tensor refresh functions should be called once per step, before setters.

        self.plug_com_pos = fc.translate_along_local_z(pos=self.plug_pos,
                                                       quat=self.plug_quat,
                                                       offset=self.plug_heights * 0.5,
                                                       device=self.device)

        self.plug_com_linvel = self.plug_linvel + torch.cross(self.plug_angvel,
                                                              (self.plug_com_pos - self.plug_pos),
                                                              dim=1)

        self.above_socket_pos = fc.translate_along_local_z(pos=self.socket_pos,
                                                           quat=self.socket_quat,
                                                           offset=self.socket_heights + self.plug_heights,
                                                           device=self.device)

        self.socket_tip = fc.translate_along_local_z(pos=self.socket_pos,
                                                     quat=self.socket_quat,
                                                     offset=self.socket_heights,
                                                     device=self.device)

        self.plug_tip = fc.translate_along_local_z(pos=self.plug_pos,
                                                   quat=self.plug_quat,
                                                   offset=self.plug_heights,
                                                   device=self.device)

    def _render_headless(self):

        if self.record_now and self.complete_video_frames is not None and len(self.complete_video_frames) == 0:

            video_frames = []
            for _, v in self.all_rendering_camera.items():
                env_id, camera_1, camera_2 = v[0], v[1], v[2]

                video_frame1 = self.gym.get_camera_image(self.sim,
                                                         self.envs[env_id],
                                                         camera_1,
                                                         gymapi.IMAGE_COLOR)

                video_frame1 = video_frame1.reshape((self.camera_props_viz.height, self.camera_props_viz.width, 4))

                video_frame2 = self.gym.get_camera_image(self.sim,
                                                         self.envs[env_id],
                                                         camera_2,
                                                         gymapi.IMAGE_COLOR)

                video_frame2 = video_frame2.reshape((self.camera_props_viz.height, self.camera_props_viz.width, 4))
                video_frames.append(np.concatenate((video_frame1, video_frame2), axis=1))

            self.video_frames.append(np.concatenate(video_frames, axis=0))

        if self.record_now_ft and self.complete_ft_frames is not None and len(self.complete_ft_frames) == 0:
            self.ft_frames.append(self.actions[:1].clone().cpu().numpy().squeeze())

    def start_recording(self):
        self.complete_video_frames = None
        self.record_now = True

    def start_recording_ft(self):
        self.complete_ft_frames = None
        self.record_now_ft = True

    def pause_recording(self):
        self.complete_video_frames = []
        self.video_frames = []
        self.record_now = False

    def pause_recording_ft(self):
        self.complete_ft_frames = []
        self.ft_frames = []
        self.record_now_ft = False

    def get_complete_frames(self):
        if self.complete_video_frames is None:
            return []
        return self.complete_video_frames

    def get_ft_frames(self):
        if self.complete_ft_frames is None:
            return []
        return self.complete_ft_frames
