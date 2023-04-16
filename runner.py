# coding=utf-8

"""
Train a Transformer ML Model for Planning
"""

import logging
import os
import sys
import pickle
from typing import Optional, Dict, Any
import torch
from tqdm import tqdm
import random

import datasets
import numpy as np
from datasets import Dataset
from dataclasses import dataclass, field

import transformers
from transformers import (
    AutoConfig,
    HfArgumentParser,
    TrainingArguments,
    Trainer, 
    TrainerCallback,
    set_seed,
)
from models.model import TransfoXLModelNuPlan
from transformers import TransfoXLConfig
from transformers.trainer_utils import get_last_checkpoint
from transformers.utils import check_min_version, is_offline_mode, send_example_telemetry
from transformers.utils.versions import require_version


# Will error if the minimal version of Transformers is not installed. Remove at your own risks.
logger = logging.getLogger(__name__)


@dataclass
class ModelArguments:
    """
    Arguments pertaining to which model/config/tokenizer we are going to fine-tune from.
    """
    model_name: str = field(
        default="TransfoXLModelNuPlan_Config",
        metadata={"help": "Name of a planning model backbone"}
    )
    model_pretrain_name_or_path: str = field(
        default="transfo-xl-wt103",
        metadata={"help": "Path to pretrained model or model identifier from huggingface.co/models"}
    )
    model_revision: str = field(
        default="main",
        metadata={"help": "The specific model version to use (can be a branch name, tag name or commit id)."},
    )
    use_auth_token: bool = field(
        default=False,
        metadata={
            "help": (
                "Will use the token generated when running `huggingface-cli login` (necessary to use this script "
                "with private models)."
            )
        },
    )
    predict_result_saving_dir: Optional[str] = field(
        default=False,
        metadata={"help": "The target folder to save prediction results."},
    )
    use_nsm: Optional[bool] = field(
        default=True,
    )
    predict_intended_maneuver: Optional[bool] = field(
        default=True,
    )
    predict_current_maneuver: Optional[bool] = field(
        default=True,
    )
    predict_pose: Optional[bool] = field(
        default=True,
    )
    predict_trajectory: Optional[bool] = field(
        default=True,
    )
    per_instance_encoding: Optional[bool] = field(
        default=True,
    )
    time_to_predict: Optional[int] = field(
        default=8,
    )
    frequency_for_prediction: Optional[int] = field(
        default=20,
    )
    scale_on_not_same_loss: Optional[float] = field(
        default=1.0,
    )
    maneuver_repeat: Optional[bool] = field(
        default=False
    )

@dataclass
class DataTrainingArguments:
    """
    Arguments pertaining to what data we are going to input our model for training and eval.
    """
    saved_dataset_folder: Optional[str] = field(
        default=None, metadata={"help": "The path of a pre-saved dataset folder. The dataset should be saved by Dataset.save_to_disk())."}
    )
    dataset_config_name: Optional[str] = field(
        default=None, metadata={"help": "The configuration name of the dataset to use (via the datasets library)."}
    )
    max_train_samples: Optional[int] = field(
        default=None,
        metadata={
            "help": (
                "For debugging purposes or quicker training, truncate the number of training examples to this "
                "value if set."
            )
        },
    )
    max_eval_samples: Optional[int] = field(
        default=None,
        metadata={
            "help": (
                "For debugging purposes or quicker training, truncate the number of evaluation examples to this "
                "value if set."
            )
        },
    )
    max_predict_samples: Optional[int] = field(
        default=None,
        metadata={
            "help": (
                "For debugging purposes or quicker training, truncate the number of prediction examples to this "
                "value if set."
            )
        },
    )    
    dataset_name: Optional[str] = field(
        default=None, metadata={"help": "The dataset name from hugging face used to push the model."}
    )


