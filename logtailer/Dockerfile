FROM python:3.12-alpine

WORKDIR /app

# psycopg2 needs libpq
RUN apk add --no-cache libpq-dev gcc musl-dev

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY tailer.py .

CMD ["python", "-u", "tailer.py"]
