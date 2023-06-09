import io
import os
import random
from contextlib import redirect_stdout
from glob import glob
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import seaborn as sns
import torch
from matplotlib import pyplot as plt
from pydantic import BaseSettings
from solarnet.utils import common as utils
from solarnet.utils.transforms import Denormalize
from torch import nn
from torchsummary import summary

EPS = np.finfo(np.float32).eps


def identity(*args: Any) -> Any:
    return args


def seed_everything(seed: int, strict: bool = False) -> None:
    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)
    torch.use_deterministic_algorithms(strict)


def seed_worker(worker_id: int):
    worker_seed = torch.initial_seed() % 2**32 + worker_id
    np.random.seed(worker_seed)
    random.seed(worker_seed)


def string_summary(model: torch.nn.Module, input_size: Tuple[int, int, int], batch_size: int = -1, device: str = "cpu"):
    output: str = None
    with io.StringIO() as buf, redirect_stdout(buf):
        summary(model, input_size=input_size, batch_size=batch_size, device=device)
        output = buf.getvalue()
    return output


def initialize_weights(module: nn.Module) -> None:
    if isinstance(module, nn.Conv2d):
        torch.nn.init.kaiming_normal_(module.weight)
    elif isinstance(module, (nn.SyncBatchNorm, nn.BatchNorm2d)):
        module.weight.data.fill_(1)
        module.bias.data.zero_()


def compute_class_weights(data: Dict[Any, int], smoothing: float = 0.15, clip: float = 10.0):
    assert smoothing >= 0 and smoothing <= 1, "Smoothing factor out of range"
    if smoothing > 0:
        # the larger the smooth factor, the bigger the quantities to sum to the remaining counts (additive smoothing)
        smoothed_maxval = max(list(data.values())) * smoothing
        for k in data.keys():
            data[k] += smoothed_maxval
    # retrieve the (new) max value, divide by counts, round to 2 digits and clip to the given value
    # max / value allows to keep the majority class' weights to 1, while the others will be >= 1 and <= clip
    majority = max(data.values())
    return {k: np.clip(round(float(majority / v), ndigits=2), 0, clip) for k, v in data.items()}


def one_hot(target: torch.Tensor, num_classes: Optional[int] = None) -> torch.Tensor:
    """source: https://github.com/PhoenixDL/rising. Computes one-hot encoding of input tensor.
    Args:
        target (torch.Tensor): tensor to be converted
        num_classes (Optional[int], optional): number of classes. If None, the maximum value of target is used.
    Returns:
        torch.Tensor: one-hot encoded tensor of the target
    """
    if num_classes is None:
        num_classes = int(target.max().detach().item() + 1)
    dtype, device = target.dtype, target.device
    target_onehot = torch.zeros(*target.shape, num_classes, dtype=dtype, device=device)
    return target_onehot.scatter_(1, target.unsqueeze_(1), 1.0)


def one_hot_batch(batch: torch.Tensor, num_classes: int, ignore_index: int = 255):
    mask = batch == ignore_index
    target = batch.clone()
    target[mask] = num_classes
    onehot_target = torch.eye(num_classes + 1)[target]
    return onehot_target[:, :, :, :-1].permute(0, 3, 1, 2)


def _copy_channel(layer: nn.Module, channel: int = 0) -> torch.Tensor:
    input_weights = layer.weight
    extra_weights = input_weights[:, channel].unsqueeze(dim=1)  # make it  [64, 1, 7, 7]
    return torch.cat((input_weights, extra_weights), dim=1)  # obtain  [64, 4, 7, 7]


def expand_input(model: nn.Module, input_layer: str = None, copy_channel: int = 0) -> nn.Module:
    # when we know the layer name
    if input_layer is not None:
        model[input_layer].weight = nn.Parameter(_copy_channel(model[input_layer], channel=copy_channel))
    else:
        children = list(model.children())
        input_layer = children[0]
        while children and len(children) > 0:
            input_layer = children[0]
            children = list(children[0].children())

        assert not list(input_layer.children()), f"layer '{input_layer}' still has children!"
        input_layer.weight = nn.Parameter(_copy_channel(input_layer, channel=copy_channel))

    return model


