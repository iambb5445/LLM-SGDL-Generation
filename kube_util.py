import argparse
import logging
from kubernetes import client, config
import sys
import time
from random import Random
import os

pvc_name = "sgdl-evo-results"
namespace = "design-reasoning-lab"

class Images:
    jupyter = "gitlab-registry.nrp-nautilus.io/prp/jupyter-stack/prp"
    pypy3 = "pypy:3.11-slim"
    pypy3_pandas = "gitlab-registry.nrp-nautilus.io/bbateni/pypy3-pandas-image:latest"
    python = "python:3.11-slim"

def get_seed(rnd: Random|None):
    if rnd is None:
        rnd = Random()
    return rnd.randint(0, 1000000000)

def get_logger(name):
    return logging.getLogger(name)

def setup_logging(log_path: str):
    log_path = os.path.join("./nautilus-logs", log_path)
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    
    if log_path is not None:
        os.makedirs(os.path.dirname(log_path), exist_ok=True)
        handlers.append(logging.FileHandler(log_path))
    
    logging.basicConfig(level=logging.INFO, handlers=handlers, format="%(asctime)s [%(levelname)s] %(message)s")

def get_batch_client() -> client.BatchV1Api:
    config.load_kube_config()
    return client.BatchV1Api()

def get_repo_path(repo_name) -> str:
    return f"/mnt/{repo_name}"

def run_setup_git(batch_api: client.BatchV1Api, repo_url: str, repo_name: str, log: logging.Logger):
    job_name = "sgdl-evo-pull-git"
    mnt_path = get_repo_path(repo_name)
    commands = [
        f"if [ -d {mnt_path} ]; then cd {mnt_path} && git pull; "
        f"else git clone --single-branch {repo_url} {mnt_path}; fi"
    ]
    job = make_job(job_name, "alpine/git", commands)
    submit_job(batch_api, job, log)
    return wait_for_job(batch_api, job_name, log)

def make_job(job_name: str, image: str, commands: list[str]) -> client.V1Job:
    full_command = " && ".join(commands)

    main = client.V1Container(
        name="main",
        image=image,
        command=["sh", "-c"],
        args=[full_command],
        resources=client.V1ResourceRequirements(
            requests={"cpu": "500m", "memory": "512Mi"},
            limits={"cpu": "2", "memory": "2Gi"},
        ),
        volume_mounts=[
            client.V1VolumeMount(mount_path='/mnt', name="results"),
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
            template=client.V1PodTemplateSpec(spec=pod_spec),
            backoff_limit=2, # number or retries
            ttl_seconds_after_finished=3600, # eligibale to be deleted 1 hour after it finishes execution
        ),
    )

def submit_job(batch_api: client.BatchV1Api, job: client.V1Job, log: logging.Logger):
    assert job.metadata is not None
    name: str = job.metadata.name
    # delete previous job with same name if it exists (bc of retries)
    try:
        batch_api.delete_namespaced_job(
            name=name, namespace=namespace,
            body=client.V1DeleteOptions(propagation_policy="Foreground"),
        )
        log.info(f"Deleted existing job '{name}', waiting for cleanup...")
        time.sleep(8)
    except Exception:
        pass
    batch_api.create_namespaced_job(namespace=namespace, body=job)
    log.info(f"Submitted job: {name}")


def wait_for_job(batch_api: client.BatchV1Api, job_name: str, log: logging.Logger, poll_interval: int=20):
    log.info(f"Waiting for '{job_name}'...")
    while True:
        job = batch_api.read_namespaced_job(name=job_name, namespace=namespace)
        assert isinstance(job, client.V1Job)
        spec_completions = (job.spec.completions if job.spec else None) or 1
        backoff_limit = (job.spec.backoff_limit if job.spec else None) or 0
        succeeded = (job.status.succeeded if job.status else None) or 0
        failed = (job.status.failed if job.status else None) or 0
        active = (job.status.active if job.status else None) or 0
        log.info(f"  {job_name}: active={active} succeeded={succeeded} failed={failed} target={spec_completions}")
        if succeeded >= spec_completions:
            log.info(f"  '{job_name}' complete.")
            return True
        if failed > backoff_limit:
            log.error(f"  '{job_name}' failed.")
            return False
        time.sleep(poll_interval)