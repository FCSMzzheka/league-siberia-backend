# Устанавливаем официальный образ Python
FROM python:3.10-slim

# Указываем рабочую папку внутри сервера
WORKDIR /app

# Копируем список библиотек и устанавливаем их
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Копируем основной код бота и базу данных
COPY main.py .

# Указываем команду для непрерывного старта бота
CMD ["python", "main.py"]