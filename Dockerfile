FROM python:3.11-slim-bookworm
# Pinned to Debian 12 (bookworm). Trixie removed the font packages
# Playwright's --with-deps installer asks for (ttf-ubuntu-font-family,
# ttf-unifont), causing chromium install to fail with exit 100.

WORKDIR /app

# Install dependencies
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Install Chromium for Playwright (used by services/fuel_surcharge.py to
# scrape DHL's JS-rendered surcharge page). --with-deps pulls in the
# system libraries chromium needs (apt-get under the hood).
RUN playwright install --with-deps chromium

# Copy source
COPY main.py run.sh ./
COPY cogs ./cogs
COPY services ./services
COPY dashboard ./dashboard
COPY i18n ./i18n
COPY scripts ./scripts
# Shared bot data (products master etc.) — required by cogs.shipping
COPY data ./data
# Seed default deployments scaffolding (template) so the dashboard has the .env.example.
COPY deployments ./deployments

# Default to running the dashboard. Railway can override via railway.json startCommand.
ENV PYTHONUNBUFFERED=1
EXPOSE 8000
CMD ["sh", "-c", "uvicorn dashboard.app:app --host 0.0.0.0 --port ${PORT:-8000}"]
