# Copyright (c) Facebook, Inc. and its affiliates.
from typing import Tuple

import torch
from torch import nn
from torch.nn import functional as F

from detectron2.config import configurable
from detectron2.data import MetadataCatalog
from detectron2.modeling import META_ARCH_REGISTRY, build_backbone, build_sem_seg_head
from detectron2.modeling.backbone import Backbone
from detectron2.structures import Boxes, ImageList, Instances, BitMasks
from detectron2.utils.memory import retry_if_cuda_oom

from .modeling.criterion import SetCriterion
from .modeling.matcher import HungarianMatcher


@META_ARCH_REGISTRY.register()
class MaskFormer(nn.Module):
    """
    Main class for mask classification semantic segmentation architectures.
    """

    @configurable
    def __init__(
        self,
        *,
        backbone: Backbone,
        sem_seg_head: nn.Module,
        criterion: nn.Module,
        num_queries: int,
        object_mask_threshold: float,
        overlap_threshold: float,
        metadata,
        size_divisibility: int,
        sem_seg_postprocess_before_inference: bool,
        pixel_mean: Tuple[float],
        pixel_std: Tuple[float],
        # inference
        semantic_on: bool,
        panoptic_on: bool,
        instance_on: bool,
        test_topk_per_image: int,
    ):
        """
        Args:
            backbone: a backbone module, must follow detectron2's backbone interface
            sem_seg_head: a module that predicts semantic segmentation from backbone features
            criterion: a module that defines the loss
            num_queries: int, number of queries
            object_mask_threshold: float, threshold to filter query based on classification score
                for panoptic segmentation inference
            overlap_threshold: overlap threshold used in general inference for panoptic segmentation
            metadata: dataset meta, get `thing` and `stuff` category names for panoptic
                segmentation inference
            size_divisibility: Some backbones require the input height and width to be divisible by a
                specific integer. We can use this to override such requirement.
            sem_seg_postprocess_before_inference: whether to resize the prediction back
                to original input size before semantic segmentation inference or after.
                For high-resolution dataset like Mapillary, resizing predictions before
                inference will cause OOM error.
            pixel_mean, pixel_std: list or tuple with #channels element, representing
                the per-channel mean and std to be used to normalize the input image
            semantic_on: bool, whether to output semantic segmentation prediction
            instance_on: bool, whether to output instance segmentation prediction
            panoptic_on: bool, whether to output panoptic segmentation prediction
            test_topk_per_image: int, instance segmentation parameter, keep topk instances per image
        """
        super().__init__()
        self.backbone = backbone
        self.sem_seg_head = sem_seg_head
        self.criterion = criterion
        self.num_queries = num_queries
        self.overlap_threshold = overlap_threshold
        self.object_mask_threshold = object_mask_threshold
        self.metadata = metadata
        if size_divisibility < 0:
            # use backbone size_divisibility if not set
            size_divisibility = self.backbone.size_divisibility
        self.size_divisibility = size_divisibility
        self.sem_seg_postprocess_before_inference = sem_seg_postprocess_before_inference
        self.register_buffer("pixel_mean", torch.Tensor(pixel_mean).view(-1, 1, 1), False)
        self.register_buffer("pixel_std", torch.Tensor(pixel_std).view(-1, 1, 1), False)

        # additional args
        self.semantic_on = semantic_on
        self.instance_on = instance_on
        self.panoptic_on = panoptic_on
        self.test_topk_per_image = test_topk_per_image

        if not self.semantic_on:
            assert self.sem_seg_postprocess_before_inference

    @classmethod
    def from_config(cls, cfg):
        backbone = build_backbone(cfg)
        sem_seg_head = build_sem_seg_head(cfg, backbone.output_shape())

        # Loss parameters:
        deep_supervision = cfg.MODEL.MASK_FORMER.DEEP_SUPERVISION
        no_object_weight = cfg.MODEL.MASK_FORMER.NO_OBJECT_WEIGHT

        # loss weights
        class_weight = cfg.MODEL.MASK_FORMER.CLASS_WEIGHT
        dice_weight = cfg.MODEL.MASK_FORMER.DICE_WEIGHT
        mask_weight = cfg.MODEL.MASK_FORMER.MASK_WEIGHT

        # building criterion
        matcher = HungarianMatcher(
            cost_class=class_weight,
            cost_mask=mask_weight,
            cost_dice=dice_weight,
            num_points=cfg.MODEL.MASK_FORMER.TRAIN_NUM_POINTS,
        )

        weight_dict = {"loss_ce": class_weight, "loss_mask": mask_weight, "loss_dice": dice_weight}

        if deep_supervision:
            dec_layers = cfg.MODEL.MASK_FORMER.DEC_LAYERS
            aux_weight_dict = {}
            for i in range(dec_layers - 1):
                aux_weight_dict.update({k + f"_{i}": v for k, v in weight_dict.items()})
            weight_dict.update(aux_weight_dict)

        losses = ["labels", "masks"]

        criterion = SetCriterion(
            sem_seg_head.num_classes,
            matcher=matcher,
            weight_dict=weight_dict,
            eos_coef=no_object_weight,
            losses=losses,
            num_points=cfg.MODEL.MASK_FORMER.TRAIN_NUM_POINTS,
            oversample_ratio=cfg.MODEL.MASK_FORMER.OVERSAMPLE_RATIO,
            importance_sample_ratio=cfg.MODEL.MASK_FORMER.IMPORTANCE_SAMPLE_RATIO,
        )

        return {
            "backbone": backbone,
            "sem_seg_head": sem_seg_head,
            "criterion": criterion,
            "num_queries": cfg.MODEL.MASK_FORMER.NUM_OBJECT_QUERIES,
            "object_mask_threshold": cfg.MODEL.MASK_FORMER.TEST.OBJECT_MASK_THRESHOLD,
            "overlap_threshold": cfg.MODEL.MASK_FORMER.TEST.OVERLAP_THRESHOLD,
            "metadata": MetadataCatalog.get(cfg.DATASETS.TRAIN[0]),
            "size_divisibility": cfg.MODEL.MASK_FORMER.SIZE_DIVISIBILITY,
            "sem_seg_postprocess_before_inference": (
                cfg.MODEL.MASK_FORMER.TEST.SEM_SEG_POSTPROCESSING_BEFORE_INFERENCE
                or cfg.MODEL.MASK_FORMER.TEST.PANOPTIC_ON
                or cfg.MODEL.MASK_FORMER.TEST.INSTANCE_ON
            ),
            "pixel_mean": cfg.MODEL.PIXEL_MEAN,
            "pixel_std": cfg.MODEL.PIXEL_STD,
            # inference
            "semantic_on": cfg.MODEL.MASK_FORMER.TEST.SEMANTIC_ON,
            "instance_on": cfg.MODEL.MASK_FORMER.TEST.INSTANCE_ON,
            "panoptic_on": cfg.MODEL.MASK_FORMER.TEST.PANOPTIC_ON,
            "test_topk_per_image": cfg.TEST.DETECTIONS_PER_IMAGE,
        }

    @property
    def device(self):
        return self.pixel_mean.device

    def forward(self, batched_inputs, device, input_shape):
        """
        Args:
            batched_inputs: a list, batched outputs of :class:`DatasetMapper`.
                Each item in the list contains the inputs for one image.
                For now, each item in the list is a dict that contains:
                   * "image": Tensor, image in (C, H, W) format.
                   * "instances": per-region ground truth
                   * Other information that's included in the original dicts, such as:
                     "height", "width" (int): the output resolution of the model (may be different
                     from input resolution), used in inference.
        Returns:
            list[dict]:
                each dict has the results for one image. The dict contains the following keys:

                * "sem_seg":
                    A Tensor that represents the
                    per-pixel segmentation prediced by the head.
                    The prediction has shape KxHxW that represents the logits of
                    each class for each pixel.
                * "panoptic_seg":
                    A tuple that represent panoptic output
                    panoptic_seg (Tensor): of shape (height, width) where the values are ids for each segment.
                    segments_info (list[dict]): Describe each segment in `panoptic_seg`.
                        Each dict contains keys "id", "category_id", "isthing".
        """
        features = self.backbone(batched_inputs)
        outputs = self.sem_seg_head(features)

        mask_cls_results = outputs["pred_logits"]
        mask_pred_results = outputs["pred_masks"]
        decoder_outputs = outputs["decoder_output"]
        neg_decoder_outputs = outputs["neg_decoder_output"]
        mask_embed = outputs["mask_embed"]
        # upsample masks
        mask_pred_results = F.interpolate(
            mask_pred_results,
            size=(batched_inputs.shape[-2], batched_inputs.shape[-1]),
            mode="bilinear",
            align_corners=False,
        )

        del outputs

        processed_results_dict = {
                            "decoder_outputs":decoder_outputs,
                            "neg_decoder_outputs":neg_decoder_outputs,
                             "sem_seg":None,
                             "panoptic_seg":None,
                             "instances":None,
                             "instances_scores":None,
                             "mask_embed": mask_embed
                             }
                                    #  "mask_embed": mask_embed
        
        if self.sem_seg_postprocess_before_inference:
            mask_pred_results = retry_if_cuda_oom(self.sem_seg_postprocess)(
                mask_pred_results, batched_inputs.shape[-2:], input_shape[0], input_shape[1]
            )
            mask_cls_results = mask_cls_results.to(mask_pred_results)

        if self.semantic_on:
            r = retry_if_cuda_oom(self.semantic_inference)(mask_cls_results, mask_pred_results)
            sem_seg = r.to(torch.device(device))
            processed_results_dict["sem_seg"] = sem_seg

        # panoptic segmentation inference
        if self.panoptic_on:
            # required to batch inference
            panoptic_r = retry_if_cuda_oom(self.panoptic_inference)(mask_cls_results, mask_pred_results)
            panoptic_seg = panoptic_r.get('pred_masks').to(torch.device(device))
            processed_results_dict["panoptic_seg"] = panoptic_seg
        
        # instance segmentation inference
        if self.instance_on:
            instance_r = retry_if_cuda_oom(self.instance_inference)(mask_cls_results, mask_pred_results, decoder_outputs, mask_embed)  # , mask_embed
            instances = instance_r.get('pred_masks').to(torch.device(device))
            instance_scores = instance_r.get("scores").to(torch.device(device))
            decoder_outputs = instance_r.get("decoder_outputs").to(torch.device(device))
            mask_embed = instance_r.get("mask_embed").to(torch.device(device))
            processed_results_dict["instances"] = instances
            processed_results_dict["instances_scores"] = instance_scores
            processed_results_dict["decoder_outputs"] = decoder_outputs
            processed_results_dict["mask_embed"] = mask_embed
            
        return processed_results_dict


    def sem_seg_postprocess(self, result, img_size, output_height, output_width):

        result = result[:, :,  : img_size[0], : img_size[1]]
        result = F.interpolate(
            result, size=(output_height, output_width), mode="bilinear", align_corners=False
        )
        return result

    def prepare_targets(self, targets, images):
        h_pad, w_pad = images.tensor.shape[-2:]
        new_targets = []
        for targets_per_image in targets:
            # pad gt
            gt_masks = targets_per_image.gt_masks
            padded_masks = torch.zeros((gt_masks.shape[0], h_pad, w_pad), dtype=gt_masks.dtype, device=gt_masks.device)
            padded_masks[:, : gt_masks.shape[1], : gt_masks.shape[2]] = gt_masks
            new_targets.append(
                {
                    "labels": targets_per_image.gt_classes,
                    "masks": padded_masks,
                }
            )
        return new_targets

    def semantic_inference(self, mask_cls, mask_pred):
        mask_cls = F.softmax(mask_cls, dim=-1)[..., :-1]
        mask_pred = mask_pred.sigmoid()
        semseg = torch.einsum("bqc,bqhw->bchw", mask_cls, mask_pred)
        return semseg

    def panoptic_inference(self, mask_cls, mask_pred):
        # required to batch inference
        bs = mask_cls.shape[0]
        scores, labels = F.softmax(mask_cls, dim=-1).max(-1)
        mask_pred = mask_pred.sigmoid()

        keep = labels.ne(self.sem_seg_head.num_classes) & (scores > self.object_mask_threshold)
        cur_scores = scores[keep]
        cur_classes = labels[keep]
        cur_masks = mask_pred[keep]
        cur_mask_cls = mask_cls[keep]
        # cur_mask_cls = cur_mask_cls[:, :-1]
        cur_mask_cls = cur_mask_cls[:, :, :-1]

        cur_prob_masks = cur_scores.view(bs, -1, 1, 1) * cur_masks

        h, w = cur_masks.shape[-2:]
        panoptic_seg = torch.zeros((bs, h, w), dtype=torch.int32, device=cur_masks.device)
        segments_info = []

        current_segment_id = 0

        if cur_masks.shape[0] == 0:
            # We didn't detect any mask :(
            return panoptic_seg, segments_info
        else:
            # take argmax
            cur_mask_ids = cur_prob_masks.argmax(0)  # check
            stuff_memory_list = {}
            for k in range(cur_classes.shape[0]):
                pred_class = cur_classes[k].item()
                isthing = pred_class in self.metadata.thing_dataset_id_to_contiguous_id.values()
                mask_area = (cur_mask_ids == k).sum().item()
                original_area = (cur_masks[k] >= 0.5).sum().item()
                mask = (cur_mask_ids == k) & (cur_masks[k] >= 0.5)

                if mask_area > 0 and original_area > 0 and mask.sum().item() > 0:
                    if mask_area / original_area < self.overlap_threshold:
                        continue

                    # merge stuff regions
                    if not isthing:
                        if int(pred_class) in stuff_memory_list.keys():
                            panoptic_seg[mask] = stuff_memory_list[int(pred_class)]
                            continue
                        else:
                            stuff_memory_list[int(pred_class)] = current_segment_id + 1

                    current_segment_id += 1
                    panoptic_seg[mask] = current_segment_id

                    segments_info.append(
                        {
                            "id": current_segment_id,
                            "isthing": bool(isthing),
                            "category_id": int(pred_class),
                        }
                    )

            return panoptic_seg, segments_info

    def instance_inference(self, mask_cls, mask_pred, decoder_outputs, mask_embed):  # , mask_embed
        # mask_pred is already processed to have the same shape as original input
        bs = mask_cls.shape[0]
        image_size = mask_pred.shape[-2:]

        # [Q, K]
        scores = F.softmax(mask_cls, dim=-1)[:, :, :-1]
        labels = torch.arange(self.sem_seg_head.num_classes, device=self.device).unsqueeze(0).repeat(self.num_queries, 1).flatten(0, 1).unsqueeze(0).repeat(bs, 1)
        # scores_per_image, topk_indices = scores.flatten(0, 1).topk(self.num_queries, sorted=False)
        scores_per_image, topk_indices = scores.flatten(start_dim=1).topk(self.test_topk_per_image, sorted=False)
        # labels_per_image = labels.masked_select(
        #     torch.zeros(bs, labels.shape[-1]).to(mask_cls.device).scatter(1, topk_indices, 1).bool()
        # ).contiguous().view(bs,self.test_topk_per_image)
        labels_per_image = labels.gather(1,topk_indices).to(mask_cls.device)
        topk_indices = topk_indices // self.sem_seg_head.num_classes
        # mask_pred = mask_pred.unsqueeze(1).repeat(1, self.sem_seg_head.num_classes, 1).flatten(0, 1)
        # mask_pred = mask_pred.masked_select(
        #     torch.zeros(bs, mask_pred.shape[1]).to(mask_pred.device).scatter(1, topk_indices, 1).
        #             bool().unsqueeze(2).unsqueeze(3).expand(mask_pred.shape)
        # ).contiguous().view(bs, self.test_topk_per_image, mask_pred.shape[-2], mask_pred.shape[-1])
        mask_pred = mask_pred.gather(1,topk_indices.unsqueeze(2).unsqueeze(3).expand(bs,topk_indices.shape[-1],mask_pred.shape[-2],mask_pred.shape[-1])).to(mask_cls.device)
        decoder_outputs = decoder_outputs.gather(1,topk_indices.unsqueeze(2).expand(bs,topk_indices.shape[-1],decoder_outputs.shape[-1])).to(mask_cls.device)
        mask_embed = mask_embed.gather(1,topk_indices.unsqueeze(2).expand(bs,topk_indices.shape[-1],mask_embed.shape[-1])).to(mask_cls.device)
        # if this is panoptic segmentation, we only keep the "thing" classes
        if self.panoptic_on:
            keep = torch.zeros_like(scores_per_image).bool()
            for i, lab in enumerate(labels_per_image):
                keep[i] = lab in self.metadata.thing_dataset_id_to_contiguous_id.values()

            scores_per_image = scores_per_image[keep]
            labels_per_image = labels_per_image[keep]
            mask_pred = mask_pred[keep]

        result = Instances(image_size)
        # mask (before sigmoid)
        result.pred_masks = (mask_pred > 0).float()
        # result.pred_boxes = Boxes(torch.zeros(bs, mask_pred.size(0), 4))
        # Uncomment the following to get boxes from masks (this is slow)
        # result.pred_boxes = BitMasks(mask_pred > 0).get_bounding_boxes()

        # calculate average mask prob
        mask_scores_per_image = (mask_pred.sigmoid().flatten(2) * result.pred_masks.flatten(2)).sum(2) / (result.pred_masks.flatten(2).sum(2) + 1e-6)
        result.scores = scores_per_image * mask_scores_per_image
        result.pred_classes = labels_per_image
        result.decoder_outputs = decoder_outputs
        result.mask_embed = mask_embed 
        return result