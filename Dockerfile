FROM python:3.11-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Copy source (deployments/ is mounted as volume per-server)
COPY main.py run.sh ./
COPY cogs ./cogs
COPY services ./services
COPY i18n ./i18n
COPY data ./data
COPY scripts ./scripts

# /data is the per-deployment mount point
WORKDIR /data
CMD ["python3", "/app/main.py", "--env-dir", "/data"]
