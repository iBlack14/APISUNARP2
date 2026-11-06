FROM mcr.microsoft.com/playwright/python:v1.49.0-noble

WORKDIR /app

ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1
ENV CHROME_PATH=/usr/bin/google-chrome
ENV CHROME_PROFILE=/tmp/chrome-profile

# Chrome real + xvfb
RUN apt-get update && apt-get install -y --no-install-recommends \
    wget xvfb x11-utils \
    && wget -q -O /tmp/chrome.deb https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb \
    && apt-get install -y --no-install-recommends /tmp/chrome.deb \
    && rm /tmp/chrome.deb \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY api.py .

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health/db', timeout=3)"]

CMD ["xvfb-run", "-a", "--server-args=-screen 0 1366x768x24", "python", "-m", "uvicorn", "api:app", "--host", "0.0.0.0", "--port", "8000", "--log-level", "info", "--access-log"]
