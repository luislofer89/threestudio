import math
import random
from contextlib import contextmanager
from dataclasses import dataclass, field

import torch
import torch.nn as nn
import torch.nn.functional as F
from diffusers import (
    DDPMScheduler,
    DPMSolverMultistepScheduler,
    IFPipeline,
    UNet2DConditionModel,
)
from diffusers.loaders import AttnProcsLayers
from diffusers.models.attention_processor import (
    LoRAAttnAddedKVProcessor,
    LoRAAttnProcessor,
)
from diffusers.models.embeddings import TimestepEmbedding
from diffusers.utils.import_utils import is_xformers_available

import threestudio
from threestudio.models.prompt_processors.base import PromptProcessorOutput
from threestudio.utils.base import BaseModule
from threestudio.utils.misc import C, cleanup, parse_version
from threestudio.utils.typing import *


class ToWeightsDType(nn.Module):
    def __init__(self, module: nn.Module, dtype: torch.dtype):
        super().__init__()
        self.module = module
        self.dtype = dtype

    def forward(self, x: Float[Tensor, "..."]) -> Float[Tensor, "..."]:
        return self.module(x).to(self.dtype)


@threestudio.register("deep-floyd-vsd-guidance")
class DeepFloydVSDGuidance(BaseModule):
    @dataclass
    class Config(BaseModule.Config):
        pretrained_model_name_or_path: str = "DeepFloyd/IF-I-XL-v1.0"
        pretrained_model_name_or_path_lora: str = "DeepFloyd/IF-I-XL-v1.0"
        enable_memory_efficient_attention: bool = False
        enable_sequential_cpu_offload: bool = False
        enable_attention_slicing: bool = False
        enable_channels_last_format: bool = False
        guidance_scale: float = 7.5
        guidance_scale_lora: float = 1.0
        half_precision_weights: bool = True
        lora_cfg_training: bool = True
        lora_n_timestamp_samples: int = 1

        min_step_percent: float = 0.02
        max_step_percent: float = 0.98
        anneal_start_step: Optional[int] = 5000
        anneal_end_step: Optional[int] = 25000
        random_timestep: bool = True
        force_lora_v_prediction: bool = False
        anneal_strategy: str = "milestone"
        max_step_percent_annealed: float = 0.5

        view_dependent_prompting: bool = True
        camera_condition_type: str = "extrinsics"

    cfg: Config

    def configure(self) -> None:
        threestudio.info(f"Loading Deep Floyd ...")

        self.weights_dtype = (
            torch.float16 if self.cfg.half_precision_weights else torch.float32
        )

        pipe_kwargs = {
            "text_encoder": None,
            "safety_checker": None,
            "watermarker": None,
            "feature_extractor": None,
            "requires_safety_checker": False,
            "torch_dtype": self.weights_dtype,
            "variant": "fp16" if self.cfg.half_precision_weights else None,
        }

        pipe_lora_kwargs = {
            "text_encoder": None,
            "safety_checker": None,
            "watermarker": None,
            "feature_extractor": None,
            "requires_safety_checker": False,
            "torch_dtype": self.weights_dtype,
            "variant": "fp16" if self.cfg.half_precision_weights else None,
        }

        @dataclass
        class SubModules:
            pipe: IFPipeline
            pipe_lora: IFPipeline

        pipe = IFPipeline.from_pretrained(
            self.cfg.pretrained_model_name_or_path,
            **pipe_kwargs,
        ).to(self.device)
        if (
            self.cfg.pretrained_model_name_or_path
            == self.cfg.pretrained_model_name_or_path_lora
        ):
            self.single_model = True
            pipe_lora = pipe
        else:
            self.single_model = False
            pipe_lora = IFPipeline.from_pretrained(
                self.cfg.pretrained_model_name_or_path_lora,
                **pipe_lora_kwargs,
            ).to(self.device)
            cleanup()
        self.submodules = SubModules(pipe=pipe, pipe_lora=pipe_lora)

        if self.cfg.enable_memory_efficient_attention:
            if parse_version(torch.__version__) >= parse_version("2"):
                threestudio.info(
                    "PyTorch2.0 uses memory efficient attention by default."
                )
            elif not is_xformers_available():
                threestudio.warn(
                    "xformers is not available, memory efficient attention is not enabled."
                )
            else:
                self.pipe.enable_xformers_memory_efficient_attention()
                self.pipe_lora.enable_xformers_memory_efficient_attention()

        if self.cfg.enable_sequential_cpu_offload:
            self.pipe.enable_sequential_cpu_offload()
            self.pipe_lora.enable_sequential_cpu_offload()

        if self.cfg.enable_attention_slicing:
            self.pipe.enable_attention_slicing(1)
            self.pipe_lora.enable_attention_slicing(1)

        if self.cfg.enable_channels_last_format:
            self.pipe.unet.to(memory_format=torch.channels_last)
            self.pipe_lora.unet.to(memory_format=torch.channels_last)

        del self.pipe.text_encoder
        if not self.single_model:
            del self.pipe_lora.text_encoder
        cleanup()

        for p in self.unet.parameters():
            p.requires_grad_(False)
        for p in self.unet_lora.parameters():
            p.requires_grad_(False)

        # FIXME: hard-coded dims
        self.camera_embedding = ToWeightsDType(
            TimestepEmbedding(16, 2816), self.weights_dtype
        )
        self.unet_lora.class_embedding = self.camera_embedding

        # set up LoRA layers
        lora_attn_procs = {}
        # down_blocks.1.attentions.0.processor
        # down_blocks.1.attentions.1.processor
        # down_blocks.1.attentions.2.processor
        # down_blocks.2.attentions.0.processor
        # down_blocks.2.attentions.1.processor
        # down_blocks.2.attentions.2.processor
        # down_blocks.3.attentions.0.processor
        # down_blocks.3.attentions.1.processor
        # down_blocks.3.attentions.2.processor
        # up_blocks.0.attentions.0.processor
        # up_blocks.0.attentions.1.processor
        # up_blocks.0.attentions.2.processor
        # up_blocks.0.attentions.3.processor
        # up_blocks.1.attentions.0.processor
        # up_blocks.1.attentions.1.processor
        # up_blocks.1.attentions.2.processor
        # up_blocks.1.attentions.3.processor
        # up_blocks.2.attentions.0.processor
        # up_blocks.2.attentions.1.processor
        # up_blocks.2.attentions.2.processor
        # up_blocks.2.attentions.3.processor
        # mid_block.attentions.0.processor
        for name in self.unet_lora.attn_processors.keys():
            cross_attention_dim = self.unet_lora.config.cross_attention_dim
            if name.startswith("mid_block"):
                hidden_size = self.unet_lora.config.block_out_channels[-1]
            elif name.startswith("up_blocks"):
                block_id = int(name[len("up_blocks.")])
                hidden_size = list(reversed(self.unet_lora.config.block_out_channels))[
                    block_id
                ]
            elif name.startswith("down_blocks"):
                block_id = int(name[len("down_blocks.")])
                hidden_size = self.unet_lora.config.block_out_channels[block_id]

            lora_attn_procs[name] = LoRAAttnAddedKVProcessor(
                hidden_size=hidden_size, cross_attention_dim=cross_attention_dim
            )

        self.unet_lora.set_attn_processor(lora_attn_procs)

        self.lora_layers = AttnProcsLayers(self.unet_lora.attn_processors)
        self.lora_layers._load_state_dict_pre_hooks.clear()
        self.lora_layers._state_dict_hooks.clear()

        self.scheduler = DDPMScheduler.from_pretrained(
            self.cfg.pretrained_model_name_or_path,
            subfolder="scheduler",
            torch_dtype=self.weights_dtype,
        )

        self.scheduler_lora = DDPMScheduler.from_pretrained(
            self.cfg.pretrained_model_name_or_path_lora,
            subfolder="scheduler",
            torch_dtype=self.weights_dtype,
        )

        self.scheduler_sample = DPMSolverMultistepScheduler.from_config(
            self.pipe.scheduler.config
        )
        if self.cfg.force_lora_v_prediction:
            self.scheduler_lora = self.scheduler_lora.__class__.from_config(
                self.scheduler_lora.config,
                variance_type="fixed_small",
                prediction_type="v_prediction",
            )

            self.scheduler_lora_sample = DPMSolverMultistepScheduler.from_config(
                self.pipe_lora.scheduler.config,
                variance_type="fixed_small",
                prediction_type="v_prediction",
            )
        else:
            self.scheduler_lora = self.scheduler_lora.__class__.from_config(
                self.scheduler_lora.config,
                variance_type="fixed_small",
            )
            self.scheduler_lora_sample = DPMSolverMultistepScheduler.from_config(
                self.pipe_lora.scheduler.config,
                variance_type="fixed_small",
            )
        print(self.scheduler_lora.config.prediction_type)
        print(self.scheduler_lora_sample.config.prediction_type)

        self.pipe.scheduler = self.scheduler
        self.pipe_lora.scheduler = self.scheduler_lora

        self.num_train_timesteps = self.scheduler.config.num_train_timesteps
        self.min_step = int(self.num_train_timesteps * self.cfg.min_step_percent)
        self.max_step = int(self.num_train_timesteps * self.cfg.max_step_percent)

        self.alphas: Float[Tensor, "..."] = self.scheduler.alphas_cumprod.to(
            self.device
        )

        threestudio.info(f"Loaded Deep Floyd!")

    @property
    def pipe(self):
        return self.submodules.pipe

    @property
    def pipe_lora(self):
        return self.submodules.pipe_lora

    @property
    def unet(self):
        return self.submodules.pipe.unet

    @property
    def unet_lora(self):
        return self.submodules.pipe_lora.unet

    @torch.no_grad()
    @torch.cuda.amp.autocast(enabled=False)
    def _sample(
        self,
        pipe: IFPipeline,
        sample_scheduler: DPMSolverMultistepScheduler,
        text_embeddings: Float[Tensor, "BB N Nf"],
        num_inference_steps: int,
        guidance_scale: float,
        num_images_per_prompt: int = 1,
        height: Optional[int] = None,
        width: Optional[int] = None,
        class_labels: Optional[Float[Tensor, "BB 16"]] = None,
        cross_attention_kwargs: Optional[Dict[str, Any]] = None,
        generator: Optional[Union[torch.Generator, List[torch.Generator]]] = None,
    ) -> Float[Tensor, "B H W 3"]:
        height = height or pipe.unet.config.sample_size
        width = width or pipe.unet.config.sample_size
        batch_size = text_embeddings.shape[0] // 2
        device = self.device

        sample_scheduler.set_timesteps(num_inference_steps, device=device)
        timesteps = sample_scheduler.timesteps
        num_channels_latents = pipe.unet.config.in_channels

        latents = pipe.prepare_intermediate_images(
            batch_size * num_images_per_prompt,
            num_channels_latents,
            height,
            width,
            self.weights_dtype,
            device,
            generator,
        )

        for i, t in enumerate(timesteps):
            # expand the latents if we are doing classifier free guidance
            latent_model_input = torch.cat([latents] * 2)
            latent_model_input = sample_scheduler.scale_model_input(
                latent_model_input, t
            )

            # predict the noise residual
            if class_labels is None:
                with self.disable_unet_class_embedding(pipe.unet) as unet:
                    noise_pred = unet(
                        latent_model_input,
                        t,
                        encoder_hidden_states=text_embeddings.to(self.weights_dtype),
                        cross_attention_kwargs=cross_attention_kwargs,
                    ).sample
            else:
                noise_pred = pipe.unet(
                    latent_model_input,
                    t,
                    encoder_hidden_states=text_embeddings.to(self.weights_dtype),
                    class_labels=class_labels,
                    cross_attention_kwargs=cross_attention_kwargs,
                ).sample

            noise_pred_text, noise_pred_uncond = noise_pred.chunk(2)
            noise_pred_uncond, _ = noise_pred_uncond.split(3, dim=1)
            noise_pred_text, _ = noise_pred_text.split(3, dim=1)
            noise_pred = noise_pred_uncond + guidance_scale * (
                noise_pred_text - noise_pred_uncond
            )

            # compute the previous noisy sample x_t -> x_t-1
            latents = sample_scheduler.step(noise_pred, t, latents).prev_sample

        images = (latents / 2 + 0.5).clamp(0, 1)
        # we always cast to float32 as this does not cause significant overhead and is compatible with bfloat16
        images = images.permute(0, 2, 3, 1).float()
        return images

    def sample(
        self,
        prompt_utils: PromptProcessorOutput,
        elevation: Float[Tensor, "B"],
        azimuth: Float[Tensor, "B"],
        camera_distances: Float[Tensor, "B"],
        seed: int = 0,
        **kwargs,
    ) -> Float[Tensor, "N H W 3"]:
        # view-dependent text embeddings
        text_embeddings_vd = prompt_utils.get_text_embeddings(
            elevation,
            azimuth,
            camera_distances,
            view_dependent_prompting=self.cfg.view_dependent_prompting,
        )
        cross_attention_kwargs = {"scale": 0.0} if self.single_model else None
        generator = torch.Generator(device=self.device).manual_seed(seed)

        return self._sample(
            pipe=self.pipe,
            sample_scheduler=self.scheduler_sample,
            text_embeddings=text_embeddings_vd,
            num_inference_steps=25,
            guidance_scale=self.cfg.guidance_scale,
            cross_attention_kwargs=cross_attention_kwargs,
            generator=generator,
        )

    def sample_lora(
        self,
        prompt_utils: PromptProcessorOutput,
        elevation: Float[Tensor, "B"],
        azimuth: Float[Tensor, "B"],
        camera_distances: Float[Tensor, "B"],
        mvp_mtx: Float[Tensor, "B 4 4"],
        c2w: Float[Tensor, "B 4 4"],
        seed: int = 0,
        **kwargs,
    ) -> Float[Tensor, "N H W 3"]:
        # input text embeddings, view-independent
        text_embeddings = prompt_utils.get_text_embeddings(
            elevation, azimuth, camera_distances, view_dependent_prompting=False
        )

        if self.cfg.camera_condition_type == "extrinsics":
            camera_condition = c2w
        elif self.cfg.camera_condition_type == "mvp":
            camera_condition = mvp_mtx
        else:
            raise ValueError(
                f"Unknown camera_condition_type {self.cfg.camera_condition_type}"
            )

        B = elevation.shape[0]
        camera_condition_cfg = torch.cat(
            [
                camera_condition.view(B, -1),
                torch.zeros_like(camera_condition.view(B, -1)),
            ],
            dim=0,
        )

        generator = torch.Generator(device=self.device).manual_seed(seed)
        return self._sample(
            sample_scheduler=self.scheduler_lora_sample,
            pipe=self.pipe_lora,
            text_embeddings=text_embeddings,
            num_inference_steps=25,
            guidance_scale=self.cfg.guidance_scale_lora,
            class_labels=camera_condition_cfg,
            cross_attention_kwargs={"scale": 1.0},
            generator=generator,
        )

    @torch.cuda.amp.autocast(enabled=False)
    def forward_unet(
        self,
        unet: UNet2DConditionModel,
        latents: Float[Tensor, "..."],
        t: Float[Tensor, "..."],
        encoder_hidden_states: Float[Tensor, "..."],
        class_labels: Optional[Float[Tensor, "B 16"]] = None,
        cross_attention_kwargs: Optional[Dict[str, Any]] = None,
    ) -> Float[Tensor, "..."]:
        input_dtype = latents.dtype
        return unet(
            latents.to(self.weights_dtype),
            t.to(self.weights_dtype),
            encoder_hidden_states=encoder_hidden_states.to(self.weights_dtype),
            class_labels=class_labels,
            cross_attention_kwargs=cross_attention_kwargs,
        ).sample.to(input_dtype)

    @contextmanager
    def disable_unet_class_embedding(self, unet: UNet2DConditionModel):
        class_embedding = unet.class_embedding
        try:
            unet.class_embedding = None
            yield unet
        finally:
            unet.class_embedding = class_embedding

    def compute_grad_vsd(
        self,
        latents: Float[Tensor, "B 3 64 64"],
        text_embeddings_vd: Float[Tensor, "BB 77 768"],
        text_embeddings: Float[Tensor, "BB 77 768"],
        camera_condition: Float[Tensor, "B 4 4"],
    ):
        B = latents.shape[0]

        with torch.no_grad():
            # random timestamp
            if self.cfg.random_timestep:
                t = torch.randint(
                    self.min_step,
                    self.max_step + 1,
                    [B],
                    dtype=torch.long,
                    device=self.device,
                )
            else:
                t = torch.full([B], self.max_step, dtype=torch.long, device=self.device)
            # add noise
            noise = torch.randn_like(latents)
            latents_noisy = self.scheduler.add_noise(latents, noise, t)
            # pred noise
            latent_model_input = torch.cat([latents_noisy] * 2, dim=0)
            with self.disable_unet_class_embedding(self.unet) as unet:
                cross_attention_kwargs = {"scale": 0.0} if self.single_model else None
                noise_pred_pretrain = self.forward_unet(
                    unet,
                    latent_model_input,
                    torch.cat([t] * 2),
                    encoder_hidden_states=text_embeddings_vd,
                    cross_attention_kwargs=cross_attention_kwargs,
                )

            # use view-independent text embeddings in LoRA
            noise_pred_est = self.forward_unet(
                self.unet_lora,
                latent_model_input,
                torch.cat([t] * 2),
                encoder_hidden_states=text_embeddings,
                class_labels=torch.cat(
                    [
                        camera_condition.view(B, -1),
                        torch.zeros_like(camera_condition.view(B, -1)),
                    ],
                    dim=0,
                ),
                cross_attention_kwargs={"scale": 1.0},
            )

        (
            noise_pred_pretrain_text,
            noise_pred_pretrain_uncond,
        ) = noise_pred_pretrain.chunk(2)

        noise_pred_pretrain_text, _ = noise_pred_pretrain_text.split(3, dim=1)
        noise_pred_pretrain_uncond, _ = noise_pred_pretrain_uncond.split(3, dim=1)

        # NOTE: guidance scale definition here is aligned with diffusers, but different from other guidance
        noise_pred_pretrain = noise_pred_pretrain_uncond + self.cfg.guidance_scale * (
            noise_pred_pretrain_text - noise_pred_pretrain_uncond
        )

        # TODO: more general cases
        assert self.scheduler.config.prediction_type == "epsilon"

        (
            noise_pred_est_text,
            noise_pred_est_uncond,
        ) = noise_pred_est.chunk(2)

        noise_pred_est_text, _ = noise_pred_est_text.split(3, dim=1)
        noise_pred_est_uncond, _ = noise_pred_est_uncond.split(3, dim=1)

        # NOTE: guidance scale definition here is aligned with diffusers, but different from other guidance
        noise_pred_est = noise_pred_est_uncond + self.cfg.guidance_scale_lora * (
            noise_pred_est_text - noise_pred_est_uncond
        )

        if self.scheduler_lora.config.prediction_type == "v_prediction":
            alphas_cumprod = self.scheduler_lora.alphas_cumprod.to(
                device=latents_noisy.device, dtype=latents_noisy.dtype
            )
            alpha_t = alphas_cumprod[t] ** 0.5
            sigma_t = (1 - alphas_cumprod[t]) ** 0.5

            noise_pred_est = latents * sigma_t + noise_pred_est * alpha_t

        w = (1 - self.alphas[t]).view(-1, 1, 1, 1)

        grad = w * (noise_pred_pretrain - noise_pred_est)
        return grad

    def train_lora(
        self,
        latents: Float[Tensor, "B 3 64 64"],
        text_embeddings: Float[Tensor, "BB 77 768"],
        camera_condition: Float[Tensor, "B 4 4"],
    ):
        B = latents.shape[0]
        latents = latents.detach().repeat(self.cfg.lora_n_timestamp_samples, 1, 1, 1)

        t = torch.randint(
            int(self.num_train_timesteps * 0.0),
            int(self.num_train_timesteps * 1.0),
            [B * self.cfg.lora_n_timestamp_samples],
            dtype=torch.long,
            device=self.device,
        )

        noise = torch.randn_like(latents)
        noisy_latents = self.scheduler_lora.add_noise(latents, noise, t)
        if self.scheduler_lora.config.prediction_type == "epsilon":
            target = noise
        elif self.scheduler_lora.config.prediction_type == "v_prediction":
            target = self.scheduler_lora.get_velocity(latents, noise, t)
        else:
            raise ValueError(
                f"Unknown prediction type {self.scheduler_lora.config.prediction_type}"
            )
        # use view-independent text embeddings in LoRA
        text_embeddings, _ = text_embeddings.chunk(2)
        if self.cfg.lora_cfg_training and random.random() < 0.1:
            camera_condition = torch.zeros_like(camera_condition)
        noise_pred = self.forward_unet(
            self.unet_lora,
            noisy_latents,
            t,
            encoder_hidden_states=text_embeddings.repeat(
                self.cfg.lora_n_timestamp_samples, 1, 1
            ),
            class_labels=camera_condition.view(B, -1).repeat(
                self.cfg.lora_n_timestamp_samples, 1
            ),
            cross_attention_kwargs={"scale": 1.0},
        )
        noise_pred, _ = noise_pred.split(3, dim=1)
        return F.mse_loss(noise_pred.float(), target.float(), reduction="mean")

    def forward(
        self,
        rgb: Float[Tensor, "B H W C"],
        prompt_utils: PromptProcessorOutput,
        elevation: Float[Tensor, "B"],
        azimuth: Float[Tensor, "B"],
        camera_distances: Float[Tensor, "B"],
        mvp_mtx: Float[Tensor, "B 4 4"],
        c2w: Float[Tensor, "B 4 4"],
        rgb_as_latents=False,
        **kwargs,
    ):
        batch_size = rgb.shape[0]

        rgb_BCHW = rgb.permute(0, 3, 1, 2)
        assert rgb_as_latents == False, f"No latent space in {self.__class__.__name__}"
        rgb_BCHW = rgb_BCHW * 2.0 - 1.0  # scale to [-1, 1] to match the diffusion range
        latents = F.interpolate(
            rgb_BCHW, (64, 64), mode="bilinear", align_corners=False
        )

        # view-dependent text embeddings
        text_embeddings_vd = prompt_utils.get_text_embeddings(
            elevation,
            azimuth,
            camera_distances,
            view_dependent_prompting=self.cfg.view_dependent_prompting,
        )

        # input text embeddings, view-independent
        text_embeddings = prompt_utils.get_text_embeddings(
            elevation, azimuth, camera_distances, view_dependent_prompting=False
        )

        if self.cfg.camera_condition_type == "extrinsics":
            camera_condition = c2w
        elif self.cfg.camera_condition_type == "mvp":
            camera_condition = mvp_mtx
        else:
            raise ValueError(
                f"Unknown camera_condition_type {self.cfg.camera_condition_type}"
            )

        grad = self.compute_grad_vsd(
            latents, text_embeddings_vd, text_embeddings, camera_condition
        )

        grad = torch.nan_to_num(grad)

        # reparameterization trick
        # d(loss)/d(latents) = latents - target = latents - (latents - grad) = grad
        target = (latents - grad).detach()
        loss_vsd = 0.5 * F.mse_loss(latents, target, reduction="sum") / batch_size

        loss_lora = self.train_lora(latents, text_embeddings, camera_condition)

        return {
            "loss_vsd": loss_vsd,
            "loss_lora": loss_lora,
            "grad_norm": grad.norm(),
        }

    def update_step(self, epoch: int, global_step: int, on_load_weights: bool = False):
        if (
            self.cfg.anneal_start_step is not None
            and global_step > self.cfg.anneal_start_step
        ):
            if self.cfg.anneal_strategy == "milestone":
                self.max_step = int(
                    self.num_train_timesteps * self.cfg.max_step_percent_annealed
                )
            elif self.cfg.anneal_strategy == "sqrt":
                max_step_percent_annealed = self.cfg.max_step_percent - (
                    self.cfg.max_step_percent - self.cfg.min_step_percent
                ) * math.sqrt(
                    (global_step - self.cfg.anneal_start_step)
                    / (self.cfg.anneal_end_step - self.cfg.anneal_start_step)
                )
                self.max_step = int(
                    self.num_train_timesteps * max_step_percent_annealed
                )
            elif self.cfg.anneal_strategy == "none":
                pass
            else:
                raise ValueError(
                    f"Unknown anneal strategy {self.cfg.anneal_strategy}, should be one of 'milestone', 'sqrt', 'none'"
                )