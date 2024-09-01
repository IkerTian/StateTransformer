
SPLIT=test14_hard # test14_hard, val14_split
CHALLENGE=open_loop_boxes # open_loop_boxes, closed_loop_nonreactive_agents, closed_loop_reactive_agents
CHECKPOINT=/cephfs/zhanjh/checkpoint/epoch_102-step_130809_pdm_str_warmup.ckpt

python $NUPLAN_DEVKIT_ROOT/nuplan/planning/script/run_simulation.py \
+simulation=$CHALLENGE \
planner=pdm_hybrid_ref_planner \
planner.pdm_hybrid_planner.checkpoint_path=$CHECKPOINT \
scenario_filter=$SPLIT \
scenario_builder=nuplan \
scenario_builder.data_root=/cephfs/shared/test \
worker.threads_per_node=16 \
experiment_uid=pdm_str_warmup_102 \
hydra.searchpath="[pkg://tuplan_garage.planning.script.config.common, pkg://tuplan_garage.planning.script.config.simulation, pkg://nuplan.planning.script.config.common, pkg://nuplan.planning.script.experiments]"
# worker=single_machine_thread_pool \

# scenario_builder.data_root=$NUPLAN_DATA_ROOT/nuplan-v1.1/test \