FROM python:3.12-slim

WORKDIR /app

RUN pip install --no-cache-dir fastapi==0.115.6 uvicorn==0.34.0 "psycopg[binary]>=3.2,<4" pydantic==2.10.4

COPY reniec_api.py .

EXPOSE 8000

CMD ["python", "reniec_api.py"]
