import argparse
import logging
from kubernetes import client
import sys
import time
from random import Random
from kube_util import get_seed, setup_logging, get_logger, get_batch_client, run_setup_git, \
    make_job, wait_for_job, submit_job, get_repo_path, pvc_name, namespace, Images

repo_url = "https://github.com/iambb5445/SolitaireGDL"
repo_name = "sgdl"

def make_eval_job(job_name: str, seed: int, gen_dir: str, num_workers: int):
    repo_path = get_repo_path(repo_name)
    # Each worker: pip install pandas (if not in image already), then run evaluate.py
    # JOB_COMPLETION_INDEX is injected automatically by k8s Indexed Jobs (it's magic)
    # JOB_COMPLETION_INDEX is per pod, so it won't depend on other jobs (e.g. mutation job) or previous generation
    # JOB_COMPLETION_INDEX is also the same after a retry, so the index won't be wrong if a job fails and retries
    cmd = (
        # "pip install 'pandas==2.2.3' -q && "
        f"cd {repo_path} && "
        f"pypy3 job_scripts/evaluate.py {gen_dir} "
        f"--seed {seed} --ignore-errors --should-log "
        f"--worker-index $JOB_COMPLETION_INDEX --worker-count {num_workers}"
    )
    main = client.V1Container(
        name="eval-worker",
        image=Images.pypy3_pandas,
        command=["sh", "-c"],
        args=[cmd],
        # I have to monitor and adjust the resources
        # kubectl top pods
        resources=client.V1ResourceRequirements(
            requests={"cpu": "1", "memory": "2Gi"},
            limits={"cpu": "4", "memory": "8Gi"},
        ),
        volume_mounts=[
            client.V1VolumeMount(mount_path="/mnt", name="results"),
        ],
    )

    pod_spec = client.V1PodSpec(
        restart_policy="Never",
        security_context=client.V1SecurityContext(run_as_user=0),
        containers=[main],
        volumes=[
            client.V1Volume(
                name="results",
                persistent_volume_claim=client.V1PersistentVolumeClaimVolumeSource(claim_name=pvc_name),
            ),
        ],
    )

    return client.V1Job(
        api_version="batch/v1",
        kind="Job",
        metadata=client.V1ObjectMeta(name=job_name, namespace=namespace),
        spec=client.V1JobSpec(
            completions=num_workers,
            parallelism=num_workers,
            completion_mode="Indexed",
            template=client.V1PodTemplateSpec(spec=pod_spec),
            backoff_limit=2,
            ttl_seconds_after_finished=3600,
        ),
    )

# TODO I can run this at the start of prep job instead of running this in a separate job
def make_cleanup_job(batch_api: client.BatchV1Api, gen: int, results_dir: str, variant: str|None, log: logging.Logger,
                     build_history: bool = False, history_count: int = 0, skill: bool = False):
    gen_dir = f"{results_dir}/g{gen}"
    job_name = f"sgdl-evo-merge-g{gen}{('-' + variant) if variant else ''}"

    merge_cmd = (
        "pip install pandas -q && "
        "python3 -c \""
        "import pandas as pd, glob, os; "
        f"files = sorted(glob.glob('{gen_dir}/evaluation_worker_*.csv')); "
        "df = pd.concat([pd.read_csv(f) for f in files], ignore_index=True); "
        f"df.to_csv('{gen_dir}/evaluation.csv', index=False); "
        "print(f'Merged ' + str(len(files)) + ' files, ' + str(len(df)) + ' rows')"
        "\""
    )

    commands = [merge_cmd]
    image = Images.jupyter if build_history else Images.python

    if build_history:
        g_prev = f"{results_dir}/g{gen - 1}"
        repo_path = get_repo_path(repo_name)
        skill_flag = " --skill" if skill else ""
        history_cmd = (
            f"cd {repo_path} && python job_scripts/make_llm_history.py "
            f"{gen_dir} --ignore-non-existent --prev-dir {g_prev} --included-history {history_count}{skill_flag}"
        )
        commands.append(history_cmd)

    job = make_job(job_name, image, commands)
    submit_job(batch_api, job, log)
    return wait_for_job(batch_api, job_name, log)

def run_prep_job(gen: int, rnd: Random, results_dir: str, variant: str, population_size: int,
                 mutation_count: int, crossover_count: int, max_copied_count: int,
                 max_mutations_per_game: int, max_crossover_per_game: int, batch_api: client.BatchV1Api,
                 log: logging.Logger):
    g_prev = f"{results_dir}/g{gen - 1}"
    g_best = f"{results_dir}/g{gen - 1}-best"
    g_curr = f"{results_dir}/g{gen}"
    eval_csv = f"{g_prev}/evaluation.csv"
    repo_path = get_repo_path(repo_name)
    job_name = f"sgdl-evo-prep-g{gen}{('-' + variant) if variant else ''}"
    # run_command = "pypy3"
    run_command = "python"

    if gen == 0:
        random_seed = get_seed(rnd)
        log.info(f"Random seed: {random_seed}")
        commands = [
            f"mkdir -p {g_curr}",
            
            f"cd {repo_path} && {run_command} job_scripts/generate_random.py "
            f"{population_size} {g_curr} --seed {random_seed} --ignore-errors --index-from-existing"
        ]
    else:
        copy_best_seed = get_seed(rnd)
        mutation_seed = get_seed(rnd)
        crossover_seed = get_seed(rnd)
        random_seed = get_seed(rnd)
        log.info(f"Copy seed: {copy_best_seed} | Mutation seed: {mutation_seed} | Crossover seed: {crossover_seed} | Random seed: {random_seed}")
        commands = [
            f"mkdir -p {g_curr} {g_best}",

            f"cd {repo_path} && {run_command} job_scripts/choose_best.py "
            f"{eval_csv} {g_prev} {g_best} --ignore-non-existent --index-from-existing",

            f"{run_command} job_scripts/copy_best.py "
            f"{g_best} {max_copied_count} {g_curr} --seed {copy_best_seed} --index-from-existing",

            f"{run_command} job_scripts/generate_mutations.py "
            f"{g_best} {mutation_count} {g_curr} --seed {mutation_seed} --ignore-errors --ensure-change --index-from-existing --max-per-game {max_mutations_per_game}",

            f"{run_command} job_scripts/generate_crossover.py "
            f"{g_best} {crossover_count} {g_curr} --seed {crossover_seed} --ignore-errors --index-from-existing  --max-per-game {max_crossover_per_game}",

            # fill remainder with random games
            f"EXISTING=$(ls {g_curr}/*.sgdl 2>/dev/null | wc -l); "
            # I can't calculate this beforehand because crossover/mutation might fail because of lack of parents
            # also we don't know the number of "good" games copied from the previous generation
            f"REMAINING=$(( {population_size} - EXISTING )); "
            f"[ $REMAINING -gt 0 ] && "
            f"{run_command} job_scripts/generate_random.py "
            f"$REMAINING {g_curr} --seed {random_seed} --ignore-errors --index-from-existing"
        ]

    job = make_job(job_name, Images.jupyter, commands)
    submit_job(batch_api, job, log)
    return wait_for_job(batch_api, job_name, log)

