import logging
from kubernetes import client, config
from kubernetes.client.exceptions import ApiException
from contextlib import contextmanager
import subprocess
import sys
import time
from random import Random
import os
from pathlib import Path
from typing import Callable, TypeVar
from typing import ParamSpec

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

def _get_helper_pod_name(variant: str) -> str:
    return f"sgdl-evo-pvc-helper{('-' + variant) if variant else ''}"

def setup_logging(log_path: str|None):
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    
    if log_path is not None:
        os.makedirs(os.path.dirname(log_path), exist_ok=True)
        print(f"Logging at {log_path}")
        handlers.append(logging.FileHandler(log_path))
    
    logging.basicConfig(level=logging.INFO, handlers=handlers, format="%(asctime)s [%(levelname)s] %(message)s")

def get_batch_client() -> client.BatchV1Api:
    config.load_kube_config()
    return client.BatchV1Api()

def get_core_client() -> client.CoreV1Api:
    config.load_kube_config()
    return client.CoreV1Api()

def get_repo_path(repo_name) -> str:
    return f"/mnt/{repo_name}"

P = ParamSpec("P")
R = TypeVar("R")

def _api_call_with_retry(fn: Callable[P, R], log: logging.Logger, attempts: int=7, *args: P.args, **kwargs: P.kwargs) -> R:
    delay = 2
    for attempt in range(attempts):
        try:
            return fn(*args, **kwargs)
        except ApiException as e:
            if e.status == 429:
                log.warning(f"Rate limited on '{fn.__name__}', retrying in {delay}s (attempt {attempt + 1}/{attempts})")
                time.sleep(delay)
                delay *= 2
            else:
                raise
    raise ApiException(f"Still rate limited after {attempts} attempts on '{fn.__name__}'")

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
        _api_call_with_retry(batch_api.delete_namespaced_job, log, 
            name=name, namespace=namespace,
            body=client.V1DeleteOptions(propagation_policy="Foreground"),
        )
        log.info(f"Deleted existing job '{name}', waiting for cleanup...")
        time.sleep(8)
    except ApiException as e:
        if e.status != 404:
            raise
    _api_call_with_retry(batch_api.create_namespaced_job, log, namespace=namespace, body=job)
    log.info(f"Submitted job: {name}")

def wait_for_job(batch_api: client.BatchV1Api, job_name: str, log: logging.Logger, poll_interval: int=20):
    log.info(f"Waiting for '{job_name}'...")
    while True:
        job = _api_call_with_retry(batch_api.read_namespaced_job, log, name=job_name, namespace=namespace)
        assert isinstance(job, client.V1Job)
        spec_completions = (job.spec.completions if job.spec else None) or 1
        backoff_limit = (job.spec.backoff_limit if job.spec else None) or 0
        succeeded = (job.status.succeeded if job.status else None) or 0
        failed = (job.status.failed if job.status else None) or 0
        active = (job.status.active if job.status else None) or 0
        log.info(f"  {job_name}: active={active} succeeded={succeeded} failed={failed} target={spec_completions}")
        if succeeded >= spec_completions:
            log.info(f"  '{job_name}' complete.")
            log_job_output(job_name, log)
            return True
        if failed > backoff_limit:
            log.error(f"  '{job_name}' failed.")
            log_job_output(job_name, log)
            return False
        time.sleep(poll_interval)

def _make_pvc_helper_pod(variant: str) -> client.V1Pod:
    container = client.V1Container(
        name="pvc-helper",
        image="alpine",
        command=["sh"],
        stdin=True,
        tty=True,
        volume_mounts=[client.V1VolumeMount(mount_path="/mnt", name="results")],
    )
    pod_spec = client.V1PodSpec(
        restart_policy="Never",
        security_context=client.V1SecurityContext(run_as_user=0),
        containers=[container],
        volumes=[
            client.V1Volume(
                name="results",
                persistent_volume_claim=client.V1PersistentVolumeClaimVolumeSource(claim_name=pvc_name),
            ),
        ],
    )
    return client.V1Pod(
        api_version="v1",
        kind="Pod",
        metadata=client.V1ObjectMeta(name=_get_helper_pod_name(variant), namespace=namespace),
        spec=pod_spec,
    )

def _get_pvc_helper_phase(variant: str, core_api: client.CoreV1Api, log: logging.Logger) -> str|None:
    pod = _api_call_with_retry(core_api.read_namespaced_pod, log, name=_get_helper_pod_name(variant), namespace=namespace)
    assert isinstance(pod, client.V1Pod)
    phase = pod.status.phase if pod.status else None
    return phase

