# modules/core/job_manager.py
import time
import requests
import traceback
from urllib.parse import quote
from concurrent.futures import ThreadPoolExecutor
import config.env_config
from modules.core.web_engine import run_job
from modules.utils.logger_config import setup_logger

logger = setup_logger(__name__, level=config.env_config.LOG_LEVEL, log_to_file=False)

# Constants
# Single source of truth for the API location (previously hardcoded to :8080
# here and :8000 in prepopulate_jobs.py, so the worker talked to nothing).
SERVER_URL = config.env_config.REMOTE_JOB_API_URL
MAX_WORKERS = 1  # Adjust to your system capacity and desired concurrency
POLL_INTERVAL = 60  # Seconds to wait before re-checking for new jobs

def get_next_job():
    try:
        response = requests.get(f"{SERVER_URL}/next-job", timeout=10)
        response.raise_for_status()
        data = response.json()
        url = data.get('url')
        if url:
            return url
        else:
            return None
    except requests.RequestException as e:
        logger.warning(f"🔴  Error fetching next job: {e}")
        return None

def mark_job_result(url: str, success: bool):
    status = "success" if success else "failed"
    payload = {
        "url": url,
        "status": status
    }
    try:
        response = requests.post(
            f"{SERVER_URL}/job/update",  # Corrected endpoint
            json=payload,
            timeout=10
        )
        response.raise_for_status()
        logger.debug(f"✅ Marked job {url} as {status}")
    except requests.RequestException as e:
        logger.error(f"❌ Failed to mark job result for {url}: {e}")

def mark_job_waiting(url: str, question: str):
    """Record that a job stopped awaiting a human answer."""
    try:
        response = requests.post(
            f"{SERVER_URL}/job/update",
            json={"url": url, "status": "waiting_for_user", "note": question},
            timeout=10,
        )
        response.raise_for_status()
        logger.debug(f"⏸️ Marked job {url} as waiting_for_user")
    except requests.RequestException as e:
        logger.error(f"❌ Failed to mark job waiting for {url}: {e}")


def job_worker():
    while True:
        url = get_next_job()
        if not url:
            logger.debug(f"No new jobs available, sleeping for {POLL_INTERVAL}s.")
            time.sleep(POLL_INTERVAL)
            continue
        logger.info(f"Starting job for {url}")
        try:
            result = run_job(url)
            if isinstance(result, dict) and result.get("status") == "waiting_for_user":
                # Not a failure — the job is still actionable once the user
                # answers. Leave it out of the success/failed split so it is
                # not silently retried into the same dead end.
                logger.info(f"⏸️    Job paused for {url} - needs: {result['question']}")
                mark_job_waiting(url, result["question"])
                continue
            logger.info(f"🏁    Job completed for {url} - Result: {result}")
            mark_job_result(url, success=bool(result))
        except Exception as e:
            logger.critical(f"💥    Job failed for {url} - Error: {e}")
            traceback.print_exc()
            mark_job_result(url, success=False)

def start_scheduler():
    logger.info(f"🚀    Starting Job Scheduler with {MAX_WORKERS} workers...")
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        for _ in range(MAX_WORKERS):
            executor.submit(job_worker)
