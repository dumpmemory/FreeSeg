# Copyright (c) Facebook, Inc. and its affiliates.
import copy
import logging

import numpy as np
import torch
from torch.nn import functional as F

from detectron2.config import configurable
from detectron2.data import detection_utils as utils
from detectron2.data import transforms as T
from detectron2.structures import BitMasks, Instances

from .mask_former_semantic_dataset_mapper import MaskFormerSemanticDatasetMapper

__all__ = ["ADEDatasetMapper"]


class ADEDatasetMapper(MaskFormerSemanticDatasetMapper):
    """
    A callable which takes a dataset dict in Detectron2 Dataset format,
    and map it into a format used by MaskFormer for panoptic segmentation.

    The callable currently does the following:

    1. Read the image from "file_name"
    2. Applies geometric transforms to the image and annotation
    3. Find and applies suitable cropping to the image and annotation
    4. Prepare image and annotation to Tensors
    """

    @configurable
    def __init__(
        self,
        is_train=True,
        *,
        augmentations,
        image_format,
        ignore_label,
        size_divisibility,
    ):
        """
        NOTE: this interface is experimental.
        Args:
            is_train: for training or inference
            augmentations: a list of augmentations or deterministic transforms to apply
            image_format: an image format supported by :func:`detection_utils.read_image`.
            ignore_label: the label that is ignored to evaluation
            size_divisibility: pad image size to be divisible by this value
        """
        super().__init__(
            is_train,
            augmentations=augmentations,
            image_format=image_format,
            ignore_label=ignore_label,
            size_divisibility=size_divisibility,
        )

    def __call__(self, dataset_dict):
        """
        Args:
            dataset_dict (dict): Metadata of one image, in Detectron2 Dataset format.

        Returns:
            dict: a format that builtin models in detectron2 accept
        """
        assert self.is_train, "MaskFormerPanopticDatasetMapper should only be used for training!"

        dataset_dict = copy.deepcopy(dataset_dict)  # it will be modified by code below
        image = utils.read_image(dataset_dict["file_name"], format=self.img_format)
        utils.check_image_size(dataset_dict, image)

        
        if "sem_seg_file_name" in dataset_dict:
            # PyTorch transformation not implemented for uint16, so converting it to double first
            sem_seg_gt = utils.read_image(dataset_dict.pop("sem_seg_file_name")).astype("double")
        else:
            sem_seg_gt = None

        aug_input = T.AugInput(image, sem_seg=sem_seg_gt)
        aug_input, transforms = T.apply_transform_gens(self.tfm_gens, aug_input)
        image = aug_input.image
        if sem_seg_gt is not None:
            sem_seg_gt = aug_input.sem_seg

        image_shape = (image.shape[-2], image.shape[-1])  # h, w

        
        if "pan_seg_file_name" in dataset_dict:
            pan_seg_gt = utils.read_image(dataset_dict.pop("pan_seg_file_name"), "RGB")
            segments_info = dataset_dict["segments_info"]
        
            # apply the same transformation to panoptic segmentation
            pan_seg_gt = transforms.apply_segmentation(pan_seg_gt)

            from panopticapi.utils import rgb2id

            pan_seg_gt = rgb2id(pan_seg_gt)

            # Pad image and segmentation label here!
            image = torch.as_tensor(np.ascontiguousarray(image.transpose(2, 0, 1)))
            if sem_seg_gt is not None:
                sem_seg_gt = torch.as_tensor(sem_seg_gt.astype("long"))
            pan_seg_gt = torch.as_tensor(pan_seg_gt.astype("long"))
            

            if "annotations" in dataset_dict:
                raise ValueError("Pemantic segmentation dataset should not have 'annotations'.")

            # Prepare per-category binary masks
            pan_seg_gt = pan_seg_gt.numpy()
            instances = Instances(image_shape)
            classes = []
            masks = []
            for segment_info in segments_info:
                class_id = segment_info["category_id"]
                if not segment_info["iscrowd"]:
                    classes.append(class_id)
                    masks.append(pan_seg_gt == segment_info["id"])

            classes = np.array(classes)
            instances.gt_classes = torch.tensor(classes, dtype=torch.int64)
            if len(masks) == 0:
                # Some image does not have annotation (all ignored)
                instances.gt_masks = torch.zeros((0, pan_seg_gt.shape[-2], pan_seg_gt.shape[-1]))
            else:
                masks = BitMasks(
                    torch.stack([torch.from_numpy(np.ascontiguousarray(x.copy())) for x in masks])
                )
                instances.gt_masks = masks.tensor

            dataset_dict["pan_instances"] = instances


            # for instance segmentation

            ins_instances = Instances(image_shape)
            classes = []
            masks = []
            for segment_info in segments_info:
                class_id = segment_info["category_id"]
                if "iscrowd" not in segment_info or segment_info["iscrowd"] == False:
                    if segment_info["isthing"]:
                        classes.append(class_id)
                        masks.append(pan_seg_gt == segment_info["id"])
            
            classes = np.array(classes)
            ins_instances.gt_classes = torch.tensor(classes, dtype=torch.int64)
            if len(masks) == 0:
                # Some image does not have annotation (all ignored)
                ins_instances.gt_masks = torch.zeros((0, pan_seg_gt.shape[-2], pan_seg_gt.shape[-1]))
                ins_instances.gt_boxes = Boxes(torch.zeros((0, 4)))
            else:
                masks = BitMasks(
                    torch.stack([torch.from_numpy(np.ascontiguousarray(x.copy())) for x in masks])
                )
                ins_instances.gt_masks = masks.tensor
                ins_instances.gt_boxes = masks.get_bounding_boxes()
            
            dataset_dict["ins_instances"] = ins_instances


            # semantic segmentation
            if sem_seg_gt is not None:
                image = torch.as_tensor(np.ascontiguousarray(image.transpose(2, 0, 1)))
                sem_seg_gt = torch.as_tensor(sem_seg_gt.astype("long"))
                if self.size_divisibility > 0:
                    image_size = (image.shape[-2], image.shape[-1])
                    padding_size = [
                        0,
                        self.size_divisibility - image_size[1],
                        0,
                        self.size_divisibility - image_size[0],
                    ]
                    image = F.pad(image, padding_size, value=128).contiguous()
                    
                    sem_seg_gt = F.pad(
                        sem_seg_gt, padding_size, value=self.ignore_label
                    ).contiguous()

                image_shape = (image.shape[-2], image.shape[-1])  # h, w
                dataset_dict["image"] = image

                dataset_dict["sem_seg"] = sem_seg_gt.long()

                # Prepare per-category binary masks
                sem_seg_gt = sem_seg_gt.numpy()
                sem_seg_instances = Instances(image_shape)
                sem_classes = np.unique(sem_seg_gt)
                # remove ignored region
                sem_classes = sem_classes[sem_classes != self.ignore_label]
                sem_seg_instances.gt_classes = torch.tensor(sem_classes, dtype=torch.int64)

                sem_masks = []
                for class_id in sem_classes:
                    sem_masks.append(sem_seg_gt == class_id)

                if len(sem_masks) == 0:
                    # Some image does not have annotation (all ignored)
                    sem_seg_instances.gt_masks = torch.zeros(
                        (0, sem_seg_gt.shape[-2], sem_seg_gt.shape[-1])
                    )
                else:
                    sem_masks = BitMasks(
                        torch.stack(
                            [
                                torch.from_numpy(np.ascontiguousarray(x.copy()))
                                for x in sem_masks
                            ]
                        )
                    )
                    sem_seg_instances.gt_masks = sem_masks.tensor
                
                dataset_dict["sem_instances"] = sem_seg_instances

        return dataset_dict