def ensure_pvc_helper(variant: str, core_api: client.CoreV1Api, log: logging.Logger, timeout: int = 120):
    try:
        phase = _get_pvc_helper_phase(variant, core_api, log)
        if phase == "Running":
            return
        log.info(f"{_get_helper_pod_name(variant)} exists but is {phase}. Recreating.")
        _api_call_with_retry(core_api.delete_namespaced_pod, log, name=_get_helper_pod_name(variant), namespace=namespace)
        time.sleep(3)
    except ApiException as e:
        if e.status != 404: # 404 is when pod doesn't exist
            raise

    log.info(f"Creating PVC helper pod {_get_helper_pod_name(variant)}")
    _api_call_with_retry(core_api.create_namespaced_pod, log, namespace=namespace, body=_make_pvc_helper_pod(variant))

    start_time = time.time()
    while True:
        phase = _get_pvc_helper_phase(variant, core_api, log)
        if phase == "Running":
            log.info(f"{_get_helper_pod_name(variant)} is running.")
            return
        if time.time() - start_time >= timeout:
            raise TimeoutError(f"Timed out waiting for {_get_helper_pod_name(variant)} to start (phase={phase})")
        time.sleep(3)

def teardown_pvc_helper(variant: str, core_api: client.CoreV1Api, log: logging.Logger, timeout: int = 30):
    try:
        _api_call_with_retry(core_api.delete_namespaced_pod, log, name=_get_helper_pod_name(variant), namespace=namespace, grace_period_seconds=0)
        log.info(f"Deleted PVC helper pod {_get_helper_pod_name(variant)}.")
    except ApiException as e:
        if e.status != 404:
            raise
        return

    start_time = time.time()
    while time.time() - start_time < timeout:
        try:
            _api_call_with_retry(core_api.read_namespaced_pod, log, name=_get_helper_pod_name(variant), namespace=namespace)
        except ApiException as e:
            if e.status == 404:
                return
            raise
        time.sleep(1)
    log.warning(f"{_get_helper_pod_name(variant)} didn't fully terminate within {timeout}s.")

# I can use it with: with pvc_transfer_session(core_api, log):
@contextmanager
def pvc_transfer_session(variant: str, core_api: client.CoreV1Api, log: logging.Logger):
    ensure_pvc_helper(variant, core_api, log)
    try:
        yield
    finally:
        teardown_pvc_helper(variant, core_api, log)

def helper_exec(variant: str, cmd: str, log: logging.Logger) -> str:
    full_cmd = ["kubectl", "exec", "-n", namespace, _get_helper_pod_name(variant), "--", "sh", "-c", cmd]
    result = subprocess.run(full_cmd, capture_output=True, text=True)
    if result.returncode != 0:
        log.error(f"helper_exec failed ({cmd}):\n{result.stderr}")
        raise RuntimeError(f"helper_exec failed: {result.stderr.strip()}")
    return result.stdout

def copy_from_pvc(remote_path: str, local_path: str, variant: str, log: logging.Logger):
    local_path = Path(local_path).as_posix()
    parent = os.path.dirname(local_path.rstrip("/")) or "."
    os.makedirs(parent, exist_ok=True)
    # contrary to copy_to_pvc, this doesn't need /.
    cmd = ["kubectl", "cp", "-n", namespace, f"{_get_helper_pod_name(variant)}:{remote_path}", local_path]
    log.info(f"kubectl cp {remote_path} (pvc) -> {local_path} (local)")
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        log.error(f"copy_from_pvc failed:\n{result.stderr}")
        raise RuntimeError(f"copy_from_pvc failed: {result.stderr.strip()}")

def copy_to_pvc(local_path: str, remote_path: str, variant: str, log: logging.Logger):
    local_path = Path(local_path).as_posix()
    helper_exec(variant, f"mkdir -p {remote_path}", log)
    local_path = f"{local_path}/." # otherwise this will also copy the folder itself
    cmd = ["kubectl", "cp", "-n", namespace, local_path, f"{_get_helper_pod_name(variant)}:{remote_path}"]
    log.info(f"kubectl cp {local_path} (local) -> {remote_path} (pvc)")
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        log.error(f"copy_to_pvc failed:\n{result.stderr}")
        raise RuntimeError(f"copy_to_pvc failed: {result.stderr.strip()}")
    
def log_job_output(job_name: str, log: logging.Logger):
    result = subprocess.run(
        ["kubectl", "logs", "-n", namespace, f"job/{job_name}"],
        capture_output=True, text=True
    )
    for line in result.stdout.splitlines():
        log.info(f"  [{job_name}] {line}")