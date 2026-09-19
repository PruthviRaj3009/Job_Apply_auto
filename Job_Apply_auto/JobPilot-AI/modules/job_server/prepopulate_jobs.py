import os

import requests
from dotenv import load_dotenv

load_dotenv()

# Same env var the worker reads, so both ends always agree on the port.
SERVER_URL = os.getenv("REMOTE_JOB_API_URL", "http://127.0.0.1:8000")

job_urls = [
    "https://www.uber.com/global/en/careers/list/140557/",
    f"https://workday.wd5.myworkdayjobs.com/en-US/Workday/job/Israel-Tel-Aviv/Software-Engineer---HiredScore_JR-0096009-2?q=software%20engineer",
]

def load_jobs():
    response = requests.post(
        f"{SERVER_URL}/load-jobs",
        json={"urls": job_urls}
    )
    print("✅ Response:", response.status_code, response.json())

if __name__ == "__main__":
    load_jobs()
