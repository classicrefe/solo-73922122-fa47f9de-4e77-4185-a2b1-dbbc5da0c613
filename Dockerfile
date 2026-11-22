FROM python:3.12-slim

WORKDIR /srv/app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

ENV DB_PATH=/data/registry.db \
    DEMO_SEED=1

EXPOSE 8000

# 单进程单写者：SQLite + 进程内写锁保证批次/名录/证据事务一致
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
