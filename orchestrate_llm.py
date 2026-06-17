import argparse
import logging
from kubernetes import client
import sys
import os
import time
from random import Random
from typing import Callable
from kube_util import get_seed, setup_logging, get_logger, get_batch_client, run_setup_git, \
    pvc_name, namespace, make_job, Images, get_repo_path, submit_job, wait_for_job
from orchestrate import run_prep_job, run_eval_job, make_cleanup_job
from llm_connect import OpenAIChat, DeepSeekChat, OpenAILib
from prompt import system_message, get_prompt
import json

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

def run_history_job(gen: int, results_dir: str, variant: str, 
                    oneshot: bool, history_count: int, skill: bool, batch_api: client.BatchV1Api,
                    log: logging.Logger):
    g_prev = f"{results_dir}/g{gen - 1}"
    g_curr = f"{results_dir}/g{gen}"
    repo_path = get_repo_path(repo_name)
    job_name = f"sgdl-evo-history-g{gen}{('-' + variant) if variant else ''}"
    # run_command = "pypy3"
    run_command = "python"

    if oneshot:
        commands = [
            f"mkdir -p {g_curr}",
            
            f"cd {repo_path} && {run_command} job_scripts/make_llm_history.py "
            f"{g_curr} --ignore-non-existent --oneshot"
        ]
    else:
        commands = [
            f"mkdir -p {g_curr}",
            
            f"cd {repo_path} && {run_command} job_scripts/make_llm_history.py "
            f"{g_curr} --ignore-non-existent --prev-dir {g_prev} --included-history {history_count} --skill {skill}"
        ]

    job = make_job(job_name, Images.jupyter, commands)
    submit_job(batch_api, job, log)
    return wait_for_job(batch_api, job_name, log)

def prep_llm(gen: int, results_dir: str, model: str, history_count: int, skill: bool,
             variant: str, log: logging.Logger, batch_api: client.BatchV1Api) -> bool:
    g_prev = f"{results_dir}/g{gen - 1}"
    g_curr = f"{results_dir}/g{gen}"
    # TODO pull results from the PVC
    filenames = sorted([])
    mapping = {}
    for index, filename in enumerate(filenames):
        chat: OpenAILib = llm_models[model]()
        prev_skill_filename = os.path.join(g_prev, "skill.md") if skill else None
        data = [] # TODO get sgdl from filename and get its evaluation from evaluation.csv for any number of history
        response = chat.ask(get_prompt([], prev_skill_filename))
        name = "" # TODO extract from response
        new_filename = f"{index}_{name}.sgdl"
        mapping[filename] = new_filename
        # TODO extract sgdl from response and save as new_filename in g_curr
    with open(os.path.join(g_curr, "mapping.txt"), "w") as f:
        json.dump(mapping, f)
    # TODO push results to the PVC
    # TODO update skill
    run_history_job(gen, results_dir, variant, False, history_count, skill, batch_api, log)
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

    args = parser.parse_args()
    variant = args.variant
    timestamp = int(time.time())
    results_dir = args.results_dir if args.results_dir else f"/results{('-' + variant) if variant else ''}/{timestamp}"
    results_dir = f"/mnt/{results_dir}"
    start_gen = args.start_gen
    end_gen = args.end_gen
    population_size = args.population_size
    worker_count = args.eval_workers
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

    run_setup_git(batch_api, repo_url, repo_name, log)

    log.info("=" * 50)
    log.info(f"Starting evolution: gen {start_gen} to {end_gen} | Seed: {expr_seed} | Timestamp: {timestamp}")
    log.info(f"Population size {population_size} | LLM ...") # TODO
    log.info(f"Number of evaluation workers: {worker_count}")
    log.info(f"Namespace: {namespace} | PVC: {pvc_name} | Variant: {variant}")
    log.info(f"Results at {results_dir}")
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
            ok = prep_llm(gen, results_dir, llm_model, llm_history, skill, variant, log, batch_api)

        if not ok:
            log.error(f"Prep job for gen {gen} failed. Exiting.")
            sys.exit(1)

        ok = run_eval_job(gen, eval_seed, results_dir, variant, worker_count, batch_api, log)
        if not ok:
            log.error(f"Eval job for gen {gen} failed. Exiting.")
            sys.exit(1)

        # Merge partial CSVs from eval workers into evaluation.csv
        ok = make_cleanup_job(batch_api, gen, results_dir, variant, log)
        if not ok:
            log.error(f"Merge job for gen {gen} failed. Exiting.")
            sys.exit(1)

        log.info(f"Generation {gen} done.")

    log.info("All generations complete!")


if __name__ == "__main__":
    main()