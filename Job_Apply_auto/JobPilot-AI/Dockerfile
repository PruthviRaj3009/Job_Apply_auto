FROM python:3.11.9-slim-bookworm

WORKDIR /app

# OS patches first so the layer is cached independently of app code.
RUN apt-get update && apt-get upgrade -y \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# Requirements before source, so a code change does not reinstall deps.
COPY requirements-server.txt .
RUN pip install --no-cache-dir -r requirements-server.txt

COPY . .
RUN mkdir -p .job_db

# The module is modules.job_server.job_api — the old path
# (modules.core.job_api_server) does not exist and the container never started.
CMD ["uvicorn", "modules.job_server.job_api:app", "--host", "0.0.0.0", "--port", "8080"]
