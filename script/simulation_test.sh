python run_simulation.py  \
--test_type closed_loop_nonreactive_agents  \
--data_path /localssd/test  \
--map_path /cephfs/shared/nuplan-v1.1/maps  \
--model_path /cephfs/zhanjh/checkpoint/checkpoint-150000 \
--split_filter_yaml nuplan_simulation/test14_hard.yaml \
--max_scenario_num 8 \
--batch_size 8  \
--device cuda  \
--exp_folder test_pdm_simulation  \
--processes-repetition 1 \