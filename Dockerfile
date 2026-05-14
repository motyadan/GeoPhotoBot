FROM python:3.11-slim

WORKDIR /bot

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py .
COPY admins.json .
COPY allowed_users.json .
COPY chatid.py .

CMD ["python", "main.py"]
