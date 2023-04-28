try:
    # imports for runner
    from models.TransformerXL.model import *
    from models.GPT2.models import *
    from models.nsm import NSMDecoder
    from models.encoders import *
    from models.decoders import *
except:
    # imports for unit test
    from TransformerXL.model import *
    from GPT2.models import *
    from nsm import NSMDecoder
    from encoders import *
    from decoders import *

import torch.nn as nn
from transformers import GPT2Model,GPT2PreTrainedModel
_CHECKPOINT_FOR_DOC = "transfo-xl-wt103"
_CONFIG_FOR_DOC = "TransfoXLConfig"

class TransfoXLModelNuPlan(TransfoXLPreTrainedModel):
    _keys_to_ignore_on_load_missing = [r"h\.\d+\.attn\.masked_bias", r"lm_head.weight"]

    def __init__(self, config, **kwargs):
        super().__init__(config)
        self.transformer = TransfoXLModel(config)
        # added
        # self.goal_cnn_encoder = CNNEncoder(config, in_channels=1)
        # self.map_cnn_encoder = CNNEncoder(config, in_channels=20)
        # self.agents_cnn_encoder = CNNEncoder(config, in_channels=72)
        #
        # self.goal_cnn_downsample = CNNDownSampling(config, in_channels=config.d_head)
        # self.map_cnn_downsample = CNNDownSampling(config, in_channels=config.d_head)
        # self.agents_cnn_downsample = CNNDownSampling(config, in_channels=config.d_head)
        model_args = kwargs['model_args']
        self.use_nsm = model_args.use_nsm
        self.predict_trajectory = model_args.predict_trajectory
        self.predict_intended_maneuver = model_args.predict_intended_maneuver
        self.predict_current_maneuver = model_args.predict_current_maneuver
        self.per_instance = model_args.per_instance_encoding
        self.time_to_predict = model_args.time_to_predict
        self.frequency_for_prediction = model_args.frequency_for_prediction
        self.not_same_scale = model_args.scale_on_not_same_loss
        self.maneuver_repeat = model_args.maneuver_repeat
        self.predict_single_step_trajectory = model_args.predict_single_step_trajectory
        self.predict_trajectory_with_nsm = model_args.predict_trajectory_with_nsm
        self.mask_history_intended_maneuver = model_args.mask_history_intended_maneuver
        self.mask_history_current_maneuver = model_args.mask_history_current_maneuver

        assert self.predict_trajectory or self.predict_intended_maneuver or self.predict_current_maneuver or self.predict_single_step_trajectory, 'Predict at least one target! Pass True in Model Args'
        
        if self.per_instance:
            in_channels = 1
            n_embed = config.d_embed
        else:
            in_channels = 29 # raster: goal + road_type + agent_type
            if self.use_nsm:
                n_embed = config.d_embed // 4
            else:
                n_embed = config.d_embed // 2

        self.cnn_downsample = CNNDownSamplingResNet18(n_embed, in_channels=in_channels)
        
        self.intended_m_embed = nn.Sequential(nn.Embedding(num_embeddings=30, embedding_dim=n_embed), nn.Tanh())
        self.current_m_embed = nn.Sequential(nn.Linear(12, n_embed, bias=False), nn.Tanh())
        self.action_m_embed = nn.Sequential(nn.Linear(4, config.d_embed), nn.Tanh())

        if self.predict_trajectory_with_nsm:
            self.nsm_decoder = NSMDecoder(config.d_embed)

        self.traj_decoder = None
        if self.predict_trajectory or self.predict_single_step_trajectory:
            self.traj_decoder = DecoderResCat(config.d_inner, config.d_embed, out_features=4)
        if self.predict_intended_maneuver:
            self.intended_m_decoder = DecoderResCat(config.d_inner, config.d_embed, out_features=12)
        if self.predict_current_maneuver:
            self.current_m_decoder = DecoderResCat(config.d_inner, config.d_embed, out_features=12)
        # end of added
        # Initialize weights and apply final processing
        self.post_init()

    def prepare_raster(self, images):
        # raster_images = np.array(images, dtype=np.float32)
        # raster_images = torch.tensor(raster_images, device=device, dtype=torch.float32)
        raster_images = images.permute(0, 3, 1, 2).contiguous().to(torch.float32)
        # print('debug: ', raster_images.shape)
        return raster_images

    @add_start_docstrings_to_model_forward(TRANSFO_XL_INPUTS_DOCSTRING)
    @add_code_sample_docstrings(
        checkpoint=_CHECKPOINT_FOR_DOC,
        output_type=TransfoXLNuPlanNSMOutput,
        config_class=_CONFIG_FOR_DOC,
    )
    def forward(
        self,
        # input_ids: Optional[torch.LongTensor] = None,
        intended_maneuver_vector: Optional[torch.LongTensor] = None,
        current_maneuver_vector: Optional[torch.LongTensor] = None,
        action_label: Optional[torch.LongTensor] = None,
        trajectory_label: Optional[torch.LongTensor] = None,
        context_actions:Optional[torch.LongTensor] = None,
        intended_maneuver_label: Optional[torch.LongTensor] = None,
        current_maneuver_label: Optional[torch.LongTensor] = None,
        high_res_raster: Optional[torch.LongTensor] = None,
        low_res_raster: Optional[torch.LongTensor] = None,

        mems: Optional[List[torch.FloatTensor]] = None,
        head_mask: Optional[torch.FloatTensor] = None,
        # inputs_embeds: Optional[torch.FloatTensor] = None,
        # labels: Optional[torch.LongTensor] = None,
        output_attentions: Optional[bool] = None,
        output_hidden_states: Optional[bool] = None,
        return_dict: Optional[bool] = None,
        **kwargs,
    ) -> Union[Tuple, TransfoXLNuPlanNSMOutput]:


        r"""
        labels (`torch.LongTensor` of shape `(batch_size,)`, *optional*):
            Labels for computing the sequence classification/regression loss. Indices should be in `[0, ...,
            config.num_labels - 1]`. If `config.num_labels == 1` a regression loss is computed (Mean-Square loss), If
            `config.num_labels > 1` a classification loss is computed (Cross-Entropy).
        """

        # with history menuever label input
        if self.use_nsm:
            if len(intended_maneuver_vector.shape) == 2 and len(current_maneuver_vector.shape) == 3:
                if self.per_instance:
                    intended_maneuver_vector = intended_maneuver_vector[:, -1] 
                    current_maneuver_vector = current_maneuver_vector[:, -1, :]
                elif not self.per_instance and self.maneuver_repeat:
                    intended_maneuver_vector = intended_maneuver_vector[:, -1].unsqueeze(1).repeat(1, 9)
                    current_maneuver_vector = current_maneuver_vector[:, -1, :].unsqueeze(1).repeat(1, 9, 1)
            # without history menuever label input
            else: 
                intended_maneuver_vector = intended_maneuver_vector.unsqueeze(1).repeat(1, 9)
                current_maneuver_vector = current_maneuver_vector.unsqueeze(1).repeat(1, 9, 1)
        else:
            intended_maneuver_vector = None
            current_maneuver_vector = None
            intended_maneuver_label = None
            current_maneuver_label = None

        return_dict = return_dict if return_dict is not None else self.config.use_return_dict
        device = high_res_raster.device

        if self.mask_history_intended_maneuver:
            intended_maneuver_vector[:] = 0
        if self.mask_history_current_maneuver:
            current_maneuver_vector[:] = 0.0

        if intended_maneuver_vector is not None and current_maneuver_vector is not None:
            intended_maneuver_embed = self.intended_m_embed(intended_maneuver_vector.to(device))  # [bsz, hidden_size]
            current_maneuver_embed = self.current_m_embed(current_maneuver_vector.to(device))  # [bsz, hidden_size]
        else:
            intended_maneuver_embed = None
            current_maneuver_embed = None

        batch_size, h, w, total_channels = high_res_raster.shape
        ## action embedding 
        action_embeds = self.action_m_embed(context_actions)
        
        ## ratser embedding
        if not self.per_instance:
            high_res_seq = self.cat_raster_seq(high_res_raster.permute(0, 3, 2, 1).to(device))
            low_res_seq = self.cat_raster_seq(low_res_raster.permute(0, 3, 2, 1).to(device))
            batch_size, context_length, c, h, w = high_res_seq.shape
            # embed with the format of (batchsize*history, n_embed) => (batchsize, history, n_embed): both high and low res => (batchsize, history, 2*n_embed) 
            high_res_embed = self.cnn_downsample(high_res_seq.to(torch.float32).reshape(batch_size*context_length, c, h, w))
            low_res_embed = self.cnn_downsample(low_res_seq.to(torch.float32).reshape(batch_size*context_length, c, h, w))
            high_res_embed = high_res_embed.reshape(batch_size, context_length, -1)
            low_res_embed = low_res_embed.reshape(batch_size, context_length, -1)
        
        else:
            # embed for per-instance with the format (batchsize, total_channel, n_embed)
            high_res_embed = self.cnn_downsample(high_res_raster.permute(0, 3, 1, 2).reshape(-1, 1, h, w).to(torch.float32)).view(batch_size, total_channels, -1)
            low_res_embed = self.cnn_downsample(low_res_raster.permute(0, 3, 1, 2).reshape(-1, 1, h, w).to(torch.float32)).view(batch_size, total_channels, -1)
            # insert context to raster embedding, expected format is (batchsize, total_channel + context_length, n_embed)
            high_res_embed = self.insert_action(high_res_embed, action_embeds)
            low_res_embed = self.insert_action(low_res_embed, action_embeds)

        if intended_maneuver_embed is not None and current_maneuver_embed is not None:
            if self.per_instance:
                state_embeds = torch.cat((intended_maneuver_embed.unsqueeze(1),
                                          current_maneuver_embed.unsqueeze(1),
                                          high_res_embed,
                                          low_res_embed), dim=1).to(torch.float32)
            else:
                state_embeds = torch.cat((intended_maneuver_embed,
                                          current_maneuver_embed,
                                          high_res_embed,
                                          low_res_embed), dim=-1).to(torch.float32)
        else:
            if self.per_instance:
                state_embeds = torch.cat((high_res_embed,
                                          low_res_embed), dim=1).to(torch.float32)
            else:
                state_embeds = torch.cat((high_res_embed,
                                          low_res_embed), dim=-1).to(torch.float32)
        
        if trajectory_label is not None:
            trajectory_label = trajectory_label[:, 1::2, :] # downsample the 20hz trajectory to 10hz
            if self.predict_single_step_trajectory:
                trajectory_label = trajectory_label[:, :5, :]
            pred_length = trajectory_label.shape[1]
        else:
            pred_length = 80
        
        if not self.per_instance:
            # n_embed is 2/4 multiple because different embeddings are concated togaher at the same timestep.
            n_embed = action_embeds.shape[-1]
            input_embeds = torch.zeros(
                (batch_size, context_length * 2 - 1, n_embed),
                dtype=torch.float32,
                device=device
            )
            input_embeds[:, ::2, :] = state_embeds
            input_embeds[:, 1::2, :] = action_embeds
            if not self.predict_single_step_trajectory:
                # to keep input and output at the same dimension
                input_embeds = torch.cat([input_embeds, torch.zeros((batch_size, pred_length - 2 * context_length + 1, n_embed), device=device)], dim=1)
        else:
            input_embeds = state_embeds
        
        transformer_outputs = self.transformer(
            None,
            mems=mems,
            head_mask=head_mask,
            inputs_embeds=input_embeds,
            output_attentions=output_attentions,
            output_hidden_states=output_hidden_states,
            return_dict=return_dict,
        )
        transformer_outputs_hidden_state = transformer_outputs['last_hidden_state']

        assert (
            self.config.pad_token_id is not None or batch_size == 1
        ), "Cannot handle batch sizes > 1 if no padding token is defined."

        intended_m_logits = None
        current_m_logits = None
        if self.predict_intended_maneuver and intended_maneuver_vector is not None:
            intended_m_logits = self.intended_m_decoder(transformer_outputs_hidden_state[:, 0, :])
        if self.predict_current_maneuver and current_maneuver_vector is not None:
            current_m_logits = self.current_m_decoder(transformer_outputs_hidden_state[:, 1, :])
            current_c_confifence = torch.softmax(current_m_logits, dim=-1)

        if self.traj_decoder is not None:
            # expected shape for pred trajectory is (b, pred_length, 4)
            # TODO
            traj_pred = self.traj_decoder(transformer_outputs_hidden_state[:, :pred_length, :])
        else:
            traj_pred = None

        if self.predict_trajectory_with_nsm:
            assert not self.predict_trajectory, 'Duplicate loss computation, donnot use predict_trajectory and predict_trajectory_with_nsm at the same time'
            lerp_weights = torch.arange(1.0, 1.0 + pred_length).float().to(device) / pred_length
            # interpolated_weights: [batch_size, pred_length, 12], linear interpolated from current to predicted next step weights
            interpolated_weights = torch.lerp(current_maneuver_label.unsqueeze(1).repeat(1, pred_length, 1),  # [20, 12] -> [20, pred_length, 12]
                                              current_c_confifence.unsqueeze(1).repeat(1, pred_length, 1),  #[20, 12] -> [20, pred_length, 12]
                                              lerp_weights.unsqueeze(0).unsqueeze(-1).repeat(batch_size, 1, 12))  #[pred_length] -> [1, pred_length, 12]
            # [batch_size, pred_length, d_embed] -> [batch_size, pred_length, d_embed]
            traj_hidden_state = self.nsm_decoder(hidden_states=transformer_outputs_hidden_state[:, 2:pred_length+2, :].reshape(-1, n_embed),
                                                 weight_blend=interpolated_weights.view(-1, 12))
            # traj_pred: [batch_size, pred_length, 4]
            traj_pred = self.traj_decoder(traj_hidden_state.reshape(batch_size, pred_length, n_embed))

        loss = torch.tensor(0, dtype=torch.float32, device=device)
        self.config_problem_type = 'NuPlan_NSM_SingleStep_Planning'
        if self.not_same_scale != 1:
            scaler = torch.ones(intended_maneuver_label.shape, dtype=torch.float32, device=device) * self.not_same_scale
            ones = torch.ones(intended_maneuver_label.shape, dtype=torch.float32, device=device)
            scaler[intended_maneuver_label==intended_maneuver_vector] = ones[intended_maneuver_label==intended_maneuver_vector]

        if self.predict_intended_maneuver and intended_maneuver_label is not None:
            loss_fct = CrossEntropyLoss()
            loss_to_add = loss_fct(intended_m_logits.view(-1, 12), intended_maneuver_label.view(-1).long())
            if self.not_same_scale != 1:
                loss += loss_to_add * torch.mean(scaler)
            else:
                loss += loss_to_add

        if self.predict_current_maneuver and current_maneuver_label is not None:
            loss_fct = MSELoss()
            loss_to_add = loss_fct(current_c_confifence.squeeze(), current_maneuver_label.squeeze()) * 10000
            if self.not_same_scale != 1:
                loss += loss_to_add * torch.mean(scaler)
            else:
                loss += loss_to_add        
        if trajectory_label is not None and self.traj_decoder is not None:
            loss_fct = MSELoss(reduction="mean")
            loss += loss_fct(traj_pred, trajectory_label.to(device)) * 10000

        pooled_logits = [intended_m_logits, current_m_logits, traj_pred]
        if not return_dict:
            output = (pooled_logits,) + transformer_outputs[1:]
            return ((loss,) + output) if loss is not None else output

        return TransfoXLNuPlanNSMOutput(
            loss=loss,
            logits=current_m_logits.cpu() if current_m_logits is not None else 0,
            mems=transformer_outputs.mems,
            hidden_states=transformer_outputs.hidden_states,
            attentions=transformer_outputs.attentions,
            all_logits=pooled_logits
        )
        
    def cat_raster_seq(self, raster:Optional[torch.LongTensor]):
        """
        input raster can be either high resolution raster or low resolution raster
        expected input size: [bacthsize, channel, h, w], and channel is consisted of goal(1d)+roadtype(20d)+agenttype*time(8*9d)
        """
        framenum = 9 # default for 2s and 5hz sampling
        b, c, h, w = raster.shape
        agent_type = 8
        road_type = 20

        goal_raster = raster[:, 0, :, :].reshape(b, 1, h, w)
        road_ratser = raster[:, 1:21, :, :]
        result = torch.zeros((b, framenum, agent_type + road_type + 1, h, w), device=raster.device)
        for i in range(framenum):
            agent_raster = raster[:, 1 + road_type + i::framenum, :, :]
            raster_i = torch.cat([goal_raster, road_ratser, agent_raster], dim = 1) # expected format (b, 1+20+8, h, w)
            result[:, i, :, :, :] = raster_i
        # return format (batchsize, history_frame_number, channels_per_frame, h, w)
        return result

    def insert_action(self, raster_embed, actions_embed, step=8):
        goal_embed = raster_embed[:, 0, :].unsqueeze(1)
        road_embed = raster_embed[:, 1:21, :]
        result = torch.cat([goal_embed, road_embed], dim=1)
        context_length = actions_embed.shape[1]
        for i in range(context_length):
            result = torch.cat([result, raster_embed[:, 21+i*step:21+(i+1)*step, :], actions_embed[:, i, :].unsqueeze(1)], dim=1)
        # concat the last observation->[o,a,o,a ..., o]
        result = torch.cat([result, raster_embed[:, -step:, :]], dim=1)
        return result