def init_experiment(config: BaseSettings, log_name: str = "output.log"):
    # initialize experiment
    experiment_id = config.name or utils.current_timestamp()
    # prepare folders and log outputs
    output_folder = utils.prepare_folder(config.output_folder, experiment_id=experiment_id)
    utils.prepare_file_logging(output_folder, filename=log_name)
    seed_everything(config.seed, strict=config.deterministic)

    # prepare experiment directories
    model_folder = utils.prepare_folder(output_folder / "models")
    logs_folder = utils.prepare_folder(output_folder / "logs")
    return experiment_id, output_folder, model_folder, logs_folder


def find_best_checkpoint(folder: Path, model_name: str = "*.pth", divider: str = "_") -> Path:
    wildcard_path = folder / model_name
    models = list(glob(str(wildcard_path)))
    assert len(models) > 0, f"No models found for pattern '{wildcard_path}'"
    current_best = None
    current_best_metric = None

    for model_path in models:
        model_name = os.path.basename(model_path).replace(".pth", "")
        components = model_name.split(divider)
        if len(components) > 2:
            mtype, _, metric_str = components
        else:
            mtype, metric_str = components
        assert mtype == "classifier" or mtype == "segmenter", f"Unknown model type '{mtype}'"
        model_metric = float(metric_str.split("-")[-1])
        if not current_best_metric or current_best_metric < model_metric:
            current_best_metric = model_metric
            current_best = model_path

    return current_best


def plot_confusion_matrix(cm: np.ndarray,
                          destination: Path,
                          labels: List[str],
                          title: str = "confusion matrix",
                          normalize: bool = True) -> None:
    # annot=True to annotate cells, ftm='g' to disable scientific notation
    fig = plt.figure(figsize=(6, 6))
    if normalize:
        cm /= cm.max()
    sns.heatmap(cm, annot=True, fmt='g')
    plt.xlabel("Predicted labels")
    plt.ylabel("True labels")
    plt.title(title)
    # set labels and ticks
    tick_marks = np.arange(len(labels))
    plt.xticks(tick_marks, labels, rotation=45)
    plt.yticks(tick_marks, labels)
    # save figure
    fig.savefig(str(destination))


def mask_to_rgb(mask: np.ndarray, palette: Dict[int, tuple], channels_first: bool = False) -> np.ndarray:
    """Given an input batch, or single picture with dimensions [B, H, W] or [H, W], the utility generates
    an equivalent [B, H, W, 3] or [H, W, 3] array corresponding to an RGB version.
    The conversion uses the given palette, which should be provided as simple dictionary of indices and tuples, lists
    or arrays indicating a single RGB color. (e.g. {0: (255, 255, 255)})
    Args:
        mask (np.ndarray): input mask of indices. Each index should be present in the palette
        palette (Dict[int, tuple]): dictionary of pairs <index - color>, where colors can be provided in RGB tuple format
    Returns:
        np.ndarray: tensor containing the RGB version of the input index tensor
    """
    lut = np.zeros((256, 3), dtype=np.uint8)
    for index, color in palette.items():
        lut[index] = np.array(color, dtype=np.uint8)
    result = lut[mask]
    if channels_first:
        result = result.transpose(2, 0, 1)
    return result


def make_grid(input_batch: torch.Tensor, rgb_true: np.ndarray, rgb_pred: np.ndarray) -> np.ndarray:
    assert input_batch.ndim == 4, "Tensor not in format: [batch, channels, h, w]"
    assert input_batch.shape[0] == 1, f"Only one image at a time! (received shape: {input_batch.shape})"

    image = Denormalize()(input_batch[0]).cpu().numpy()[:3].transpose(1, 2, 0)
    image = (image * 255).astype(np.uint8)
    assert image.shape == rgb_true.shape == rgb_pred.shape, \
        f"Shapes not matching: {image.shape}, {rgb_true.shape}, {rgb_pred.shape}"

    return np.concatenate((image, rgb_true, rgb_pred), axis=1).astype(np.uint8)
