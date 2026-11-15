# sERP application image. Use docker-compose.yml to run it together with PostgreSQL.
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY serp_server.py ./
COPY engine/ engine/
COPY web/ web/
ENV SERP_HOST=0.0.0.0 SERP_NO_WINDOW=1 PYTHONUNBUFFERED=1
EXPOSE 47394
CMD ["python", "serp_server.py", "--no-window"]