class GPTModelNuPlan(GPT2PreTrainedModel):
    def __init__(self, config, **kwargs):
        super().__init__(config)
        self.transformer = GPT2Model(config)
        model_args = kwargs['model_args']
        self.use_nsm = model_args.use_nsm
        self.predict_trajectory = model_args.predict_trajectory
        self.predict_intended_maneuver = model_args.predict_intended_maneuver
        self.predict_current_maneuver = model_args.predict_current_maneuver
        self.recover_obs = model_args.recover_obs
        self.time_to_predict = model_args.time_to_predict
        self.frequency_for_prediction = model_args.frequency_for_prediction
        self.not_same_scale = model_args.scale_on_not_same_loss
        self.predict_single_step_trajectory = model_args.predict_single_step_trajectory
        self.predict_trajectory_with_nsm = model_args.predict_trajectory_with_nsm
        self.mask_history_intended_maneuver = model_args.mask_history_intended_maneuver
        self.mask_history_current_maneuver = model_args.mask_history_current_maneuver

        assert self.predict_trajectory or self.predict_intended_maneuver or self.predict_current_maneuver or self.predict_single_step_trajectory, 'Predict at least one target! Pass True in Model Args'

        in_channels = 29 # raster: goal + road_type + agent_type    
        n_embed = config.n_embd // 2

        self.cnn_downsample = CNNDownSamplingResNet18(n_embed, in_channels=in_channels)
        
        self.intended_m_embed = nn.Sequential(nn.Embedding(num_embeddings=30, embedding_dim=n_embed), nn.Tanh())
        self.current_m_embed = nn.Sequential(nn.Linear(12, n_embed, bias=False), nn.Tanh())
        self.action_m_embed = nn.Sequential(nn.Linear(4, config.n_embd), nn.Tanh())

        if self.predict_trajectory_with_nsm:
            self.nsm_decoder = NSMDecoder(config.n_embd)

        self.traj_decoder = None
        if self.predict_trajectory or self.predict_single_step_trajectory:
            self.traj_decoder = DecoderResCat(model_args.d_inner, config.n_embd, out_features=4)
        if self.predict_intended_maneuver:
            self.intended_m_decoder = DecoderResCat(model_args.d_inner, config.n_embd, out_features=12)
        if self.predict_current_maneuver:
            self.current_m_decoder = DecoderResCat(model_args.d_inner, config.n_embd, out_features=12)
        if self.recover_obs:
            self.obs_embed_decoder = DecoderResCat(model_args.d_inner, config.n_embd, out_features=config.n_embd)
        # end of added
        # Initialize weights and apply final processing
        self.model_parallel = False
        self.device_map = None
        self.post_init()

    @add_start_docstrings(PARALLELIZE_DOCSTRING)
    def parallelize(self, device_map=None):
        warnings.warn(
            "`GPT2LMHeadModel.parallelize` is deprecated and will be removed in v5 of Transformers, you should load"
            " your model with `device_map='balanced'` in the call to `from_pretrained`. You can also provide your own"
            " `device_map` but it needs to be a dictionary module_name to device, so for instance {'transformer.h.0':"
            " 0, 'transformer.h.1': 1, ...}",
            FutureWarning,
        )
        self.device_map = (
            get_device_map(len(self.transformer.h), range(torch.cuda.device_count()))
            if device_map is None
            else device_map
        )
        assert_device_map(self.device_map, len(self.transformer.h))
        self.transformer.parallelize(self.device_map)
        self.cnn_downsample = self.cnn_downsample.to(self.transformer.first_device)
        self.intended_m_embed = self.intended_m_embed.to(self.transformer.first_device)
        self.current_m_embed = self.current_m_embed.to(self.transformer.first_device)
        self.intended_m_decoder = self.intended_m_decoder.to(self.transformer.first_device)
        self.current_m_decoder = self.current_m_decoder.to(self.transformer.first_device)
        self.nsm_decoder = self.nsm_decoder.to(self.transformer.first_device)
        self.traj_decoder = self.traj_decoder.to(self.transformer.first_device)
        self.model_parallel = True

    @add_start_docstrings(DEPARALLELIZE_DOCSTRING)
    def deparallelize(self):
        warnings.warn(
            "Like `parallelize`, `deparallelize` is deprecated and will be removed in v5 of Transformers.",
            FutureWarning,
        )
        self.transformer.deparallelize()
        self.transformer = self.transformer.to("cpu")
        self.cnn_downsample = self.cnn_downsample.to("cpu")
        self.intended_m_embed = self.intended_m_embed.to("cpu")
        self.current_m_embed = self.current_m_embed.to("cpu")
        self.intended_m_decoder = self.intended_m_decoder.to("cpu")
        self.current_m_decoder = self.current_m_decoder.to("cpu")
        self.nsm_decoder = self.nsm_decoder.to("cpu")
        self.traj_decoder = self.traj_decoder.to("cpu")
        self.model_parallel = False
        torch.cuda.empty_cache()
    
    @property
    def mode(self):
        # pred mode: Obs-Maneuver-Action Pair: [m,a | o,m,a | ... | o,m,a]
        if (self.predict_trajectory or self.predict_trajectory_with_nsm)\
            and self.predict_current_maneuver and self.predict_intended_maneuver \
            and self.recover_obs:
            return "PRED-OMA"
        
        # pred mode: Maneuver-Action Pair: [m,a | m,a |... | m,a]
        elif (self.predict_trajectory or self.predict_trajectory_with_nsm) \
            and self.predict_current_maneuver and self.predict_intended_maneuver \
            and not self.recover_obs:
            return "PRED-MA"
        
        # pred mode: Only Action
        elif (self.predict_trajectory or self.predict_trajectory_with_nsm) \
            and not self.predict_current_maneuver and not self.predict_intended_maneuver \
            and not self.recover_obs:
            return "PRED-A"
        
    def forward(
        self,
        intended_maneuver_vector: Optional[torch.Tensor] = None,
        current_maneuver_vector: Optional[torch.Tensor] = None,
        high_res_raster: Optional[torch.Tensor] = None,
        low_res_raster: Optional[torch.Tensor] = None,
        trajectory: Optional[torch.Tensor] = None,
        past_key_values: Optional[Tuple[Tuple[torch.Tensor]]] = None,
        attention_mask: Optional[torch.FloatTensor] = None,
        token_type_ids: Optional[torch.LongTensor] = None,
        position_ids: Optional[torch.LongTensor] = None,
        head_mask: Optional[torch.FloatTensor] = None,
        encoder_hidden_states: Optional[torch.Tensor] = None,
        encoder_attention_mask: Optional[torch.FloatTensor] = None,
        use_cache: Optional[bool] = None,
        output_attentions: Optional[bool] = None,
        output_hidden_states: Optional[bool] = None,
        return_dict: Optional[bool] = None,
        past_seq: Optional[int] = 8,
        **kwargs
    ) -> Union[Tuple, CausalLMOutputWithCrossAttentions]:
        """
        intended_maneuver_vector:  batch_size, seq
        current_maneuver_vector: batch_size, seq, 12
        high_res_raster: batch_size, seq, h, w, c (c=29)
        low_res_raster: batch_size, seq, h, w, c (c=29)
        trajectory: batch_size, seq, 4
        """
        if len(high_res_raster.shape) == 4: # convert (b, h, w, seq*c) ->(b, seq, h, w, c)
            _b, _h, _w, _= high_res_raster.shape
            high_res_raster = high_res_raster.reshape(_b, _h, _w, -1, 29).permute(0, 3, 1, 2, 4)
            low_res_raster = low_res_raster.reshape(_b, _h, _w, -1, 29).permute(0, 3, 1, 2, 4)
        
        return_dict = return_dict if return_dict is not None else self.config.use_return_dict
        device = high_res_raster.device
        # with history menuever label input
            
        if self.mask_history_intended_maneuver:
            intended_maneuver_vector[:] = 0
        if self.mask_history_current_maneuver:
            current_maneuver_vector[:] = 0.0

        if intended_maneuver_vector is not None and current_maneuver_vector is not None:
            intended_maneuver_embed = self.intended_m_embed(intended_maneuver_vector.to(device))  # [bsz, hidden_size]
            current_maneuver_embed = self.current_m_embed(current_maneuver_vector.to(device))  # [bsz, hidden_size]

        ## ratser embedding and concat to state embedding
        high_res_raster = high_res_raster.permute(0, 1, 4, 2, 3)
        low_res_raster = low_res_raster.permute(0, 1, 4, 2, 3)
        batch_size, seq, c, h, w = high_res_raster.shape
        future_seq = seq - past_seq
        # embed with the format of (batchsize*history, n_embed) => (batchsize, history, n_embed): both high and low res => (batchsize, history, 2*n_embed) 
        high_res_embed = self.cnn_downsample(high_res_raster.to(torch.float32).reshape(batch_size * seq, c, h, w)).reshape(batch_size, seq, -1)
        low_res_embed = self.cnn_downsample(low_res_raster.to(torch.float32).reshape(batch_size * seq, c, h, w)).reshape(batch_size, seq, -1)
        
        state_embeds = torch.cat((high_res_embed,
                                      low_res_embed), dim=-1).to(torch.float32)
        ## maneuver embedding
        if intended_maneuver_embed is not None and current_maneuver_embed is not None:       
            maneuver_embeds = torch.cat((intended_maneuver_embed,
                                          current_maneuver_embed), dim=-1).to(torch.float32)
        else:
            maneuver_embeds = None
        
        ## action embedding 
        action_embeds = self.action_m_embed(trajectory)
        n_embed = action_embeds.shape[-1]
        
        # concat state embeding, maneuver embeding, action embeding
        if self.use_nsm:
            input_embeds_past = torch.cat((
                    torch.zeros_like(state_embeds[:, :past_seq+1, :]), torch.zeros_like(maneuver_embeds[:, :past_seq, :]), torch.zeros_like(action_embeds[:, :past_seq, :])
                ), dim=1)
            input_embeds_past[:, ::3, :] = state_embeds[:, :past_seq+1, :]
            input_embeds_past[:, 1::3, :] = maneuver_embeds[:, :past_seq, :]
            input_embeds_past[:, 2::3, :] = action_embeds[:, :past_seq, :]
        else:
            input_embeds_past = torch.cat((
                torch.zeros_like(state_embeds[:, :past_seq+1]), torch.zeros_like(action_embeds[:, :past_seq, :])
            ), dim=1)
            input_embeds_past[:, ::2, :] = state_embeds[:, :past_seq+1, :]
            input_embeds_past[:, 1::2, :] = action_embeds[:, :past_seq, :]

        total_past_length = input_embeds_past.shape[1]
        if self.mode == "PRED-OMA":
            if self.use_nsm:
                input_embeds = torch.cat((
                    torch.zeros_like(state_embeds), torch.zeros_like(maneuver_embeds), torch.zeros_like(action_embeds)
                ), dim=1)
                input_embeds[:, ::3, :] = state_embeds
                input_embeds[:, 1::3, :] = maneuver_embeds
                input_embeds[:, 2::3, :] = action_embeds
            else:
                input_embeds_future = torch.cat((
                    torch.zeros_like(maneuver_embeds[:, past_seq:, :]), torch.zeros_like(action_embeds[:, past_seq:, :]),torch.zeros_like(state_embeds[:, past_seq+1:, :])
                ),dim=1)
                input_embeds_future[:, ::3, :] = maneuver_embeds[:, past_seq:, :]
                input_embeds_future[:, 1::3, :] = action_embeds[:, past_seq:, :]
                input_embeds_future[:, 2::3, :] = state_embeds[:, past_seq+1:, :]
                input_embeds = torch.cat((input_embeds_past, input_embeds_future), dim=1)
        else: 
            
            if self.mode == "PRED-MA":
                input_embeds_future = torch.cat((
                    torch.zeros_like(maneuver_embeds[:, past_seq:, :]), torch.zeros_like(action_embeds[:, past_seq:, :])
                ), dim=1)
                input_embeds_future[:, ::2, :] = maneuver_embeds[:, past_seq:, :]
                input_embeds_future[:, 1::2, :] = action_embeds[:, past_seq:, :]
            elif self.mode == "PRED-A":
                input_embeds_future = action_embeds[:, past_seq:, :]
            input_embeds = torch.cat((input_embeds_past, input_embeds_future), dim=1)
        # input_embeds = torch.zeros(
        #     (batch_size, input_seq, n_embed), dtype=torch.float32, device=device)
        # input_embeds[:, ::3, :] = state_embeds
        # input_embeds[:, 1::3, :] = maneuver_embeds
        # input_embeds[:, 2::3, :] = action_embeds

        transformer_outputs = self.transformer(
            past_key_values=past_key_values,
            attention_mask=attention_mask,
            token_type_ids=token_type_ids,
            position_ids=position_ids,
            head_mask=head_mask,
            inputs_embeds=input_embeds,
            encoder_hidden_states=encoder_hidden_states,
            encoder_attention_mask=encoder_attention_mask,
            use_cache=use_cache,
            output_attentions=output_attentions,
            output_hidden_states=output_hidden_states,
            return_dict=return_dict,
        )
        hidden_states = transformer_outputs[0]
        # compute correspond hidden states to predict
        if self.use_nsm:
            manuever_hidden_states_past = hidden_states[:, :total_past_length-1, :][:, ::3, :]
            action_hidden_states_past = hidden_states[:, :total_past_length-1, :][:, 1::3, :]
            if self.mode == "PRED-OMA":
                manuever_hidden_states = hidden_states[:, ::3, :]
                action_hidden_states = hidden_states[:, 1::3, :]
                obs_recover_hidden_states = hidden_states[:, 2::3, :]   
            elif self.mode == "PRED-MA":
                manuever_hidden_states_future = hidden_states[:, total_past_length-1:-1, :][:, ::2]
                action_hidden_states_future = hidden_states[:, total_past_length-1:, :][:, 1::2]
                manuever_hidden_states = torch.cat((manuever_hidden_states_past, manuever_hidden_states_future), dim=1)
                action_hidden_states = torch.cat((action_hidden_states_past, action_hidden_states_future), dim=1)
            elif self.mode == "PRED-A":
                action_hidden_states_future = hidden_states[:, total_past_length-1:-1, :]
                action_hidden_states = torch.cat((action_hidden_states_past, action_hidden_states_future), dim=1)
        else:
            manuever_hidden_states_past = hidden_states[:, :total_past_length-1, :][:, ::2, :]
            action_hidden_states_past = hidden_states[:, :total_past_length-1, :][:, ::2, :]
            obs_recover_hidden_states_past = hidden_states[:, :total_past_length-1, :][:, 1::2, :]
            if self.mode == "PRED-OMA":
                manuever_hidden_states_future = hidden_states[:, total_past_length-1:-1, :][:, ::3]
                action_hidden_states_future = hidden_states[:, total_past_length-1:, :][:, 1::3]
                obs_recover_hidden_states_future = hidden_states[:, total_past_length-1:, :][:, 2::3]
                manuever_hidden_states = torch.cat((manuever_hidden_states_past, manuever_hidden_states_future), dim=1)
                obs_recover_hidden_states = torch.cat((obs_recover_hidden_states_past, obs_recover_hidden_states_future), dim=1)               
            elif self.mode == "PRED-MA":
                manuever_hidden_states_future = hidden_states[:, total_past_length-1:-1, :][:, ::2]
                action_hidden_states_future = hidden_states[:, total_past_length-1:, :][:, 1::2]
                manuever_hidden_states = torch.cat((manuever_hidden_states_past, manuever_hidden_states_future), dim=1)
            elif self.mode == "PRED-A":
                action_hidden_states_future = hidden_states[:, total_past_length-1:-1, :]
            action_hidden_states = torch.cat((action_hidden_states_past, action_hidden_states_future), dim=1)
        
        intended_m_logits = None
        current_m_logits = None
        traj_logits = None
        if self.predict_intended_maneuver and intended_maneuver_vector is not None:
            intended_m_logits = self.intended_m_decoder(manuever_hidden_states)
        if self.predict_current_maneuver and current_maneuver_vector is not None:
            current_m_logits = self.current_m_decoder(manuever_hidden_states)
            current_c_confifence = torch.softmax(current_m_logits, dim=-1)
        
        if self.traj_decoder is not None and not self.predict_trajectory_with_nsm:
            # expected shape for pred trajectory is (b, pred_length, 4)
            traj_logits = self.traj_decoder(action_hidden_states)

        if self.predict_trajectory_with_nsm:
            assert not self.predict_trajectory, 'Duplicate loss computation, donnot use predict_trajectory and predict_trajectory_with_nsm at the same time'
            lerp_weights = torch.arange(1.0, 1.0 + seq).float().to(device) / seq
            # interpolated_weights: [batch_size, pred_length, 12], linear interpolated from current to predicted next step weights
            interpolated_weights = torch.lerp(current_maneuver_vector,  # [bsz, seq, 12]
                                              current_c_confifence,  #[bsz, seq, 12]
                                              lerp_weights.unsqueeze(0).unsqueeze(-1).repeat(batch_size, 1, 12))  #[pred_length] -> [1, pred_length, 12]
            # [batch_size, pred_length, d_embed] -> [batch_size, pred_length, d_embed]
            traj_hidden_state = self.nsm_decoder(hidden_states=traj_hidden_state.reshape(-1, n_embed),
                                                 weight_blend=interpolated_weights.view(-1, 12))
            # traj_pred: [batch_size, pred_length, 4]
            traj_logits = self.traj_decoder(traj_hidden_state.reshape(batch_size, seq, n_embed))
        
        if self.recover_obs:
            obs_labels = state_embeds[:, 1:, :]
            recovered_obs_embd = self.obs_embed_decoder(obs_recover_hidden_states[:, :-1, :])
            
        loss = torch.tensor(0, dtype=torch.float32, device=device)

        if self.predict_intended_maneuver and intended_maneuver_vector is not None:
            loss_fct = CrossEntropyLoss()
            loss_to_add = loss_fct(intended_m_logits.view(-1, 12), intended_maneuver_vector.view(-1).long())    
            loss += loss_to_add

        if self.predict_current_maneuver and current_maneuver_vector is not None:
            loss_fct = MSELoss()
            loss_to_add = loss_fct(current_c_confifence.squeeze(), current_maneuver_vector.squeeze())
            loss += loss_to_add
        
        if self.predict_trajectory and self.traj_decoder is not None:
            loss_fct = MSELoss(reduction="mean")
            loss_to_add = loss_fct(traj_logits[:, :-1, :], trajectory[:, :-1, :].to(device))
            loss += loss_to_add
            final_pt_loss = loss_fct(traj_logits[:, -1, :], trajectory[:, -1, :].to(device))
            loss += final_pt_loss * 1e3 

        if self.recover_obs:
            loss_fct = MSELoss(reduction="mean")
            loss_to_add = loss_fct(recovered_obs_embd, obs_labels)
            loss += loss_to_add

        if not return_dict:
            output = (traj_logits,) + transformer_outputs[1:]
            return ((loss,) + output) if loss is not None else output

        return CausalLMOutputWithCrossAttentions(
            loss=loss,
            logits=traj_logits,
            past_key_values=transformer_outputs.past_key_values,
            hidden_states=transformer_outputs.hidden_states,
            attentions=transformer_outputs.attentions,
            cross_attentions=transformer_outputs.cross_attentions,
        )

    def generate(self, 
                intended_maneuver_vector: Optional[torch.Tensor] = None,
                current_maneuver_vector: Optional[torch.Tensor] = None,
                high_res_raster: Optional[torch.Tensor] = None,
                low_res_raster: Optional[torch.Tensor] = None,
                trajectory: Optional[torch.Tensor] = None,
                attention_mask: Optional[torch.FloatTensor] = None,
                position_ids: Optional[torch.LongTensor] = None,
                use_cache: Optional[bool] = True,
                output_attentions: Optional[bool] = False,
                output_hidden_states: Optional[bool] = False,
                return_dict: Optional[bool] = True,
                seq_length: Optional[int] = 33,
                **kwargs):
        """
        all the input items only include the historic contents
        """
        device = high_res_raster.device
        if len(high_res_raster.shape) == 4: # convert (b, h, w, seq*c) ->(b, seq, h, w, c)
            _b, _h, _w, _= high_res_raster.shape
            high_res_raster = high_res_raster.reshape(_b, _h, _w, -1, 29).permute(0, 3, 1, 2, 4)
            low_res_raster = low_res_raster.reshape(_b, _h, _w, -1, 29).permute(0, 3, 1, 2, 4)
        if not self.use_nsm:
            intended_maneuver_vector, current_maneuver_vector = None, None
        if intended_maneuver_vector is not None and current_maneuver_vector is not None:
            intended_maneuver_embed = self.intended_m_embed(intended_maneuver_vector.to(device))
            current_maneuver_embed = self.current_m_embed(current_maneuver_vector.to(device))
        else:
            intended_maneuver_embed, current_maneuver_embed = None, None
        
        ## ratser embedding and state embedding concat
        high_res_raster = high_res_raster.permute(0, 1, 4, 2, 3)
        low_res_raster = low_res_raster.permute(0, 1, 4, 2, 3)
        batch_size, seq, c, h, w = high_res_raster.shape
        # embed with the format of (batchsize*history, n_embed) => (batchsize, history, n_embed): both high and low res => (batchsize, history, 2*n_embed) 
        high_res_embed = self.cnn_downsample(high_res_raster.to(torch.float32).reshape(batch_size * seq, c, h, w)).reshape(batch_size, seq, -1)
        low_res_embed = self.cnn_downsample(low_res_raster.to(torch.float32).reshape(batch_size * seq, c, h, w)).reshape(batch_size, seq, -1)
        state_embeds = torch.cat((high_res_embed, low_res_embed), dim=-1).to(torch.float32)
        ## maneuver embedding
        if intended_maneuver_embed is not None and current_maneuver_embed is not None:
            maneuver_embeds = torch.cat((intended_maneuver_embed, 
                                        current_maneuver_embed), dim=-1).to(torch.float32)
        else:
            maneuver_embeds = None
        ## action embedding
        action_embeds = self.action_m_embed(trajectory)
        if not self.use_nsm:
            input_embeds = torch.cat((torch.zeros_like(state_embeds, dtype=torch.float32, device=device), 
                                      torch.zeros_like(action_embeds, dtype=torch.float32, device=device)), dim=1)
            
            input_embeds[:, ::2, :] = state_embeds
            input_embeds[:, 1::2, :] = action_embeds
        else:
            input_embeds = torch.cat((torch.zeros_like(state_embeds, dtype=torch.float32, device=device), 
                                      torch.zeros_like(maneuver_embeds, dtype=torch.float32, device=device),
                                      torch.zeros_like(action_embeds, dtype=torch.float32, device=device)), dim=1)
            input_embeds[:, ::3, :] = state_embeds
            input_embeds[:, 1::3, :] = maneuver_embeds
            input_embeds[:, 2::3, :] = action_embeds
        
        # result dict
        result_to_return = dict()
        result_to_return["trajectory"] = list()
        result_to_return["intend_maneuver"] = list()
        result_to_return["current_maneuver"] = list()
        step = 0
        while True:
            # TODO: attention mask prepare and position_ids prepare
            attention_mask = self._prepare_attention_mask_for_generation(input_embeds)
            position_ids = self._prepare_position_ids_for_generation(attention_mask)
            transformer_outputs = self.transformer(
                inputs_embeds=input_embeds,
                attention_mask=attention_mask,
                position_ids=position_ids,
                use_cache=use_cache,
                output_attentions=output_attentions,
                output_hidden_states=output_hidden_states,
                return_dict=return_dict,
            )
            hidden_state = transformer_outputs[0]
            # pred mode: Obs-Maneuver-Action Pair: [m,a | o,m,a | ... | o,m,a]
            if self.mode == "PRED-OMA":
                if step > 3 * seq_length - 1:
                    break
                if step % 3 == 0:
                    intended_m_logits = self.intended_m_decoder(hidden_state[:, -1, :].unsqueeze(1))
                    current_m_logits = self.current_m_decoder(hidden_state[:, -1, :].unsqueeze(1))
                    current_c_confifence = torch.softmax(current_m_logits, dim=-1)
                    result_to_return["intend_maneuver"].append(intended_m_logits)
                    result_to_return["current_maneuver"].append(current_c_confifence)
                    next_embed = torch.cat((
                        self.intended_m_embed(torch.argmax(intended_m_logits, dim=-1)), 
                        self.current_m_embed(current_m_logits)), dim=-1)
                elif step % 3 == 1:
                    if self.predict_trajectory_with_nsm:
                        lerp_weights = torch.arange(1.0, 1.0 + seq).float().to(device) / seq
                        interpolated_weights = torch.lerp(current_maneuver_vector,  # [bsz, seq, 12]
                                                        current_c_confifence,  #[bsz, seq, 12]
                                                        lerp_weights.unsqueeze(0).unsqueeze(-1).repeat(batch_size, 1, 12))  #[pred_length] -> [1, pred_length, 12]
                        # [batch_size, pred_length, d_embed] -> [batch_size, pred_length, d_embed]
                        traj_hidden_state = self.nsm_decoder(hidden_states=hidden_state.reshape(-1, hidden_state.shape[-1]),
                                                            weight_blend=interpolated_weights.view(-1, 12))
                        # traj_pred: [batch_size, pred_length, 4]
                        traj_logits = self.traj_decoder(traj_hidden_state.reshape(batch_size, seq, hidden_state.shape[-1]))
                    elif self.predict_trajectory:
                        traj_logits = self.traj_decoder(hidden_state[:, -1, :].unsqueeze(1))
                    result_to_return["trajectory"].append(traj_logits)
                    next_embed = self.action_m_embed(traj_logits)
                elif step % 3 == 2:
                    next_embed = self.obs_embed_decoder(hidden_state[:, -1, :].unsqueeze(1))
                            
            # pred mode: Maneuver-Action Pair: [m,a | m,a |... | m,a]
            elif self.mode == "PRED-MA":
                if step > 2 * seq_length - 1:
                    break
                if step % 2 == 0:
                    intended_m_logits = self.intended_m_decoder(hidden_state[:, -1, :].unsqueeze(1))
                    current_m_logits = self.current_m_decoder(hidden_state[:, -1, :].unsqueeze(1))
                    current_c_confifence = torch.softmax(current_m_logits, dim=-1)
                    result_to_return["intend_maneuver"].append(intended_m_logits)
                    result_to_return["current_maneuver"].append(current_c_confifence)
                    next_embed = torch.cat((
                        self.intended_m_embed(torch.argmax(intended_m_logits, dim=-1)), 
                        self.current_m_embed(current_m_logits)), dim=-1)
                elif step % 2 == 1:
                    if self.predict_trajectory_with_nsm:
                        lerp_weights = torch.arange(1.0, 1.0 + seq).float().to(device) / seq
                        interpolated_weights = torch.lerp(current_maneuver_vector,  # [bsz, seq, 12]
                                                        current_c_confifence,  #[bsz, seq, 12]
                                                        lerp_weights.unsqueeze(0).unsqueeze(-1).repeat(batch_size, 1, 12))  #[pred_length] -> [1, pred_length, 12]
                        # [batch_size, pred_length, d_embed] -> [batch_size, pred_length, d_embed]
                        traj_hidden_state = self.nsm_decoder(hidden_states=hidden_state.reshape(-1, hidden_state.shape[-1]),
                                                            weight_blend=interpolated_weights.view(-1, 12))
                        # traj_pred: [batch_size, pred_length, 4]
                        traj_logits = self.traj_decoder(traj_hidden_state.reshape(batch_size, seq, hidden_state.shape[-1]))
                    elif self.predict_trajectory:
                        traj_logits = self.traj_decoder(hidden_state[:, -1, :].unsqueeze(1))
                    result_to_return["trajectory"].append(traj_logits)
                    next_embed = self.action_m_embed(traj_logits)
            
            # pred mode : Only Action
            elif self.mode == "PRED-A":
                if step > seq_length - 1:
                    break
                if self.predict_trajectory_with_nsm:
                        lerp_weights = torch.arange(1.0, 1.0 + seq).float().to(device) / seq
                        interpolated_weights = torch.lerp(current_maneuver_vector,  # [bsz, seq, 12]
                                                        current_c_confifence,  #[bsz, seq, 12]
                                                        lerp_weights.unsqueeze(0).unsqueeze(-1).repeat(batch_size, 1, 12))  
                        traj_hidden_state = self.nsm_decoder(hidden_states=hidden_state.reshape(-1, hidden_state.shape[-1]),
                                                            weight_blend=interpolated_weights.view(-1, 12))
                        traj_logits = self.traj_decoder(traj_hidden_state.reshape(batch_size, seq, hidden_state.shape[-1]))
                elif self.predict_trajectory:
                    traj_logits = self.traj_decoder(hidden_state[:, -1, :].unsqueeze(1))
                result_to_return["trajectory"].append(traj_logits)
                next_embed = self.action_m_embed(traj_logits)

            input_embeds = torch.cat((input_embeds, next_embed), dim=1)
            step += 1
        
        result_to_return["trajectory"] = torch.cat(result_to_return["trajectory"], dim=1)   
        result_to_return["intend_maneuver"] = torch.cat(result_to_return["intend_maneuver"], dim=1) if len(result_to_return["intend_maneuver"]) > 0 else None
        result_to_return["current_maneuver"] = torch.cat(result_to_return["current_maneuver"], dim=1) if len(result_to_return["current_maneuver"]) > 0 else None
        
        return result_to_return
    
    # def _prepare_model_inputs(self, input)
    def _prepare_attention_mask_for_generation(self, input_embeds):
        return torch.ones(input_embeds.shape[:2], dtype=torch.long, device=input_embeds.device)

    def _prepare_position_ids_for_generation(self, attention_mask):
        position_ids = attention_mask.long().cumsum(-1) - 1
        position_ids.masked_fill_(attention_mask == 0, 1)
        return position_ids
    
