FROM mcr.microsoft.com/playwright/python:v1.49.0-noble

WORKDIR /app

RUN pip install --no-cache-dir fastapi==0.115.6 uvicorn==0.34.0 pydantic==2.10.4 playwright==1.49.0

COPY sunarp_api.py .

RUN apt-get update && apt-get install -y --no-install-recommends xvfb && rm -rf /var/lib/apt/lists/*

EXPOSE 8000

CMD ["xvfb-run", "-a", "--server-args=-screen 0 1366x768x24", "python", "sunarp_api.py"]
