import copy
import torch
import torch.nn as nn
from typing import Tuple, Optional, Dict
from dataclasses import dataclass
import numpy as np
from transformer4planning.models.decoder.base import TrajectoryDecoder
from transformers.modeling_outputs import CausalLMOutputWithCrossAttentions
from transformers.configuration_utils import PretrainedConfig
from transformers.modeling_utils import PreTrainedModel
from transformers.utils import logging
logging.set_verbosity_info()
logger = logging.get_logger("transformers")


@dataclass
class LTMOutput(CausalLMOutputWithCrossAttentions):
    loss: Optional[torch.FloatTensor] = None
    logits: torch.FloatTensor = None
    pred_dict: Optional[Dict[str, torch.FloatTensor]] = None
    past_key_values: Optional[Tuple[Tuple[torch.FloatTensor]]] = None
    hidden_states: Optional[Tuple[torch.FloatTensor]] = None
    attentions: Optional[Tuple[torch.FloatTensor]] = None
    cross_attentions: Optional[Tuple[torch.FloatTensor]] = None
    loss_items: Optional[Dict[str, torch.FloatTensor]] = None


def fetch_ith_sample(inputs, i):
    sample = {}
    for key in inputs.keys():
        sample[key] = inputs[key][i]
    return sample


def move_np_to_torch(nparray, device, dtype):
    return torch.tensor(nparray, device=device, dtype=dtype)


def list_of_sample_dic_to_batch(list_of_sample_dic):
    batch_size = len(list_of_sample_dic)
    batched_dic = {}
    for each_key in list_of_sample_dic[0]:
        if isinstance(list_of_sample_dic[0][each_key], np.ndarray):
            shape = list_of_sample_dic[0][each_key].shape
            batched_data = np.zeros((batch_size, *shape))
            for i in range(batch_size):
                batched_data[i] = list_of_sample_dic[i][each_key]
            batched_dic[each_key] = batched_data
        elif each_key in ['aug_current']:
            if each_key not in batched_dic:
                batched_dic[each_key] = []
            batched_dic[each_key] = np.array([list_of_sample_dic[i][each_key] for i in range(batch_size)])
    return batched_dic


class STRConfig(PretrainedConfig):
    def update_by_model_args(self, model_args):
        for each_key in model_args.__dict__:
            self.__dict__[each_key] = model_args.__dict__[each_key]
        # to be compatible with older models
        attr_list = ["use_key_points", "kp_decoder_type", "separate_kp_encoder", "use_proposal",
                     "autoregressive_proposals", "selected_exponential_past",
                     "rms_norm", "residual_in_fp32", "fused_add_norm", "raster_encoder_type",
                     "vit_intermediate_size", "mean_circular_loss",
                     "camera_image_encoder", "use_speed", "no_yaw_with_stepping",
                     "skip_yaw_norm"]
        for each_attr in attr_list:
            if not hasattr(self, each_attr):
                self.__dict__[each_attr] = False


