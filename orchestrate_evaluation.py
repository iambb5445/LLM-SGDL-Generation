import argparse
import logging
from kubernetes import client
import sys
import os
import time
from kube_util import get_seed, setup_logging, get_logger, get_batch_client, get_core_client, run_setup_git, \
    pvc_name, namespace, pvc_transfer_session, copy_from_pvc, copy_to_pvc, submit_job, wait_for_job, Images
from orchestrate import make_job, make_eval_job, get_merge_command
from orchestrate import repo_name, repo_url

def run_eval_job(
        results_dir: str, variant: str, worker_count: int, batch_api: client.BatchV1Api,
        log: logging.Logger
    ):
    job_name = f"sgdl-evo-eval{('-' + variant) if variant else ''}"

    job = make_eval_job(job_name, results_dir, worker_count)
    submit_job(batch_api, job, log)
    return wait_for_job(batch_api, job_name, log)

def make_merge_job(dir: str, variant: str, batch_api: client.BatchV1Api, log: logging.Logger):
    job_name = f"sgdl-evo-merge{('-' + variant) if variant else ''}"
    job = make_job(job_name, Images.python, [get_merge_command(dir)])
    submit_job(batch_api, job, log)
    return wait_for_job(batch_api, job_name, log)

def main():
    parser = argparse.ArgumentParser(description="GDL Evolution Orchestrator (runs locally)")

    parser.add_argument("dir", type=str, help="Path to local directory containing sgdl files.")
    parser.add_argument('--seed', type=int, default=None, help="Integer seed to be used for evaluation.")
    parser.add_argument("--results-dir", default=None, help="Path to results dir on the PVC (as seen from inside pods). If not given, will add timestamp and variant.")
    parser.add_argument("--eval-workers", type=int, default=10, help="Number of workers used to parallelize evaluation process.")
    parser.add_argument("--variant", type=str, default="", help="Optional name suffix for job names (e.g. 'llm', 'llm-skil')")

    args = parser.parse_args()
    variant = args.variant
    timestamp = int(time.time())
    results_dir = args.results_dir if args.results_dir else f"eval{('-' + variant) if variant else ''}/{timestamp}"
    log_dir = f"./logs/{results_dir}"
    os.makedirs(os.path.dirname(log_dir), exist_ok=True)
    results_dir = f"/mnt/{results_dir}"
    worker_count = args.eval_workers
    dir = args.dir
    expr_seed: int = args.seed if args.seed is not None else get_seed(None)

    setup_logging(os.path.join(log_dir, "orchestrate.log"))
    log = get_logger(__name__)

    batch_api = get_batch_client()
    core_api = get_core_client()

    run_setup_git(batch_api, repo_url, repo_name, log)

    log.info("=" * 50)
    log.info(f"Starting Evaluation | Seed: {expr_seed} | Timestamp: {timestamp}")
    log.info(f"Number of evaluation workers: {worker_count}")
    log.info(f"Namespace: {namespace} | PVC: {pvc_name} | Variant: {variant}")
    log.info(f"Results at {results_dir} | Local Input Dir {dir}")
    log.info("=" * 50)

    with pvc_transfer_session(variant, core_api, log):
        log.info(f"Pushing {dir} -> PVC at {results_dir}")
        copy_to_pvc(dir, results_dir, variant, log)
    
    if not run_eval_job(results_dir, variant, worker_count, batch_api, log):
        log.error(f"Eval job failed. Exiting.")
        sys.exit(1)

    if not make_merge_job(results_dir, variant, batch_api, log):
        log.error(f"Merge job failed. Exiting.")
        sys.exit(1)

    log.info("Evaluation complete.")

    # download final generation to local_workdir so I can easily browse them
    final_remote = f"{results_dir}/evaluation.csv"
    final_local = os.path.join(dir, f"evaluation.csv")
    with pvc_transfer_session(variant, core_api, log):
        log.info(f"Downloading final {final_remote} -> {final_local}")
        copy_from_pvc(final_remote, final_local, variant, log)


if __name__ == "__main__":
    main()