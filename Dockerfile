FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY server/ server/
COPY web/ web/

EXPOSE 8000/tcp
EXPOSE 9999/udp

CMD ["python", "-m", "uvicorn", "main:app", "--app-dir", "server", "--host", "0.0.0.0", "--port", "8000"]
