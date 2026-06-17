import argparse
import logging
from kubernetes import client
import sys
import os
import time
from random import Random
from typing import Callable
from kube_util import get_seed, setup_logging, get_logger, get_batch_client, get_core_client, run_setup_git, \
    pvc_name, namespace, pvc_transfer_session, copy_from_pvc, copy_to_pvc
from orchestrate import run_prep_job, run_eval_job, make_cleanup_job
from llm_connect import OpenAIChat, DeepSeekChat, OpenAILib
from prompt import system_message, get_prompt, process_response
import json
import pandas as pd

repo_url = "https://github.com/iambb5445/SolitaireGDL"
repo_name = "sgdl"
# I decided to put the LLM code in here instead of in another repository
# This is because nautilus nodes every once if a while have connection problems, so it would be easier to run locally
# Especially since the orchestrate code is already running locally, I can also run the LLM code
# llm_repo_url = "https://github.com/iambb5445/..."
# llm_repo_name = "llm"

llm_models: dict[str, Callable[[], OpenAILib]] = {
    'gpt4o-mini': lambda: OpenAIChat(OpenAIChat.OpenAIModel.GPT_4O_mini, system_message),
    'deepseek-r1': lambda: DeepSeekChat(DeepSeekChat.DeepSeekModel.DEEP_SEEK_REASONER, system_message),
    # TODO more
}

def ask_until_valid(chat: OpenAILib, prompt: str, prev_name: str) -> tuple[str, str|None]:
    max_retries = 3
    while True:
        try:
            response = chat.ask(prompt)
            sgdl = process_response(response)
            return prev_name, sgdl # keep the same name
        except Exception as e:
            max_retries -= 1
            if max_retries == 0:
                return prev_name, None

def get_prev_filename(local_workdir, curr_filename: str, prev_gen: int):
    with open(os.path.join(local_workdir, f"g{prev_gen}", "mapping.txt"), "w") as f:
        prev_mapping: dict[str, str] = json.load(f)
    return prev_mapping[curr_filename]

def get_name_from_filename(filename: str):
    return filename.split("_")[-1].split(".")[0]

def get_lineage(gen: int, filename: str, history_count: int, local_workdir: str):
    data: list[tuple[str, pd.DataFrame]] = []
    filename_iterator = filename
    for i in range(gen - 1, max(0, gen - history_count) - 1, -1):
        filename_iterator = get_prev_filename(local_workdir, filename_iterator, i)
        with open(os.path.join(local_workdir, f"gen{i}", filename_iterator)) as f:
            sgdl_of_past = f.read()
            name = get_name_from_filename(filename_iterator)
        eval = pd.read_csv(os.path.join(local_workdir, f"gen{i}", "history.csv")).groupby("name").get_group(name)
        data.append((sgdl_of_past, eval))
    data.reverse()
    return data

def prep_llm(gen: int, results_dir: str, local_workdir: str, model: str, history_count: int,
             skill: bool, log: logging.Logger, core_api: client.CoreV1Api) -> bool:
    g_prev = f"{results_dir}/g{gen - 1}"
    g_curr = f"{results_dir}/g{gen}"
    local_prev = os.path.join(local_workdir, f"g{gen - 1}")
    local_curr = os.path.join(local_workdir, f"g{gen}")
    os.makedirs(local_curr, exist_ok=True)

    with pvc_transfer_session(core_api, log):
        log.info(f"Pulling {g_prev} from PVC -> {local_prev}")
        copy_from_pvc(g_prev, local_prev, log)

    filenames = sorted(f for f in os.listdir(local_prev) if f.endswith(".sgdl"))
    mapping: dict[str, str] = {}
    for index, filename in enumerate(filenames):
        chat: OpenAILib = llm_models[model]()
        prev_skill_filename = os.path.join(local_prev, "skill.md") if skill else None
        lineage = get_lineage(gen, filename, history_count, local_workdir)
        name, sgdl = ask_until_valid(chat, get_prompt(lineage, prev_skill_filename), get_name_from_filename(filename))
        new_filename = f"{index}_{name}.sgdl"
        mapping[filename] = new_filename
        with open(os.path.join(local_curr, f"{index}_{name}.sgdl"), "w") as f:
            f.write(sgdl if sgdl is not None else lineage[0][0])
    with open(os.path.join(local_curr, "mapping.txt"), "w") as f:
        json.dump(mapping, f)
    if skill:
        # TODO update skill
        pass

    with pvc_transfer_session(core_api, log):
        log.info(f"Pushing {local_curr} -> PVC at {g_curr}")
        copy_to_pvc(local_curr, g_curr, log)

    return True