if  __name__ == '__main__':
    import datasets
    import argparse, time, pickle
    parser = argparse.ArgumentParser()
    parser.add_argument("--use_nsm", default=True)
    parser.add_argument("--predict_intended_maneuver", default=True)
    parser.add_argument("--predict_current_maneuver", default=True)
    parser.add_argument("--predict_trajectory", default=True)
    parser.add_argument("--recover_obs", default=True)
    # parser.add_argument("--per_instance_encoding", default=False)
    parser.add_argument("--time_to_predict", default=8)
    parser.add_argument("--frequency_for_prediction", default=20)
    parser.add_argument("--scale_on_not_same_loss", default=1.0)
    parser.add_argument("--maneuver_repeat", default=True)
    parser.add_argument("--predict_single_step_trajectory", default=False)
    parser.add_argument("--predict_trajectory_with_nsm", default=False)
    parser.add_argument("--mask_history_intended_maneuver", default=False)
    parser.add_argument("--mask_history_current_maneuver", default=False)
    
    parser.add_argument("--d_inner", default=1024)
    model_args = parser.parse_args()

    # model = TransfoXLModelNuPlan.from_pretrained('transfo-xl-wt103', model_args=model_args)
    # model.config.pad_token_id = 0
    dataset = datasets.load_from_disk("/media/shiduozhang/My Passport/nuplan/nsm_array3d")
    dataset.set_format("torch",columns=['trajectory','high_res_raster','low_res_raster','intended_maneuver_vector','current_maneuver_vector'])
    print(dataset.features)
    start = time.time()
    example = dataset[0]
    print(time.time() - start)

    # with open("autoregressive_data_3d.pkl", "wb") as f:
    #     pickle.dump(example, f)
    # with open("autoregressive_data_3d.pkl", "rb") as f:    
    #     example = pickle.load(f)
    # # shuffle example
    # # dataset = dataset.shuffle(seed=42)
    # # start = time.time()
    # # example = dataset[0]
    # # print(time.time() - start)

    # # # fix shuffle
    # dataset = dataset.flatten()
    # start = time.time()
    # example = dataset[0]
    # print(time.time() - start)

    # # 
    # dataset = datasets.load_from_disk("/home/shiduozhang/nuplan/dataset/nsm_sparse_balance")
    # # print(dataset.features)
    # start = time.time()
    # example = dataset[0]
    # print(time.time() - start)
    # result = model.forward(
    #     intended_maneuver_label=example['intended_maneuver_label'].unsqueeze(0),
    #     intended_maneuver_vector=example['intended_maneuver_vector'].unsqueeze(0).unsqueeze(0).repeat(1, 9),
    #     current_maneuver_label=example['current_maneuver_label'].unsqueeze(0),
    #     current_maneuver_vector=example['current_maneuver_vector'].unsqueeze(0).unsqueeze(0).repeat(1, 9, 1),
    #     action_label=None,
    #     trajectory_label=example['trajectory_label'].unsqueeze(0),
    #     context_actions=example['context_actions'][:8].unsqueeze(0),
    #     high_res_raster=example['high_res_raster'][:,:,:93].unsqueeze(0),
    #     low_res_raster=example['low_res_raster'][:,:,:93].unsqueeze(0),
    #     mems=None,
    #     head_mask=None,
    #     output_attentions=None,
    #     output_hidden_states=None,
    #     return_dict=True,
    # )
    model = GPTModelNuPlan.from_pretrained('gpt2', model_args=model_args)
    result = model.generate(
        intended_maneuver_vector=torch.zeros(2,8,dtype=torch.int32),
        current_maneuver_vector=torch.zeros(2,8,12),
        high_res_raster=torch.zeros(2,9,224,224,29),
        low_res_raster=torch.zeros(2,9,224,224,29),
        trajectory=torch.zeros(2,8,4),
        return_dict=True,
    )
    result = model(
        intended_maneuver_vector=example["intended_maneuver_vector"].unsqueeze(0),#torch.zeros(2,10,dtype=torch.int32),
        current_maneuver_vector=example["current_maneuver_vector"].unsqueeze(0),#torch.zeros(2,10,12),
        high_res_raster=example["high_res_raster"].unsqueeze(0),#torch.zeros(2,10,224,224,29),
        low_res_raster=example["low_res_raster"].unsqueeze(0),#torch.zeros(2,10,224,224,29),
        trajectory=example["trajectory"].unsqueeze(0),#torch.zeros(2,10,4),
        return_dict=True,
    )
    print("done")