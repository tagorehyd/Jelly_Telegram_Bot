FROM python:3.11-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

RUN pip install --no-cache-dir requests

COPY jelly_admin_with_upi.py /app/jelly_admin_with_upi.py
COPY bot /app/bot
COPY config/strings.json /app/config/strings.json

CMD ["python", "jelly_admin_with_upi.py"]