class STR(PreTrainedModel):
    def __init__(self, config, **kwargs):
        super().__init__(config)
        self.config = config
        self.encoder = None
        self.traj_decoder = None
        self.camera_image_encoder = None
        self.k = int(self.config.k)

        self.use_proposal = self.config.use_proposal
        # if self.use_proposal: assert self.config.task == "waymo", "NotImplemented"

        self.use_key_points = self.config.use_key_points
        self.kp_decoder_type = self.config.kp_decoder_type

        self.model_parallel = False
        self.device_map = None
        self.clf_metrics = None
        # Initialize weights and apply final processing
        self.build_encoder()
        self.build_decoder()

        self.training_scenarios = None

        print('model inited')


    def build_encoder(self):
        if self.config.task == "nuplan":
            if "raster" in self.config.encoder_type:
                if self.config.autoregressive:
                    from transformer4planning.models.encoder.nuplan_raster_encoder import NuplanRasterizeAutoRegressiveEncoder
                    self.encoder = NuplanRasterizeAutoRegressiveEncoder(self.config)
                else:
                    from transformer4planning.models.encoder.nuplan_raster_encoder import NuplanRasterizeEncoder
                    self.encoder = NuplanRasterizeEncoder(self.config)
            elif "vector" in self.config.encoder_type:
                from transformer4planning.models.encoder.pdm_encoder import PDMEncoder
                pdm_kwargs = dict(
                    hidden_dim=self.config.n_embd,
                    centerline_dim=120,
                    history_dim=20
                )
                self.encoder = PDMEncoder(pdm_kwargs, self.config)
            else:
                raise AttributeError("encoder_type should be either raster or vector")
        elif self.config.task == "waymo":
            from transformer4planning.models.encoder.waymo_vector_encoder import WaymoVectorizeEncoder
            action_kwargs = dict(
                d_embed=self.config.n_embd
            )
            self.encoder = WaymoVectorizeEncoder(action_kwargs, self.config)
        else:
            raise NotImplementedError

    def build_decoder(self):
        # load pretrained diffusion keypoint decoder
        #TODO: add diffusion decoder trained from scratch
        if self.use_proposal:
            if self.config.task == "nuplan":
                from transformer4planning.models.decoder.base import ProposalDecoderCLS
                self.proposal_decoder = ProposalDecoderCLS(self.config, proposal_num=self.use_proposal)
            elif self.config.task == "waymo":
                from transformer4planning.models.decoder.base import ProposalDecoder
                self.proposal_decoder = ProposalDecoder(self.config)

        if self.use_key_points != 'no':
            if self.kp_decoder_type == "diffusion":
                from transformer4planning.models.decoder.diffusion_decoder import KeyPointDiffusionDecoder
                self.key_points_decoder = KeyPointDiffusionDecoder(self.config)
                if self.config.key_points_diffusion_decoder_load_from is not None:
                    logger.info(f"Now loading pretrained key_points_diffusion_decoder from {self.config.key_points_diffusion_decoder_load_from}.")
                    state_dict = torch.load(self.config.key_points_diffusion_decoder_load_from)
                    self.key_points_decoder.model.load_state_dict(state_dict)
                    logger.info("Pretrained keypoint decoder has been loaded!")
                else:
                    logger.info("Now initializing diffusion decoder from scratch. Training will consume lots of time.")
            elif self.kp_decoder_type == "mlp":
                from transformer4planning.models.decoder.base import KeyPointMLPDeocder
                self.key_points_decoder = KeyPointMLPDeocder(self.config)

        self.traj_decoder = TrajectoryDecoder(self.config)

    def _prepare_attention_mask_for_generation(self, input_embeds):
        return torch.ones(input_embeds.shape[:2], dtype=torch.long, device=input_embeds.device)

    def _prepare_position_ids_for_generation(self, attention_mask):
        position_ids = attention_mask.long().cumsum(-1) - 1
        position_ids.masked_fill_(attention_mask == 0, 1)
        return position_ids

    def forward(
            self,
            return_dict: Optional[bool] = None,
            **kwargs
    ):
        return_dict = return_dict if return_dict is not None else self.config.use_return_dict
        if not return_dict:
            raise NotImplementedError('need to return dict for evaluations in trainer.py')

        input_embeds, info_dict = self.encoder(is_training=self.training, **kwargs)
        transformer_outputs_hidden_state = self.embedding_to_hidden(input_embeds, return_dict=return_dict)

        if self.config.autoregressive:
            frames_length_to_predict = info_dict["pred_length"]
            trajectory_label = info_dict['trajectory_label'][..., -frames_length_to_predict:, :]
            loss = torch.tensor(0, dtype=input_embeds.dtype, device=transformer_outputs_hidden_state.device)
            traj_loss, traj_logits = self.traj_decoder.compute_traj_loss_autoregressive(transformer_outputs_hidden_state,
                                                                                        trajectory_label,
                                                                                        info_dict)
            loss += traj_loss
        else:
            trajectory_label = info_dict["trajectory_label"]
            loss = torch.tensor(0, dtype=input_embeds.dtype, device=transformer_outputs_hidden_state.device)
            traj_loss, traj_logits = self.traj_decoder.compute_traj_loss(transformer_outputs_hidden_state,
                                                                         trajectory_label,
                                                                         info_dict)
            loss += traj_loss

        loss_items = dict(
            traj_loss=traj_loss,
        )
        pred_dict = {"traj_logits": traj_logits}
        # if finetuning_with_stepping:
        #     logger.warning('passing gt for debugging')
        #     pred_dict = {"traj_logits": trajectory_label}

        if self.use_proposal:
            if self.config.task == "nuplan":
                proposal_loss, pred_proposal_cls = self.proposal_decoder.compute_proposal_loss(transformer_outputs_hidden_state, info_dict)
                loss += proposal_loss
                loss_items["proposal_loss"] = proposal_loss
                pred_dict["proposal"] = pred_proposal_cls
                # debugging
                pred_proposal_score = pred_proposal_cls.softmax(-1)
                topk_score, topk_indx = torch.topk(pred_proposal_score[:, 0, :], dim=-1, k=self.k)

            elif self.config.task == "waymo":
                proposal_loss, proposal_loss_logits = self.proposal_decoder.compute_proposal_loss(transformer_outputs_hidden_state, info_dict)
                loss += proposal_loss
                loss += proposal_loss_logits
                loss_items["proposal_loss"] = proposal_loss
                pred_dict["proposal"] = proposal_loss_logits

        if self.config.dense_pred:
            assert self.config.task == "waymo"
            loss += info_dict["dense_pred_loss"]
            loss_items["dense_pred_loss"] = info_dict["dense_pred_loss"]

        if self.use_key_points != 'no':
            if self.config.generate_diffusion_dataset_for_key_points_decoder:
                future_key_points = info_dict["future_key_points"] if self.config.predict_yaw else \
                            info_dict["future_key_points"][..., :2]
                self.key_points_decoder.save_features(input_embeds,info_dict["context_length"],info_dict,future_key_points,transformer_outputs_hidden_state)

            if self.config.kp_decoder_type == "diffusion":
                # assert not self.training, "please train diffusion decoder separately."
                # return a dummy loss&kp_logits here. The real data for computing metrics will be computed in the generate function
                kp_loss = torch.tensor(0.0).to(transformer_outputs_hidden_state.device)
                kp_logits = info_dict["future_key_points"].to(transformer_outputs_hidden_state.device) if self.config.predict_yaw else \
                            info_dict["future_key_points"][..., :2].to(transformer_outputs_hidden_state.device)
            else:
                kp_loss, kp_logits = self.key_points_decoder.compute_keypoint_loss(transformer_outputs_hidden_state, info_dict)
                # kp_loss will be 10x larger than traj_loss when converged

            loss += kp_loss
            traj_logits = torch.cat([kp_logits, traj_logits], dim=1)
            pred_dict["kp_logits"] = kp_logits
            loss_items["kp_loss"] = kp_loss

        # WIP: training with simulations
        if self.config.finetuning_with_simulation_on_val and self.training:
            sim_loss = self.do_closed_loop_simulation(kwargs)
            loss += sim_loss

        # if not return_dict:
        #     output = (traj_logits,) + transformer_outputs[1:]
        #     return ((loss,) + output) if loss is not None else output
        return LTMOutput(
            loss=loss,
            logits=traj_logits,  # deprecated, use pred_dict for evaluation instead
            pred_dict=pred_dict,
            hidden_states=transformer_outputs_hidden_state,
            loss_items=loss_items
        )

    def embedding_to_hidden(self, input_embeds, attention_mask=None, position_ids=None, return_dict=True):
        # default as the gpt2 model output
        transformer_outputs = self.transformer(
            inputs_embeds=input_embeds,
            attention_mask=attention_mask,
            position_ids=position_ids,
            return_dict=return_dict,
            # **kwargs
        )
        return transformer_outputs['last_hidden_state']

    def do_closed_loop_simulation(self,
                                  inputs,
                                  metric_key_prefix: str = "CLS", ):
        """
        Run closed loop simulation and returns the metrics.
        Args:
            val1k_datasest (`Dataset`, *optional*):
                Pass a dataset. If it is a [`~datasets.Dataset`], columns not accepted by the `model.forward()` method
                 are automatically removed. It must implement the `__len__` method.
            metric_key_prefix (`str`, *optional*, defaults to `"eval"`):
                An optional prefix to be used as the metrics key prefix. For example the metrics "8sFDE" will be named
                "CLS_8sFDE" if the prefix is "CLS" (default)

        Returns:
            A dictionary containing the evaluation loss and the potential metrics computed from the predictions. The
            dictionary also contains the epoch number which comes from the training state.
        """
        from run_simulation import build_simulation_in_batch
        from nuplan_simulation.planner_utils import build_metrics_aggregators
        from nuplan.planning.simulation.main_callback.metric_aggregator_callback import MetricAggregatorCallback
        from nuplan.planning.simulation.main_callback.multi_main_callback import MultiMainCallback
        import datetime, time, os

        experiment_name = 'closed_loop_nonreactive_agents'  # [open_loop_boxes, closed_loop_nonreactive_agents, closed_loop_reactive_agents]
        job_name = 'STR_planner'
        experiment_time = datetime.datetime.now()
        experiment = os.path.join(experiment_name, job_name, str(experiment_time))
        output_dir = os.path.join(f"tempForTrainSim")
        simulation_dir = "simulation"
        metric_dir = "metrics"
        aggregator_metric_dir = "aggregator_metric"

        # initialize main aggregator
        metric_aggregators = build_metrics_aggregators(experiment_name, output_dir, aggregator_metric_dir)
        metric_save_path = f"{output_dir}/{metric_dir}"
        metric_aggregator_callback = MetricAggregatorCallback(metric_save_path, metric_aggregators)
        main_callbacks = MultiMainCallback([metric_aggregator_callback])
        main_callbacks.on_run_simulation_start()
        # set a timer
        start_time = time.perf_counter()
        scenarios = self.training_scenarios
        # filter scenarios
        scenario_ids_in_batch = inputs['scenario_id']
        filtered_scenarios = [each for each in scenarios if each.token in scenario_ids_in_batch]

        # begin testing
        logger.info('Running simulations at training...')

        if not hasattr(self, 'all_road_dic'):
            self.all_road_dic = {}

        batch_size = len(scenario_ids_in_batch)
        _, overall_score_dic, overall_score = build_simulation_in_batch(experiment_name, filtered_scenarios, output_dir, simulation_dir, metric_dir,
                                                                        batch_size=batch_size,
                                                                        model=self,
                                                                        all_road_dic=self.all_road_dic,
                                                                        save_reports=False)
        main_callbacks.on_run_simulation_end()
        # overall_score_dic['overall_score'] = overall_score
        logger.info('Simulation at training Done')

        # compute loss
        sim_loss = torch.tensor(1.0 / max(0.01, overall_score), device=self.device)

        logger.info(f'\nTime all: {time.perf_counter() - start_time:.3f} s with loss {sim_loss:.3f}')
        return sim_loss


    @torch.no_grad()
    def generate(self, **kwargs) -> torch.FloatTensor:
        input_embeds, info_dict = self.encoder(is_training=False, **kwargs)
        batch_size, _, _ = input_embeds.shape
        device = input_embeds.device
        context_length = info_dict["context_length"]

        if self.use_proposal:
            if self.config.autoregressive_proposals:
                # TODO: Training for debugging results
                proposal_result = []
                proposal_scores = []
                assert self.config.task == 'nuplan', 'waymo proposal autoregressive not implemented yet'
                dummy_proposal_embedding = self.encoder.proposal_m_embed(torch.zeros((batch_size, int(self.config.proposal_num), 1), device=device))  # bsz, 16, 256
                input_embeds[:, context_length:context_length+int(self.config.proposal_num), :] = dummy_proposal_embedding
                # loop over each intention for generation
                for i in range(int(self.config.proposal_num)):
                    context_embeds = input_embeds[:, :context_length + 1 + i, :]
                    attention_mask = torch.ones(context_embeds.shape[:2], dtype=torch.long, device=device)
                    position_ids = self._prepare_position_ids_for_generation(attention_mask.clone())
                    transformer_outputs_hidden_state = self.embedding_to_hidden(context_embeds)
                    proposal_hidden_state = transformer_outputs_hidden_state[:, context_length - 1 + i:context_length - 1 + 1 + i, :]  # (bs, 1, n_embed)
                    proposal_pred_score = self.proposal_decoder.proposal_cls_decoder(proposal_hidden_state).softmax(-1)  # (bs, 1, 5)
                    # WARNING: Only tested with self.k = 1
                    topk_score, topk_indx = torch.topk(proposal_pred_score[:, 0, :], dim=-1, k=self.k)
                    # topk_score: (bs, 5) topk_indx: (bs, 1)
                    proposal_pred_embed = self.encoder.proposal_m_embed(topk_indx.float())  # (bs, n_embed)
                    # print('test generate 1: ', topk_indx.unsqueeze(-1).float().shape, topk_indx, topk_score, proposal_pred_score[:, 0, :])
                    proposal_result.append(topk_indx.unsqueeze(1))  # list of (bs, 1, 1)
                    proposal_scores.append(proposal_pred_score[:, 0, :].unsqueeze(1))  # list of (bs, 1, 13)
                    input_embeds[:, context_length+i:context_length+i+1, :] = proposal_pred_embed.unsqueeze(1)
                proposal_result = torch.cat(proposal_result, dim=1)  # (bs, 13, 1)
                proposal_scores = torch.cat(proposal_scores, dim=1)  # (bs, 13, 5)
            else:
                if self.config.task == "nuplan":
                    dummy_proposal_embedding = self.encoder.proposal_m_embed(torch.zeros((batch_size, 1), device=device)).unsqueeze(1)
                elif self.config.task == 'waymo':
                    dummy_proposal_embedding = self.encoder.proposal_m_embed(torch.zeros((batch_size, 2), device=device)).unsqueeze(1)
                input_embeds[:, context_length:context_length+1, :] = dummy_proposal_embedding

                context_embeds = input_embeds[:, :context_length+1, :]
                attention_mask = torch.ones(context_embeds.shape[:2], dtype=torch.long, device=device)
                transformer_outputs_hidden_state = self.embedding_to_hidden(
                    input_embeds=context_embeds,
                    attention_mask=attention_mask,
                    position_ids=self._prepare_position_ids_for_generation(attention_mask.clone())
                )
                proposal_hidden_state = transformer_outputs_hidden_state[:, context_length-1:context_length-1+1, :] # (bs, 1, n_embed)
                proposal_pred_score = self.proposal_decoder.proposal_cls_decoder(proposal_hidden_state).softmax(-1) # (bs, 1, 64/5)
                if self.config.task == "nuplan":
                    # WARNING: Only tested with self.k = 1
                    topk_score, topk_indx = torch.topk(proposal_pred_score[:, 0, :], dim=-1, k=self.k)
                    # topk_score: (bs, 5), topk_indx: (bs, 1)
                    proposal_pred_embed = self.encoder.proposal_m_embed(topk_indx.float())  # (bs, n_embed)
                    # print('test generate 1: ', topk_indx.unsqueeze(-1).float().shape, topk_indx, topk_score, proposal_pred_score[:, 0, :])
                    proposal_result = topk_indx
                    proposal_scores = proposal_pred_score[:, 0, :]
                    # proposal_pred_embed: (bs, k, n_embed)
                elif self.config.task == 'waymo':
                    proposal_logit = info_dict["center_obj_proposal_pts"] # (bs, 64, 2)
                    topk_score, topk_indx = torch.topk(proposal_pred_score[:, 0, :], dim=-1, k=self.k)
                    proposal_pred_logit = proposal_logit[torch.arange(batch_size)[:, None].repeat(1, self.k).view(-1), topk_indx.view(-1), :].view(batch_size, self.k, 2)
                    proposal_pred_embed = self.encoder.proposal_m_embed(proposal_pred_logit)
                    proposal_result = topk_indx
                    proposal_scores = proposal_pred_score[:, 0, :]

        traj_logits_k = []
        key_points_logits_k = []
        for mode in range(self.k):
            # print('test generate 2: ', proposal_pred_embed.shape)
            if self.use_proposal:
                if self.config.autoregressive_proposals:
                    # already updated in previous step
                    pass
                else:
                    if self.config.task == "nuplan":
                        input_embeds[:, context_length:context_length + 1, :] = proposal_pred_embed.unsqueeze(1)
                    elif self.config.task == 'waymo':
                        input_embeds[:, context_length:context_length + 1, :] = proposal_pred_embed.unsqueeze(2)[:, mode, :, :]
            if self.use_key_points != "no":
                pred_length = info_dict["pred_length"]
                selected_indices = self.encoder.selected_indices
                kp_start_index = context_length
                if self.use_proposal:
                    if self.config.autoregressive_proposals:
                        kp_start_index += int(self.config.proposal_num)
                    else:
                        kp_start_index += 1
                # pass the following infos during generate for one sample (non-batch) generate with KP checking
                map_name = kwargs.get("map", None)
                route_ids = kwargs.get("route_ids", None)
                ego_pose = kwargs.get("ego_pose", None)
                road_dic = kwargs.get("road_dic", None)
                idm_reference_global = kwargs.get("idm_reference_global", None)  # WIP, this was not fulled tested
                trajectory_label_dummy = torch.zeros((batch_size, pred_length, 4), device=device)
                if 'specified' in self.use_key_points:
                    future_key_points = trajectory_label_dummy[:, selected_indices, :]
                else:
                    ar_future_interval = 20
                    future_key_points = trajectory_label_dummy[:, ar_future_interval - 1::ar_future_interval, :]

                assert future_key_points.shape[1] > 0, 'future points not enough to sample'

                if self.config.task == "nuplan" and not self.config.separate_kp_encoder:
                    if self.config.use_speed:
                        # padding speed, padding the last dimension from 4 to 7
                        future_key_points = torch.cat([future_key_points, torch.zeros_like(future_key_points)[:, :, :3]], dim=-1)
                    future_key_embeds_dummy = self.encoder.action_m_embed(future_key_points)
                else:
                    future_key_embeds_dummy = self.encoder.kps_m_embed(future_key_points)

                key_points_num = future_key_points.shape[1]

                input_embeds[:, kp_start_index:kp_start_index + key_points_num, :] = future_key_embeds_dummy
                pred_key_points_during_generate = []
                for i in range(key_points_num):
                    input_embeds_current = input_embeds[:, :kp_start_index + i, :]
                    attention_mask = torch.ones(input_embeds_current.shape[:2], dtype=torch.long, device=input_embeds.device)
                    position_ids = self._prepare_position_ids_for_generation(attention_mask.clone())
                    transformer_outputs_hidden_state = self.embedding_to_hidden(
                        input_embeds_current,
                        attention_mask,
                        position_ids,
                    )
                    future_key_point_hidden_state = transformer_outputs_hidden_state[:,
                                                    kp_start_index + i - 1,
                                                    :].reshape(batch_size, 1, -1)

                    key_points_logit, _ = self.key_points_decoder.generate_keypoints(future_key_point_hidden_state)
                    pred_key_point = torch.zeros((batch_size, 1, 4), device=device)
                    if self.config.predict_yaw:
                        pred_key_point[:, 0, :] = key_points_logit[:, 0, :]
                    else:
                        pred_key_point[:, 0, :2] = key_points_logit[:, 0, :]

                    off_road_checking = True
                    if off_road_checking and batch_size == 1 and route_ids is not None and road_dic is not None and ego_pose is not None and map_name is not None:
                        from transformer4planning.utils import nuplan_utils
                        if i in [0, 1] and 'backward' in self.use_key_points:
                            # Check key points with map_api
                            # WARNING: WIP, do not use
                            y_inverse = -1 if map_name == 'sg-one-north' else 1
                            pred_key_point_copy = copy.deepcopy(pred_key_point)
                            pred_key_point_copy[0, 0, 1] *= y_inverse
                            pred_key_point_global = nuplan_utils.change_coordination(pred_key_point_copy[0, 0, :2].cpu().numpy(),
                                                                        ego_pose,
                                                                        ego_to_global=True)
                            if isinstance(route_ids, torch.Tensor):
                                route_ids = route_ids.cpu().numpy().tolist()
                            closest_lane_point_on_route, dist, _, _, on_road = nuplan_utils.get_closest_lane_point_on_route(pred_key_point_global,
                                                                                                                            route_ids,
                                                                                                                            road_dic)
                            if not on_road:
                                pred_key_point_ego = nuplan_utils.change_coordination(closest_lane_point_on_route,
                                                                                      ego_pose,
                                                                                      ego_to_global=False)
                                pred_key_point_ego[1] *= y_inverse
                                pred_key_point[0, 0, :2] = torch.tensor(pred_key_point_ego, device=pred_key_point.device)
                                print(f'Off Road Detected! Replace {i}th key point')

                    if idm_reference_global is not None and 'backward' in self.use_key_points:
                        # replace last key point with IDM reference
                        ego_state_global = idm_reference_global[selected_indices[i]]
                        idm_reference_lastpt_relative = nuplan_utils.change_coordination(np.array([ego_state_global.rear_axle.x,
                                                                                                ego_state_global.rear_axle.y]),
                                                                                        ego_pose,
                                                                                        ego_to_global=False)
                        print('replace key points with IDM reference, index: ', selected_indices[i], pred_key_point[0, 0, :2], idm_reference_lastpt_relative)  # idm relative has an unusual large negative y value?
                        pred_key_point[0, 0, :2] = torch.tensor(idm_reference_lastpt_relative, device=pred_key_point.device)
                        pred_key_point[0, 0, -1] = nuplan_utils.normalize_angle(ego_state_global.rear_axle.heading - ego_pose[-1])

                    if self.config.task == "nuplan" and not self.config.separate_kp_encoder:
                        if self.config.use_speed:
                            # padding speed, padding the last dimension from 4 to 7
                            pred_key_point = torch.cat([pred_key_point, torch.zeros_like(pred_key_point)[:, :, :3]], dim=-1)
                        key_point_embed = self.encoder.action_m_embed(pred_key_point).reshape(batch_size, 1, -1)  # b, 1, n_embed
                    else:
                        key_point_embed = self.encoder.kps_m_embed(pred_key_point).reshape(batch_size, 1, -1)  # b, 1, n_embed
                    # replace embed at the next position
                    input_embeds[:, kp_start_index + i, :] = key_point_embed[:, 0, :]
                    if self.config.predict_yaw:
                        pred_key_points_during_generate.append(pred_key_point[:, 0, :].unsqueeze(1))
                    else:
                        pred_key_points_during_generate.append(pred_key_point[:, 0, :2].unsqueeze(1))
                key_points_logits = torch.cat(pred_key_points_during_generate, dim=1).reshape(batch_size, key_points_num, -1)
                key_points_logits_k.append(key_points_logits)

            # generate remaining trajectory
            transformer_outputs_hidden_state = self.embedding_to_hidden(input_embeds)
            if self.config.autoregressive:
                points_to_predict = info_dict["pred_length"]
                raster_seq_length = info_dict["sequence_length"]
                traj_logits = torch.zeros((batch_size, points_to_predict, 4), device=device)
                predicting_index = context_length - 1  # output space index
                for i in range(points_to_predict):
                    # 1. generate trajectory
                    current_context_length = context_length + i * (1 + raster_seq_length)  # input space index
                    context_embeds = input_embeds[:, :current_context_length, :]
                    attention_mask = torch.ones(context_embeds.shape[:2], dtype=torch.long, device=device)
                    position_ids = self._prepare_position_ids_for_generation(attention_mask.clone())
                    transformer_outputs_hidden_state = self.embedding_to_hidden(
                        context_embeds,
                        attention_mask,
                        position_ids,
                    )
                    traj_logits[:, i, :] = self.traj_decoder.model(transformer_outputs_hidden_state)[:, predicting_index, :]
                    predicting_index += (1 + raster_seq_length)
                    # 2. update input_embeds with generated trajectory
                    new_point_embed = self.encoder.action_m_embed(traj_logits[:, i, :].unsqueeze(1))
                    input_embeds[:, current_context_length, :] = new_point_embed[:, 0, :]
                    # 3. generate observation
                    if i == points_to_predict - 1:
                        # skip generating observations for the last point
                        continue
                    # TODO: change to generate observation
                    next_state_embeds = torch.zeros((batch_size, raster_seq_length, 1, self.config.n_embd), device=device)
                    for j in range(raster_seq_length):
                        input_embeds[:, current_context_length + 1 + j, :] = next_state_embeds[:, j, 0, :]
                traj_logits_k.append(traj_logits)
                # # expand traj_logits from (b, 8, 4/7) to (b, 80, 4/7) with linear interpolation
                # expanded_traj_logits = torch.zeros((batch_size, points_to_predict * 10, 4), device=device)
                # # add padding 0 to the first point to the traj_logits for linear interpolation
                # traj_logits = torch.cat([torch.zeros((batch_size, 1, 4), device=device), traj_logits], dim=1)
                # for i in range(points_to_predict):
                #     for j in range(10):
                #         expanded_traj_logits[:, i * 10 + j, :] = traj_logits[:, i, :] + (traj_logits[:, i + 1, :] - traj_logits[:, i, :]) * j / 10
                # traj_logits_k.append(expanded_traj_logits)
            # expected shape for pred trajectory is (b, pred_length, 4/7)
            elif self.traj_decoder is not None:
                traj_logits = self.traj_decoder.generate_trajs(transformer_outputs_hidden_state, info_dict)
                traj_logits_k.append(traj_logits)
            else:
                raise NotImplementedError

        key_points_pred_logits = None
        if self.k == 1:
            traj_pred_logits = traj_logits_k[0]
            if len(key_points_logits_k) > 0:
                # WARNING, k select if not implemented for key points
                assert len(key_points_logits_k) == self.k
                key_points_pred_logits = key_points_logits_k[0]
        else:
            traj_pred_logits = torch.stack(traj_logits_k, dim=1)
            if len(key_points_logits_k) > 0:
                assert len(key_points_logits_k) == self.k
                key_points_pred_logits = torch.stack(key_points_logits_k, dim=1)

        pred_dict = {
            "traj_logits": traj_pred_logits
        }

        # if kwargs.get('old_file_name', None) is not None:
        #     logger.warning('passing gt at gen next step for debugging')
        #     pred_dict['traj_logits'] = kwargs['trajectory_label']

        if key_points_pred_logits is not None:
            pred_dict.update({"key_points_logits": key_points_pred_logits})

        if self.use_proposal:
            pred_dict.update({"proposal": proposal_result})  # topk results
            pred_dict.update({"proposal_scores": proposal_scores})  # topk scores
            if self.config.task == 'nuplan' and 'intentions' in info_dict:
                pred_dict.update({'intentions': info_dict['intentions']})

        if self.config.task == "waymo":
            center_objects_world = kwargs['center_objects_world'].type_as(traj_pred_logits)
            num_center_objects, num_modes, num_timestamps, num_feat = traj_pred_logits.shape

            from transformer4planning.utils.waymo_utils import rotate_points_along_z, str_to_tensor

            pred_trajs_world = rotate_points_along_z(
                points=traj_pred_logits.view(num_center_objects, num_modes * num_timestamps, num_feat),
                angle=center_objects_world[:, 6].view(num_center_objects)
            ).view(num_center_objects, num_modes, num_timestamps, num_feat)
            pred_trajs_world[:, :, :, 0:2] += center_objects_world[:, None, None, 0:2]

            pred_dict = {
                'scenario_id': str_to_tensor(kwargs['scenario_id']).to(device),
                'pred_trajs': pred_trajs_world[:, :, :, 0:2],
                'pred_scores': topk_score,
                'object_id': torch.tensor(kwargs['center_objects_id']).to(device),
                'object_type': torch.tensor(kwargs['center_objects_type']).to(device),
                'gt_trajs': kwargs['center_gt_trajs_src'],
                'track_index_to_predict': kwargs['track_index_to_predict'],
            }

        return pred_dict


