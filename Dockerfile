FROM mcr.microsoft.com/playwright/python:v1.49.0-noble

WORKDIR /app

RUN pip install --no-cache-dir fastapi==0.115.6 uvicorn==0.34.0 pydantic==2.10.4

COPY sunarp_api.py .

EXPOSE 8000

CMD ["python", "sunarp_api.py"]
