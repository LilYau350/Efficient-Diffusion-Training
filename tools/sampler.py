import torch
from tqdm import tqdm
import torch.distributed as dist
from diffusers.models import AutoencoderKL
from torch.cuda.amp import autocast
from tools import dist_util
from .cfg_edm import ablation_sampler, float_equal, Net
from models.unet import EncoderUNetModel


class Classifier:
    def __init__(self, args, device, model):
        self.args = args
        self.device = device
        self.model = model
        self.classifier = self._load_classifier() if args.use_classifier else None

    def _create_classifier(self):
        attention_ds = [self.args.image_size // res for res in self.model.attention_resolutions]
        classifier_kwargs = {
            'image_size': self.model.image_size,
            'in_channels': self.args.in_chans,
            'model_channels': self.model.model_channels,
            'out_channels': self.model.num_classes,
            'num_res_blocks': self.model.num_res_blocks,
            'attention_resolutions': tuple(attention_ds),
            'channel_mult': self.model.channel_mult,
            'num_head_channels': self.model.num_head_channels,
            'use_scale_shift_norm': self.model.use_scale_shift_norm,
            'resblock_updown': self.model.resblock_updown,
            'pool': "attention",
        }
        return EncoderUNetModel(**classifier_kwargs)

    def _load_classifier(self):
        classifier = self._create_classifier()
        classifier.load_state_dict(torch.load(self.args.use_classifier, map_location="cpu"))
        classifier.to(self.device)
        classifier.eval()
        return classifier

    def cond_fn(self, x, t, y, scale=1.0):
        assert y is not None
        with torch.enable_grad():
            x_in = x.detach().requires_grad_(True)
            logits = self.classifier(x_in, t)
            log_probs = torch.nn.functional.log_softmax(logits, dim=-1)
            selected = log_probs[range(len(log_probs)), y.view(-1)]
            return torch.autograd.grad(selected.sum(), x_in)[0] * scale


def sync_ema_model(eval_model):
    """Synchronize EMA model parameters across distributed devices."""
    for param in eval_model.parameters():
        dist.broadcast(param.data, src=0)

class Sampler:
    def __init__(self, args, device, eval_model, diffusion, classifier=None):
        self.args = args     
        self.device = device
        self.model = eval_model
        self.diffusion = diffusion      
        self.classifier = classifier

    def _model_fn(self, x, t, y=None):
        return self.model(x, t, y if self.args.class_cond else None)
    
    def ddim_sampler(self, num_samples, sample_size, image_size, num_classes, progress_bar=False):
        self.model.eval()
        all_samples, all_labels = [], []
        world_size = dist.get_world_size() if self.args.parallel else 1

        if self.args.parallel:
            sync_ema_model(self.model)
            dist.barrier()

        if progress_bar and dist_util.is_main_process():
            pbar = tqdm(total=num_samples, desc="Generating Samples (DDIM)")
            
        vae = AutoencoderKL.from_pretrained(f"stabilityai/sd-vae-ft-{self.args.vae}", local_files_only=True).to(self.device) if self.args.in_chans == 4 else None
        
        while len(all_samples) * sample_size < num_samples:
            classes = self._get_y_cond(sample_size, num_classes)
            sample = self.diffusion.ddim_sample_loop(
                self.model if not self.classifier else self._model_fn,
                (sample_size, 3, image_size, image_size),
                device=self.device,
                model_kwargs={"y": classes} if self.args.class_cond else {},
                cond_fn=(lambda x, t, y: self.classifier.cond_fn(x, t, y, self.args.guidance_scale)) if self.classifier else None,
            )
            sample = self._process_sample(sample, vae)

            self._gather_samples(all_samples, all_labels, sample, classes, world_size)

            if dist_util.is_main_process() and progress_bar:
                pbar.update(sample_size * world_size)

        return all_samples, all_labels

    def edm_sampler(self, num_samples, sample_size, image_size, num_classes, progress_bar=False):
        self.model.eval()
        all_samples, all_labels = [], []
        world_size = dist.get_world_size() if self.args.parallel else 1

        if self.args.parallel:
            sync_ema_model(self.model)
            dist.barrier()

        if progress_bar and dist_util.is_main_process():
            pbar = tqdm(total=num_samples, desc=f"Generating Samples ({self.args.solver.capitalize()})")

        vae = AutoencoderKL.from_pretrained(f"stabilityai/sd-vae-ft-{self.args.vae}", local_files_only=True).to(self.device) if self.args.in_chans == 4 else None
        net = Net(model=self.model, img_channels=self.args.in_chans, img_resolution=image_size, label_dim=num_classes,
                  noise_schedule=self.args.beta_schedule, amp=self.args.amp, power=self.args.p,
                  pred_type=self.args.mean_type).to(self.device)

        while len(all_samples) * sample_size < num_samples:
            y_cond = self._get_y_cond(sample_size, num_classes)
            z = torch.randn([sample_size, net.img_channels, net.img_resolution, net.img_resolution], device=self.device)
            class_labels, z = self._prepare_labels(y_cond, num_classes, sample_size, z)

            guidance_scale = self._limited_interval_guidance(self.args.t_from, self.args.t_to, self.args.guidance_scale)

            sample = ablation_sampler(net, latents=z, num_steps=self.args.sample_timesteps, solver=self.args.solver,
                                      discretization=self.args.discretization, schedule=self.args.schedule, scaling=self.args.scaling,
                                      class_labels=class_labels, guidance_scale=guidance_scale,)
            
            sample = self._process_sample(sample, vae)
            self._gather_samples(all_samples, all_labels, sample, class_labels, world_size)

            if dist_util.is_main_process() and progress_bar:
                pbar.update(sample_size * world_size)

        return all_samples, all_labels

    def flow_matching_sampler(self, num_samples, sample_size, image_size, num_classes, progress_bar=False):
        self.model.eval()
        all_samples, all_labels = [], []
        world_size = dist.get_world_size() if self.args.parallel else 1

        if self.args.parallel:
            sync_ema_model(self.model)
            dist.barrier()

        if progress_bar and dist_util.is_main_process():
            pbar = tqdm(total=num_samples, desc=f"Generating Samples ({self.args.solver.capitalize()})")

        vae = AutoencoderKL.from_pretrained(f"stabilityai/sd-vae-ft-{self.args.vae}", local_files_only=True).to(self.device) if self.args.in_chans == 4 else None
        

        while len(all_samples) * sample_size < num_samples:
            y_cond = self._get_y_cond(sample_size, num_classes)
            z = torch.randn([sample_size, self.args.in_chans, image_size, image_size], device=self.device)
            class_labels, z = self._prepare_labels(y_cond, num_classes, sample_size, z)

            guidance_scale = self._limited_interval_guidance(self.args.t_from, self.args.t_to, self.args.guidance_scale)

            sample = self.diffusion.sample(self.model, z, self.device, num_steps=self.args.sample_timesteps, solver=self.args.solver,
                                           class_labels=class_labels, guidance_scale=guidance_scale,)
            
            sample = self._process_sample(sample, vae)
            self._gather_samples(all_samples, all_labels, sample, class_labels, world_size)

            if dist_util.is_main_process() and progress_bar:
                pbar.update(sample_size * world_size)

        return all_samples, all_labels
    
    def _get_y_cond(self, sample_size, num_classes):
        y_cond = None  
        if self.args.class_cond:
            if self.args.class_labels is not None:  
                assert len(self.args.class_labels) == sample_size, (f"Length of class_labels ({len(self.args.class_labels)}) must match sample_size ({sample_size})")
                assert all(isinstance(x, int) and 0 <= x < num_classes for x in self.args.class_labels), (f"Class labels must be integers in [0, {num_classes})")
                y_cond = torch.tensor(self.args.class_labels, device=self.device)
            else:
                y_cond = torch.randint(0, num_classes, (sample_size,), device=self.device)
        return y_cond
        
    def _gather_samples(self, all_samples, all_labels, sample, labels, world_size):
        """Gather samples across devices if running in parallel."""
        if self.args.parallel:
            gathered_samples = [torch.zeros_like(sample) for _ in range(world_size)]
            dist.all_gather(gathered_samples, sample)
            all_samples.extend([sample.cpu().numpy() for sample in gathered_samples])
            if self.args.class_cond:
                gathered_labels = [torch.zeros_like(labels) for _ in range(world_size)]
                dist.all_gather(gathered_labels, labels)
                all_labels.extend([label.cpu().numpy() for label in gathered_labels])
        else:
            all_samples.append(sample.cpu().numpy())
            if self.args.class_cond:
                all_labels.append(labels.cpu().numpy())

    def _prepare_labels(self, y_cond, num_classes, sample_size, z):
        """Prepare conditional and unconditional labels based on guidance scale."""
        if not float_equal(self.args.guidance_scale, 1.0):
            z = torch.cat((z, z), dim=0)
            y_uncond = torch.randint(num_classes, num_classes + 1, (sample_size,), device=self.device)
            return torch.cat((y_cond, y_uncond), dim=0), z 
        return y_cond, z

    def _limited_interval_guidance(self, t_from, t_to, guidance_scale):
        if t_from >= 0 and t_to > t_from:
            return lambda t: guidance_scale if t_from <= t <= t_to else 1.0
        return guidance_scale

    def _process_sample(self, sample, vae):
        if not float_equal(self.args.guidance_scale, 1.0) and self.args.solver != 'ddim':
            sample, _ = sample.chunk(2, dim=0) # Remove null class samples       
        """Process and decode sample if using VAE."""
        if vae:
            # Encoded with scale factor 0.18215. Decode by dividing by it for accurate reconstruction and to avoid FID errors.
            sample = vae.decode(sample.float() / self.args.latent_scale).sample
        return self._inverse_normalize(sample)
    
    def _inverse_normalize(self, sample):
        """Inverse the normalization to bring the sample back to the original image range."""
        return ((sample + 1) * 127.5).clamp(0, 255).to(torch.uint8).permute(0, 2, 3, 1).contiguous()
    
    
    def sample(self, num_samples, sample_size, image_size, num_classes, progress_bar=False):
        if self.args.model_mode == 'flow':
            return self.flow_matching_sampler(num_samples, sample_size, image_size, num_classes, progress_bar)
        
        elif self.args.model_mode == 'diffusion':
            if self.args.solver == 'ddim':
                return self.ddim_sampler(num_samples, sample_size, image_size, num_classes, progress_bar)
            # elif self.args.solver == "heun":
            else:
                return self.edm_sampler(num_samples, sample_size, image_size, num_classes, progress_bar)
            
        else:
            raise ValueError(f"Unsupported model_mode: {self.args.model_mode}")
