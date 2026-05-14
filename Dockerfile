# Используем официальный образ Python
FROM python:3.9-slim

# Устанавливаем рабочую директорию
WORKDIR /bot

# Копируем зависимости и исходный код
COPY requirements.txt .
COPY main.py .
COPY allowed_users.json .
COPY admins.json .
COPY chatid.py .

# Устанавливаем зависимости
RUN pip install --no-cache-dir -r requirements.txt

# Создаем директорию для отчетов
RUN mkdir -p /bot/reports

# Запускаем бота
CMD ["python", "main.py"]