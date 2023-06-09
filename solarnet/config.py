from enum import Enum
from pathlib import Path
from typing import Optional

import torch
from pydantic import BaseSettings, Field, validator
from torch.nn import BCEWithLogitsLoss, CrossEntropyLoss
from torch.optim import SGD, Adam, AdamW
from torch.optim.lr_scheduler import (CosineAnnealingLR, ExponentialLR,
                                      ReduceLROnPlateau)

from solarnet.cli import CallableEnum, Initializer
from solarnet.losses import CombinedLoss, FocalTverskyLoss
from solarnet.metrics import F1Score, IoU, Precision, Recall


class Models(Enum):
    deeplabv3 = "deeplabv3",
    adaptnet = "adaptnet",
    unet = "unet"


class Metrics(CallableEnum):
    f1 = Initializer(F1Score, ignore_index=255)
    iou = Initializer(IoU, ignore_index=255)
    precision = Initializer(Precision, ignore_index=255)
    recall = Initializer(Recall, ignore_index=255)


class Optimizers(CallableEnum):
    adam = Initializer(Adam)
    adamw = Initializer(AdamW)
    sgd = Initializer(SGD, momentum=0.9)


class Schedulers(CallableEnum):
    plateau = Initializer(ReduceLROnPlateau)
    exp = Initializer(ExponentialLR, gamma=0.87)
    cosine = Initializer(CosineAnnealingLR, T_max=10)


class Losses(CallableEnum):
    bce = Initializer(BCEWithLogitsLoss, pos_weight=torch.tensor([0.8]))
    crossent = Initializer(CrossEntropyLoss, ignore_index=255)
    tversky = Initializer(FocalTverskyLoss, alpha=0.7, beta=0.3)
    combo = Initializer(CombinedLoss,
                        criterion_a=Initializer(CrossEntropyLoss),
                        criterion_b=Initializer(FocalTverskyLoss))


class TrainerSettings(BaseSettings):
    device: str = Field("cuda:0", description="Device to run the experiment on")
    batch_size: int = Field(64, description="Size of the batch for a single GPU")
    num_workers: int = Field(8, description="Number of workers for each dataloader")
    lr: float = Field(1e-3, description="Learning rate for the optimizer")
    weight_decay: float = Field(1e-2, description="Weight decay (usually L2 penalty) for optimizers")

    max_epochs: int = Field(100, description="Aximum amount of epochs to run the experiments")
    val_size: float = Field(0.1, description="Percentage of dataset to be used as validation")
    test_size: float = Field(0.2, description="Percentage of dataset to be used as test")
    patience: int = Field(10, description="Number of epochs before early stopping")


class CommonSettings(BaseSettings):
    seed: int = Field(42, description="Seed for deterministic runs")
    deterministic: bool = Field(False, description="Whether to force deterministic algorithms (slower) or not")
    name: Optional[str] = Field(None, description="Identifier if the experiment, autogenerated if missing")
    data_folder: str = Field("data", description="Path to the folder to store data")
    output_folder: str = Field("outputs", description="Path to the folder to store outputs")
    optimizer: Optimizers = Field(Optimizers.adam, description="Optimizer choice (hardcoded params for now)")
    scheduler: Schedulers = Field(Schedulers.exp, description="Scheduler choice (hardcoded params for now)")
    loss: Losses = Field(Losses.bce, description="Loss function for training")
    monitor: Metrics = Field(Metrics.f1, description="Which metric to monitor")
    trainer: TrainerSettings = TrainerSettings()

    @validator("loss")
    def post_load(cls, v, values, **kwargs):
        if isinstance(v, str):
            return Losses[v]
        return v


class ClassifierSettings(CommonSettings):
    backbone: str = Field("resnet50", description="Backbone required to train the classifier")
    pretrained: Optional[Path] = Field(None, description="Path to a pretrained model")


class SegmenterTrainSettings(CommonSettings):
    model: Models = Field(Models.unet, description="Which segmentation model to use")
    encoder: str = Field("resnet50", description="Backbone for the segmentation model")
    enc_weights: Optional[Path] = Field(None, description="Optional path to a pretrained encoder")
    enc_pretrained: bool = Field(False, description="Whether to use a pretrained encoder or not")
    enc_lr: Optional[float] = Field(None, description="Optional custom LR for the encoder."\
        "When missing ,the default LR is applied to the whole network")

    input_channels: int = Field(4, description="Number of input channels (3 for RGB, 4 to include IR)")
    multiclass: bool = Field(True, description="Train with multi-class (mono, poly) or binary (panel yes/no)")
    class_weights: Optional[Path] = Field(None, description="Optional class weights for the loss.")
    image_size: int = Field(256, description="Pixel size of a single input image (one side)")

    comment: Optional[str] = Field(None, description="Optional comment to describe the experiment")

    @validator("multiclass")
    def post_load(cls, v, values, **kwargs):
        if v is True and "loss" in values:
            if values["loss"] == Losses.bce:
                print("WARNING: BCE loss not supported for multi-class training, using standard cross-entropy")
                values["loss"] = Losses.crossent
        return v


class SSLSegmenterTrainSettings(SegmenterTrainSettings):
    """Settings for SSL training
    """


class SegmenterTestSettings(SegmenterTrainSettings):
    results_folder: str = Field("results", description="Subfolder name to store data results in (plots, etc.)")
    large_images_file: Optional[Path] = Field(None, description="Optional path to a file containing the large images")
    store_predictions: bool = Field(True, description="Whether to generate the corresponding predicted images or not")
    model_name: Optional[str] = Field(None, description="Model name. If None, then the best is taken")
    metric_reduction: str = Field("micro", description="Which reduction to apply, if possible")
    tile_size: Optional[int] = Field(256, description="Optional tile size to use for large inference")
