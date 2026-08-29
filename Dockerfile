FROM python:3.12-slim

WORKDIR /app

# Install Python dependencies first for better layer caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the application source
COPY . .

# Persistent log directory (also mounted as a volume in docker-compose.yml)
RUN mkdir -p /app/logs

# -u = unbuffered so docker logs shows output in real time
CMD ["python", "-u", "bot.py"]