def run_eval_job(
        gen: int, seed: int, results_dir: str, variant: str, worker_count: int, batch_api: client.BatchV1Api,
        log: logging.Logger
    ):
    gen_dir = f"{results_dir}/g{gen}"
    job_name = f"sgdl-evo-eval-g{gen}{('-' + variant) if variant else ''}"

    job = make_eval_job(job_name, seed, gen_dir, worker_count)
    submit_job(batch_api, job, log)
    return wait_for_job(batch_api, job_name, log)


def main():
    parser = argparse.ArgumentParser(description="GDL Evolution Orchestrator (runs locally)")

    parser.add_argument('--seed', type=int, default=None, help="Integer seed to be used for creating the gdls.")
    parser.add_argument("--results-dir", default=None, help="Path to results dir on the PVC (as seen from inside pods). If not given, will add timestamp and variant.")
    parser.add_argument("--start-gen", type=int, default=0, help="Starting generation index. Use 0 unless this is continuing a previous run.")
    parser.add_argument("--end-gen", type=int, default=99, help="End generation, indicating generation at which this script should stop.")
    parser.add_argument("--population-size", type=int, default=100, help="Number of games per generation (including games that are already good, mutated and crossovered games, and the rest filled with random games)")
    parser.add_argument("--mutation-count", type=int, default=20, help="How many games per generation are created using mutation (will not generate anything if there is not at least 1 good parent available)")
    parser.add_argument("--crossover-count", type=int, default=20, help="How many games per generation are created using crossover (will not generate anything if there are not at least 2 good parents available)")
    parser.add_argument("--max-copied-count", type=int, default=30, help="How many games per generation are good games copied from previous generation")
    parser.add_argument("--max-mutations-per-game", type=int, default=1, help="Maximum number of mutations per game, to avoid mutating a few games many times and diluting the next generation.")
    parser.add_argument("--max-crossover-per-game", type=int, default=1, help="Maximum number of crossover per game, to avoid mutating a few games many times and diluting the next generation.")
    parser.add_argument("--eval-workers", type=int, default=10, help="Number of workers used to parallelize evaluation process.")
    parser.add_argument("--variant", type="str", default="", help="Optional name suffix for job names (e.g. 'llm', 'llm-skil')")

    args = parser.parse_args()
    variant = args.variant
    timestamp = int(time.time())
    results_dir = args.results_dir if args.results_dir else f"/results{('-' + variant) if variant else ''}/{timestamp}"
    results_dir = f"/mnt/{results_dir}"
    start_gen = args.start_gen
    end_gen = args.end_gen
    population_size = args.population_size
    mutation_count = args.mutation_count
    crossover_count = args.crossover_count
    max_copied_count = args.max_copied_count
    worker_count = args.eval_workers
    max_mutations_per_game = args.max_mutations_per_game
    max_crossover_per_game = args.max_crossover_per_game
    expr_seed: int = args.seed if args.seed is not None else get_seed(None)
    experiment_rnd = Random(expr_seed)

    setup_logging(f"{results_dir}/orchestrator.log")
    log = get_logger(__name__)

    batch_api = get_batch_client()

    run_setup_git(batch_api, repo_url, repo_name, log)

    log.info("=" * 50)
    log.info(f"Starting evolution: gen {start_gen} to {end_gen} | Seed: {expr_seed} | Timestamp: {timestamp}")
    log.info(f"Population size {population_size} | Mutation count: {mutation_count} | Crossover count: {crossover_count} | Max copied count: {max_copied_count}")
    log.info(f"Number of evaluation workers: {worker_count}")
    log.info(f"Namespace: {namespace} | PVC: {pvc_name} | Variant: {variant}")
    log.info(f"Results at {results_dir}")
    log.info("=" * 50)

    for gen in range(args.start_gen, args.end_gen + 1):
        log.info(f"\n--- Generation {gen} ---")
        gen_seed = get_seed(experiment_rnd)
        eval_seed = get_seed(experiment_rnd)
        log.info(f"Generation Seed: {gen_seed} | Evaluation Seed: {eval_seed}")

        ok = run_prep_job(gen, Random(gen_seed), results_dir, variant, population_size,
                          mutation_count, crossover_count, max_copied_count,
                          max_mutations_per_game, max_crossover_per_game, batch_api, log)
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