def build_models(model_args):
    # TODO: refactor model building function into each model class
    if 'gpt' in model_args.model_name:
        from transformer4planning.models.backbone.gpt2 import STR_GPT2, STRGPT2Config
        config_p = STRGPT2Config()
        config_p.update_by_model_args(model_args)
        # from transformer4planning.models.backbone.gpt2 import TrajectoryGPT
        ModelCls = STR_GPT2
        tag = 'GPTTrajectory'
        if 'gpt-mini' in model_args.model_name:
            """
            Number of parameters: 300k
            """
            config_p.n_layer = 1
            config_p.n_embd = config_p.d_model = 64
            config_p.n_inner = config_p.n_embd * 4
            config_p.n_head = 1
        elif 'gpt-small' in model_args.model_name:
            """
            Number of parameters: 16M
            """
            config_p.n_layer = 4
            config_p.n_embd = config_p.d_model = 256
            config_p.n_inner = config_p.n_embd * 4
            config_p.n_head = 8
        elif 'gpt-medium' in model_args.model_name:
            """
            Number of parameters: 124M
            """
            config_p.n_layer = 12
            config_p.n_embd = config_p.d_model = 768
            config_p.n_inner = config_p.n_embd * 4
            config_p.n_head = 12
        elif 'gpt-large' in model_args.model_name:
            """
            Number of parameters: 1.5B
            """
            config_p.n_layer = 48
            config_p.n_embd = config_p.d_model = 1600
            config_p.n_inner = config_p.n_embd * 4
            config_p.n_head = 25
        else:
            logger.warning('Using default GPT2 config')
            config_p.n_layer = model_args.n_layers
            config_p.n_embd = model_args.d_embed
            config_p.n_inner = model_args.d_inner
            config_p.n_head = model_args.n_heads
        config_p.activation_function = model_args.activation_function

        if model_args.task == "train_diffusion_decoder":
            from transformer4planning.models.decoder.diffusion_decoder import (KeypointDiffusionModel, T4PTrainDiffWrapper)
            out_features = 4 if model_args.predict_yaw else 2
            diffusion_model = KeypointDiffusionModel(config_p.n_inner,
                                                     config_p.n_embd,
                                                     out_features=out_features,
                                                     key_point_num=1,
                                                     input_feature_seq_lenth=model_args.diffusion_condition_sequence_lenth,
                                                     use_key_points=model_args.use_key_points,
                                                     feat_dim=model_args.key_points_diffusion_decoder_feat_dim,)
            model = T4PTrainDiffWrapper(diffusion_model, num_key_points=model_args.key_points_num, model_args=config_p)
            if model_args.key_points_diffusion_decoder_load_from is not None:
                state_dict = torch.load(model_args.key_points_diffusion_decoder_load_from)
                model.load_state_dict(state_dict)
                logger.info("Pretrained keypoint decoder has been loaded!")
            logger.info("Only diffusion decoder will be trained!")
            return model
        # whole model training
    elif 'mamba' in model_args.model_name:
        from transformer4planning.models.backbone.mamba import STRMamba, STRMambaConfig
        config_p = STRMambaConfig()
        config_p.update_by_model_args(model_args)
        ModelCls = STRMamba
        tag = 'MambaTrajectory'
        if 'mamba-mini' in model_args.model_name:
            """
            Number of parameters: ?
            """
            config_p.n_layer = 1
            config_p.n_embd = config_p.d_model = 64
            config_p.n_inner = config_p.n_embd * 4
            config_p.n_head = 1
        elif 'mamba-small' in model_args.model_name:
            """
            Number of parameters: 6M (ViT512)
            """
            config_p.n_layer = 4
            config_p.n_embd = config_p.d_model = 256
            config_p.n_inner = config_p.n_embd * 4
            config_p.n_head = 8
        elif 'mamba-medium' in model_args.model_name:
            """
            Number of parameters: 27M (ViT)
            """
            config_p.n_layer = 8
            config_p.n_embd = config_p.d_model = 512
            config_p.n_inner = config_p.n_embd * 4
            config_p.n_head = 16
        elif 'mamba-large' in model_args.model_name:
            """
            WARNING: Gradient WILL CRUSH DURING TRAINING
            Number of parameters: 139M (ViT)
            """
            config_p.n_layer = 16
            config_p.n_embd = config_p.d_model = 1000
            config_p.n_inner = config_p.n_embd * 4
            config_p.n_head = 25
        elif 'mamba-xl' in model_args.model_name:
            """
            Number of parameters: 764M (ViT)
            """
            config_p.n_layer = 24
            config_p.n_embd = config_p.d_model = 2048
            config_p.n_inner = config_p.n_embd * 4
            config_p.n_head = 32
    elif 'mixtral' in model_args.model_name:
        from transformer4planning.models.backbone.mixtral import STR_Mixtral, STRMixtralConfig
        config_p = STRMixtralConfig()
        config_p.update_by_model_args(model_args)
        ModelCls = STR_Mixtral
        tag = 'MixTralTrajectory'
        if 'mixtral-small' in model_args.model_name:
            config_p.n_layer = 4
            config_p.n_embd = config_p.d_model = 256
            config_p.n_inner = config_p.n_embd * 4
            config_p.n_head = 8
            config_p.num_hidden_layers = 4
            config_p.hidden_size = 256
            config_p.intermediate_size = config_p.n_embd * 4
            config_p.num_attention_heads = 8
        elif 'mixtral-medium' in model_args.model_name:
            """
            Number of parameters: 1.5B (ViT)
            """
            config_p.n_layer = 16
            config_p.n_embd = config_p.d_model = 1024
            config_p.n_inner = 4096
            config_p.n_head = 16
            config_p.num_hidden_layers = 16
            config_p.hidden_size = 1024
            config_p.intermediate_size = 4096
            config_p.num_attention_heads = 16
        elif 'mixtral-3b-deep' in model_args.model_name:
            """
            Number of parameters: 350M x 8 -> 3.3B (ViT)
            """
            # 3.3B trainable with batch size of 8
            config_p.n_layer = 32
            config_p.n_embd = config_p.d_model = 1024
            config_p.n_inner = 4096
            config_p.n_head = 32
            config_p.hidden_size = config_p.n_embd
            config_p.intermediate_size = config_p.n_inner
            config_p.num_attention_heads = config_p.n_head
        elif 'mixtral-3b-wide' in model_args.model_name:
            """
            Number of parameters: 350M x 8 -> 3.8B (ViT)
            Previous Large
            """
            # 3.8B trainable with batch size of 8
            config_p.n_layer = 16
            config_p.n_embd = config_p.d_model = 1536
            config_p.n_inner = 6144
            config_p.n_head = 16
            config_p.num_hidden_layers = config_p.n_layer
            config_p.hidden_size = config_p.n_embd
            config_p.intermediate_size = config_p.n_inner
            config_p.num_attention_heads = config_p.n_head
        elif 'mixtral-6b-deep' in model_args.model_name:
            """
            Number of parameters: 6B (ViT)
            """
            # 6B trainable
            # config_p.n_layer = 25
            # config_p.n_embd = config_p.d_model = 1536
            # config_p.n_inner = 6144
            # config_p.n_head = 16

            # 6.5B trainable
            # config_p.n_layer = 20
            # config_p.n_embd = config_p.d_model = 1792
            # config_p.n_inner = 7168
            # config_p.n_head = 16

            # 6.7B trainable with batch size of 1
            config_p.n_layer = 64
            config_p.n_embd = config_p.d_model = 1024
            config_p.n_inner = 4096
            config_p.n_head = 64
            config_p.num_hidden_layers = config_p.n_layer
            config_p.hidden_size = config_p.n_embd
            config_p.intermediate_size = config_p.n_inner
            config_p.num_attention_heads = config_p.n_head
        else:
            assert False, f'Unsupported model name: {model_args.model_name}!'
    elif 'stablelm' in model_args.model_name:
        from transformer4planning.models.backbone.stablelm import STR_StableLM, STRStableLMConfig
        config_p = STRStableLMConfig()
        config_p.update_by_model_args(model_args)
        ModelCls = STR_StableLM
        tag = 'StableLMTrajectory'
        if 'stablelm-small' in model_args.model_name:
            """
            Number of parameters: 22M (ViT)
            """
            config_p.n_layer = 4
            config_p.n_embd = config_p.d_model = 256
            config_p.n_inner = config_p.n_embd * 4
            config_p.n_head = 8
            config_p.num_hidden_layers = 4
            config_p.hidden_size = 256
            config_p.intermediate_size = config_p.n_embd * 4
            config_p.num_key_value_heads = config_p.num_attention_heads = 8
            attn_implementation = "flash_attention_2"
        elif 'stablelm-medium' in model_args.model_name:
            """
            Number of parameters: 300M (ViT)
            """
            config_p.n_layer = 16
            config_p.n_embd = config_p.d_model = 1024
            config_p.n_inner = 4096
            config_p.n_head = 16
            config_p.num_hidden_layers = 16
            config_p.hidden_size = 1024
            config_p.intermediate_size = 4096
            config_p.num_key_value_heads = config_p.num_attention_heads = 16
        elif 'stablelm-large' in model_args.model_name:
            """
            WARNING: Gradient WILL CRUSH DURING TRAINING
            Number of parameters: 350M x 8 -> 2,2B (ViT)
            """
            config_p.n_layer = 24
            config_p.n_embd = config_p.d_model = 1024
            config_p.n_inner = 3584
            config_p.n_head = 16
            config_p.num_hidden_layers = 24
            config_p.hidden_size = 1024
            config_p.intermediate_size = 3584
            config_p.num_key_value_heads = config_p.num_attention_heads = 16
        elif 'stablelm-xl' in model_args.model_name:
            """
            WARNING: Gradient WILL CRUSH DURING TRAINING
            Number of parameters: 350M x 8 -> 2,2B (ViT)
            """
            config_p.n_layer = 24
            config_p.n_embd = config_p.d_model = 2048
            config_p.n_inner = 7168
            config_p.n_head = 16
            config_p.num_hidden_layers = 24
            config_p.hidden_size = 2048
            config_p.intermediate_size = 7168
            config_p.num_key_value_heads = config_p.num_attention_heads = 16
    else:
        raise ValueError("Model name must choose from ['scratch', 'pretrain'] + ['nonauto-gpt', 'transxl', 'gpt', 'xlnet']!")

    if 'scratch' in model_args.model_name:
        model = ModelCls(config_p)
        logger.info('Scratch ' + tag + ' Initialized!')
    elif 'pretrain' in model_args.model_name:
        # from transformers import AqlmConfig
        # quantization_config = AqlmConfig(weights="int8", pre_quantized=False)
        model = ModelCls.from_pretrained(model_args.model_pretrain_name_or_path, config=config_p)
        logger.info('Pretrained ' + tag + 'from {}'.format(model_args.model_pretrain_name_or_path))
        if model_args.key_points_diffusion_decoder_load_from is not None:
                print(f"Now loading pretrained key_points_diffusion_decoder from {model_args.key_points_diffusion_decoder_load_from}.")
                state_dict = torch.load(model_args.key_points_diffusion_decoder_load_from)
                model.key_points_decoder.model.load_state_dict(state_dict)
    elif 'transfer' in model_args.model_name:
        model = ModelCls(config_p)
        logger.info('Transfer' + tag + ' from {}'.format(model_args.model_pretrain_name_or_path))
    return model


def interpolate_yaw(pred_traj, mode, yaw_change_upper_threshold=0.1):
    # deprecated
    if mode == "normal":
        return pred_traj
    elif mode == "interplate" or mode == "hybrid":
        # Warning: this function is tested not better than normal mode
        assert False, "Warning: this function is tested not better than normal mode, to be updated in the future"
        # generating yaw angle from relative_traj
        dx = pred_traj[:, 4::5, 0] - pred_traj[:, :-4:5, 0]
        dy = pred_traj[:, 4::5, 1] - pred_traj[:, :-4:5, 1]
        distances = torch.sqrt(dx ** 2 + dy ** 2)
        relative_yaw_angles = torch.where(distances > 0.1, torch.arctan2(dy, dx), 0)
        # accumulate yaw angle
        # relative_yaw_angles = yaw_angles.cumsum()
        relative_yaw_angles_full = relative_yaw_angles.repeat_interleave(5, dim=1)
        if mode == "interplate":
            pred_traj[:, :, -1] = relative_yaw_angles_full
        else:
            pred_traj[:, :, -1] = torch.where(torch.abs(pred_traj[:, :, -1]) > yaw_change_upper_threshold, relative_yaw_angles_full, pred_traj[:, :, -1])
    return pred_traj