def main():
    parser = argparse.ArgumentParser(description="GDL Evolution Orchestrator (runs locally)")

    parser.add_argument('--seed', type=int, default=None, help="Integer seed to be used for creating the gdls.")
    parser.add_argument("--results-dir", default=None, help="Path to results dir on the PVC (as seen from inside pods). If not given, will add timestamp and variant.")
    parser.add_argument("--start-gen", type=int, default=0, help="Starting generation index. Use 0 unless this is continuing a previous run.")
    parser.add_argument("--end-gen", type=int, default=99, help="End generation, indicating generation at which this script should stop.")
    parser.add_argument("--population-size", type=int, default=100, help="Number of games per generation (including games that are already good, mutated and crossovered games, and the rest filled with random games)")
    parser.add_argument("--llm-model", type=str, required=True, help=f"Model to be used. Possible options are: {', '.join([key for key in llm_models.keys()])}")
    parser.add_argument("--llm-history", type=int, default=0, help="The number of previous steps passed to the LLM. If 0, the LLM only has access to the current version of the gdl and its evaluation. Otherwise, if exists, it can also see the previous changes it has applied to the gdl and the evaluation results of each step.")
    parser.add_argument('--skill', action="store_true", help="If true, the LLM will update a skill.md file based on each evaluation results. This file is passed to the LLM at every point and is updated only after evaluation of each generation.")
    parser.add_argument("--eval-workers", type=int, default=10, help="Number of workers used to parallelize evaluation process.")
    parser.add_argument("--variant", type=str, default="", help="Optional name suffix for job names (e.g. 'llm', 'llm-skil')")
    parser.add_argument("--local-workdir", default=None, help="Local directory used to stage generations when passing to LLM. If not given (recommended), uses timestamp and variant.")

    args = parser.parse_args()
    variant = args.variant
    timestamp = int(time.time())
    results_dir = args.results_dir if args.results_dir else f"/results{('-' + variant) if variant else ''}/{timestamp}"
    results_dir = f"/mnt/{results_dir}"
    start_gen = args.start_gen
    end_gen = args.end_gen
    population_size = args.population_size
    worker_count = args.eval_workers
    local_workdir = args.local_workdir if args.local_workdir else f"/local_workdir{('-' + variant) if variant else ''}/{timestamp}"
    llm_model: str = args.llm_model
    assert llm_model in llm_models.keys(), f"Model is not valid. Valid models are: {', '.join([key for key in llm_models.keys()])}"
    llm_history: int = args.llm_history
    assert llm_history >= 0, f"History steps should be a positive value. Received: {llm_history}"
    skill: bool = args.skill
    expr_seed: int = args.seed if args.seed is not None else get_seed(None)
    experiment_rnd = Random(expr_seed)

    setup_logging(f"{results_dir}/orchestrator.log")
    log = get_logger(__name__)

    batch_api = get_batch_client()
    core_api = get_core_client()

    run_setup_git(batch_api, repo_url, repo_name, log)

    log.info("=" * 50)
    log.info(f"Starting evolution: gen {start_gen} to {end_gen} | Seed: {expr_seed} | Timestamp: {timestamp}")
    log.info(f"Population size {population_size} | Model {llm_model} | History {llm_history} | Skill {skill}")
    log.info(f"Number of evaluation workers: {worker_count}")
    log.info(f"Namespace: {namespace} | PVC: {pvc_name} | Variant: {variant}")
    log.info(f"Results at {results_dir} | Local Workdir {local_workdir}")
    log.info("=" * 50)

    for gen in range(args.start_gen, args.end_gen + 1):
        log.info(f"\n--- Generation {gen} ---")
        gen_seed = get_seed(experiment_rnd)
        eval_seed = get_seed(experiment_rnd)
        log.info(f"Generation Seed: {gen_seed} | Evaluation Seed: {eval_seed}")

        if gen == 0:
            ok = run_prep_job(gen, Random(gen_seed), results_dir, variant, population_size,
                              0, 0, 0, 0, 0, batch_api, log)
        else:
            ok = prep_llm(gen, results_dir, local_workdir, llm_model, llm_history, skill, log, core_api)

        if not ok:
            log.error(f"Prep job for gen {gen} failed. Exiting.")
            sys.exit(1)

        ok = run_eval_job(gen, eval_seed, results_dir, variant, worker_count, batch_api, log)
        if not ok:
            log.error(f"Eval job for gen {gen} failed. Exiting.")
            sys.exit(1)

        ok = make_cleanup_job(batch_api, gen, results_dir, variant, log, oneshot=False,
                              build_history=(gen > 0), history_count=llm_history, skill=skill)
        if not ok:
            log.error(f"Merge job for gen {gen} failed. Exiting.")
            sys.exit(1)

        log.info(f"Generation {gen} done.")

    log.info("All generations complete!")


if __name__ == "__main__":
    main()