def main():
    # See all possible arguments in src/transformers/training_args.py
    # or by passing the --help flag to this script.
    # We now keep distinct sets of args, for a cleaner separation of concerns.

    parser = HfArgumentParser((ModelArguments, DataTrainingArguments, TrainingArguments))
    model_args, data_args, training_args = parser.parse_args_into_dataclasses()

    # Set up pytorch backend
    # if training_args.deepspeed is None:
    #     torch.distributed.init_process_group(backend='nccl')

    # Setup logging
    logging.basicConfig(
        format="%(asctime)s - %(levelname)s - %(name)s - %(message)s",
        datefmt="%m/%d/%Y %H:%M:%S",
        handlers=[logging.StreamHandler(sys.stdout)],
    )

    if training_args.should_log:
        # The default of training_args.log_level is passive, so we set log level at info here to have that default.
        transformers.utils.logging.set_verbosity_info()

    log_level = training_args.get_process_log_level()
    logger.setLevel(log_level)
    datasets.utils.logging.set_verbosity(log_level)
    transformers.utils.logging.set_verbosity(log_level)
    transformers.utils.logging.enable_default_handler()
    transformers.utils.logging.enable_explicit_format()

    # Log on each process the small summary:
    logger.warning(
        f"Process rank: {training_args.local_rank}, device: {training_args.device}, n_gpu: {training_args.n_gpu}"
        + f"distributed training: {bool(training_args.local_rank != -1)}, 16-bits training: {training_args.fp16}"
    )
    logger.info(f"Training/evaluation parameters {training_args}")

    # Detecting last checkpoint.
    last_checkpoint = None
    if os.path.isdir(training_args.output_dir) and training_args.do_train and not training_args.overwrite_output_dir:
        last_checkpoint = get_last_checkpoint(training_args.output_dir)
        if last_checkpoint is None and len(os.listdir(training_args.output_dir)) > 0:
            raise ValueError(
                f"Output directory ({training_args.output_dir}) already exists and is not empty. "
                "Use --overwrite_output_dir to overcome."
            )
        elif last_checkpoint is not None and training_args.resume_from_checkpoint is None:
            logger.info(
                f"Checkpoint detected, resuming training at {last_checkpoint}. To avoid this behavior, change "
                "the `--output_dir` or add `--overwrite_output_dir` to train from scratch."
            )

    # Set seed before initializing model.
    set_seed(training_args.seed)

    # Pass in the directory to load a saved dataset
    # See xxx.py to process and save a dataset from the NuPlan Dataset
    if os.path.isdir(data_args.saved_dataset_folder):
        nuplan_dataset = Dataset.load_from_disk(data_args.saved_dataset_folder)
        # Inspect each sample if you need
        # for each_sample in nuplan_dataset:
        #     print(each_sample['intended_maneuver_vector'])
        #     print(each_sample['intended_maneuver_label'])
        #     print(each_sample['current_maneuver_vector'])
        #     print(each_sample['current_maneuver_label'])
        nuplan_dataset.set_format(type='torch')
        print('Dataset Loaded: ', nuplan_dataset)
        nuplan_dataset = nuplan_dataset.train_test_split(test_size=0.1, shuffle=True)
    else:
        raise ValueError(f'Dataset directory ({data_args.saved_dataset_folder}) does not exist. Use save_to_disk() to save a dataset first.')

    # Load a model's pretrained weights from a path or from hugging face's model base
    if model_args.model_name == 'TransfoXLModelNuPlan':
        # Default pre-trained name for TransfoXL is 'transfo-xl-wt103'
        model = TransfoXLModelNuPlan.from_pretrained(model_args.model_pretrain_name_or_path, model_args=model_args)
        model.config.pad_token_id = 0
        model.config.eos_token_id = 0
    elif model_args.model_name == 'TransfoXLModelNuPlan_Config':
        config_p = TransfoXLConfig()
        config_p.n_layer = 4
        config_p.d_embed = 256
        config_p.d_model = 256
        config_p.d_inner = 1024
        model = TransfoXLModelNuPlan(config_p, model_args=model_args)
        # model_p.save_pretrained( '../saved_model/transformerxlSml')
        model.config.pad_token_id = 0
        model.config.eos_token_id = 0

    if training_args.do_train:
        import multiprocessing
        if 'OMP_NUM_THREADS' not in os.environ:
            os.environ["OMP_NUM_THREADS"] = str(int(multiprocessing.cpu_count() / 8))
        train_dataset = nuplan_dataset["train"]
        if data_args.max_train_samples is not None:
            max_train_samples = min(len(train_dataset), data_args.max_train_samples)
            train_dataset = train_dataset.select(range(max_train_samples))

    if training_args.do_eval:
        eval_dataset = nuplan_dataset["validation"]
        if data_args.max_eval_samples is not None:
            max_eval_samples = min(len(eval_dataset), data_args.max_eval_samples)
            eval_dataset = eval_dataset.select(range(max_eval_samples))

    if training_args.do_predict:
        predict_dataset = nuplan_dataset["test"]
        if data_args.max_predict_samples is not None:
            max_predict_samples = min(len(predict_dataset), data_args.max_predict_samples)
            predict_dataset = predict_dataset.select(range(max_predict_samples))

    # Initialize our Trainer
    trainer = Trainer(
        model=model,  # the instantiated 🤗 Transformers model to be trained
        args=training_args,  # training arguments, defined above
        train_dataset=train_dataset if training_args.do_train else None,
        eval_dataset=eval_dataset if training_args.do_eval else None,
    )

    # Training
    if training_args.do_train:
        checkpoint = None
        if training_args.resume_from_checkpoint is not None:
            checkpoint = training_args.resume_from_checkpoint
        elif last_checkpoint is not None:
            checkpoint = last_checkpoint
        train_result = trainer.train(resume_from_checkpoint=checkpoint)
        trainer.save_model()  # Saves the tokenizer too for easy upload
        trainer.save_state()

    # Evaluation
    results = {}
    if training_args.do_eval:
        logger.info("*** Evaluate ***")
        raise NotImplemented
        metrics = trainer.evaluate(metric_key_prefix="eval")
        max_eval_samples = data_args.max_eval_samples if data_args.max_eval_samples is not None else len(eval_dataset)
        metrics["eval_samples"] = min(max_eval_samples, len(eval_dataset))

        trainer.log_metrics("eval", metrics)
        trainer.save_metrics("eval", metrics)

    if training_args.do_predict:
        from sklearn.metrics import classification_report
        # Currently only supports single GPU predict outputs
        logger.info("*** Predict ***")
        """
        Compute accuracy for the following classifications:
        1. intended_maneuver
        2. current_maneuver
        3. pos_x,
        4. pos_y
        """
        if model_args.model_pretrain_name_or_path:
            state_dict = torch.load(os.path.join(model_args.model_pretrain_name_or_path, "pytorch_model.bin"))
            model.load_state_dict(state_dict)
            print("Checkpoints Loaded!")
        model.eval()
        with torch.no_grad():
            prediction_results = {
                'file_names': [],
                'current_frame': [],
                'intended_maneuver': [],
                'current_maneuver': [],
                'intended_maneuver_label': [],
                'next_step_action': [],
                'predicted_trajectory': [],
            }
            prediction_metrics = {
                'intended_maneuver': None,
                'not_same_intended_maneuver': None,
                'current_maneuver': None,
                'not_same_current_maneuver': None,
                'next_step_action': None,
                'predicted_trajectory': None,
            }        
            device = model.device
            def preprocess_data(examples):
                # take a batch of texts
                for each_key in examples:
                    if isinstance(examples[each_key], type(torch.tensor(0))):
                        examples[each_key] = examples[each_key].to(device)
                return examples        
            # TODO: add position/trajectory evaluations
            if model_args.use_nsm:
                print('Computing metrics for classifications')
                intended_m_label = []
                intended_m_vector = []
                intended_m_prediction = []
                not_same_intended_m_label = []
                not_same_intended_m_prediction = []                
                current_m_weights_bias = []
                not_same_current_m_weights_bias = []
                current_m_weights_prediction = []
            if model_args.predict_pose:
                action_bias_x = []
                action_bias_y = []
            if model_args.predict_trajectory:
                end_bias_x = []
                end_bias_y = []
                losses = []
                loss_fn = torch.nn.MSELoss(reduction="mean")
    
            # initialize intended maneuver metrics
            per_batch_size = training_args.per_device_eval_batch_size
            for input in tqdm(predict_dataset.iter(training_args.per_device_eval_batch_size)):
                if per_batch_size is None:
                    per_batch_size = len(input['intended_maneuver_label'])
                elif len(input['intended_maneuver_label']) != per_batch_size:
                    continue
                input = preprocess_data(input)
                output = model(**input)
                intended_m_logits, current_m_logits, pos_x_logits, pos_y_logits, traj_pred = output.all_logits
                file_name = input['file_name']
                current_frame_idx = input['frame_index']
                prediction_results['file_names'].append(file_name)
                prediction_results['current_frame'].append(current_frame_idx.cpu().numpy())
                if model_args.use_nsm:
                    intended_m_label.append(input['intended_maneuver_label'])  # tensor
                    intended_m_vector.append(input['intended_maneuver_vector'])  # tensor
                    if model_args.predict_intended_maneuver:
                        intended_m_prediction.append(torch.argmax(intended_m_logits, dim=-1))  # tensor
                    if model_args.predict_current_maneuver:
                        current_c_confifence = torch.softmax(current_m_logits, dim=-1)
                        current_m_weights_prediction.append(current_c_confifence)
                        current_m_weights_bias.append(torch.sum(abs(input['current_maneuver_label'] - current_c_confifence), dim=1))
                    for i in range(training_args.per_device_eval_batch_size):
                        if int(intended_m_label[-1][i]) != int(intended_m_vector[-1][i]):
                            if model_args.predict_intended_maneuver:
                                not_same_intended_m_label.append(intended_m_label[-1][i])
                                not_same_intended_m_prediction.append(intended_m_prediction[-1][i])
                            if model_args.predict_current_maneuver:
                                not_same_current_m_weights_bias.append(current_m_weights_bias[-1][i])
                if model_args.predict_pose:
                    pos_x = torch.argmax(pos_x_logits, dim=-1)
                    pos_y = torch.argmax(pos_y_logits, dim=-1)
                    action_label = input['action_label'].clone() + 100
                    action_bias_x.append(abs(pos_x - action_label[:, 0]))
                    action_bias_y.append(abs(pos_y - action_label[:, 1]))
                
                if model_args.predict_trajectory:
                    trajectory_label = input["trajectory_label"][:, 1::2, :]
                    loss = loss_fn(trajectory_label, traj_pred)
                    end_trajectory_label = trajectory_label[:, -1, :]
                    end_point = traj_pred[:, -1, :]
                    end_bias_x.append(end_trajectory_label[:, 0] - end_point[:, 0])
                    end_bias_y.append(end_trajectory_label[:, 1] - end_point[:, 1])
                    losses.append(loss)

            if model_args.use_nsm:
                if model_args.predict_intended_maneuver:
                    intended_m_label = torch.stack(intended_m_label, 0).flatten()
                    intended_m_prediction = torch.stack(intended_m_prediction, 0).flatten()
                    print('Intended Maneuver Classification')
                    prediction_metrics['intended_maneuver'] = classification_report(intended_m_prediction.cpu().numpy(), intended_m_label.cpu().numpy())
                    print(prediction_metrics['intended_maneuver'])
                    if len(not_same_intended_m_label) > 0:
                        not_same_intended_m_label = torch.stack(not_same_intended_m_label, -1).flatten()
                        not_same_intended_m_prediction = torch.stack(not_same_intended_m_prediction, -1).flatten()
                        prediction_metrics['not_same_intended_maneuver'] = classification_report(not_same_intended_m_prediction.cpu().numpy(), not_same_intended_m_label.cpu().numpy())
                        print(prediction_metrics['not_same_intended_maneuver'])
                    prediction_results['intended_maneuver'] = intended_m_prediction.cpu().numpy()
                    prediction_results['intended_maneuver_label'] = intended_m_label.cpu().numpy()
                if model_args.predict_current_maneuver:
                    current_m_weights_bias = torch.stack(current_m_weights_bias, -1).flatten()
                    current_m_weights_prediction = torch.stack(current_m_weights_prediction, 0)  # [n, batch_size, 12]
                    print('Current Maneuver Classification')
                    prediction_metrics['current_maneuver'] = np.average(current_m_weights_bias.cpu().numpy())
                    print(f'{np.average(current_m_weights_bias.cpu().numpy())} over 12')
                    if len(not_same_current_m_weights_bias) > 0:
                        not_same_current_m_weights_bias = torch.stack(not_same_current_m_weights_bias, -1).flatten()
                        prediction_metrics['not_same_current_maneuver'] = np.average(not_same_current_m_weights_bias.cpu().numpy())
                        print(f'{np.average(not_same_current_m_weights_bias.cpu().numpy())} over 12')
                    prediction_results['current_maneuver'] = current_m_weights_prediction.cpu().numpy()

                    print('inspect shape: ', prediction_results['intended_maneuver'].shape, prediction_results['current_maneuver'].shape)

            if model_args.predict_pose:
                action_bias_x = torch.stack(action_bias_x, 0).cpu().numpy()
                print('Pose x offset: ', np.average(action_bias_x))
                action_bias_y = torch.stack(action_bias_y, 0).cpu().numpy()
                print('Pose y offset: ', np.average(action_bias_y))

            if model_args.predict_trajectory:
                end_bias_x = torch.stack(end_bias_x, 0).cpu().numpy()
                end_bias_y = torch.stack(end_bias_y, 0).cpu().numpy()
                final_loss = torch.mean(torch.stack(losses, 0)).item()
                print('End point x offset: ', np.average(np.abs(end_bias_x)))
                print('End point y offset: ', np.average(np.abs(end_bias_y)))
                print('Final L2 loss of trajectory predict:', final_loss)

            if training_args.output_dir is not None:
                # save results
                output_file_path = os.path.join(training_args.output_dir, 'generated_predictions.pickle')
                with open(output_file_path, 'wb') as handle:
                    pickle.dump(prediction_results, handle, protocol=pickle.HIGHEST_PROTOCOL)

        # predict_results = trainer.predict(predict_dataset, metric_key_prefix="predict")
        # metrics = predict_results.metrics
        # max_predict_samples = (
        #     data_args.max_predict_samples if data_args.max_predict_samples is not None else len(predict_dataset)
        # )
        # metrics["predict_samples"] = min(max_predict_samples, len(predict_dataset))

        # trainer.log_metrics("predict", metrics)
        # trainer.save_metrics("predict", metrics)

        # if trainer.is_world_process_zero():
        #     if training_args.predict_with_generate:
        #         predictions = tokenizer.batch_decode(
        #             predict_results.predictions, skip_special_tokens=True, clean_up_tokenization_spaces=True
        #         )
        #         predictions = [pred.strip() for pred in predictions]
        #         output_prediction_file = os.path.join(training_args.output_dir, "generated_predictions.txt")
        #         with open(output_prediction_file, "w") as writer:
        #             writer.write("\n".join(predictions))

    kwargs = {"finetuned_from": model_args.model_pretrain_name_or_path, "tasks": "NuPlanPlanning"}
    
    # push to hub?
    if data_args.dataset_name is not None:
        kwargs["dataset_tags"] = data_args.dataset_name
        if data_args.dataset_config_name is not None:
            kwargs["dataset_args"] = data_args.dataset_config_name
            kwargs["dataset"] = f"{data_args.dataset_name} {data_args.dataset_config_name}"
        else:
            kwargs["dataset"] = data_args.dataset_name

    if training_args.push_to_hub:
        trainer.push_to_hub(**kwargs)
    else:
        trainer.create_model_card(**kwargs)

    return results


def _mp_fn(index):
    # For xla_spawn (TPUs)
    main()


if __name__ == "__main__":
    main()
