# Use slim Python 3.12 base image (good for Railway)
FROM python:3.12-slim

# Set working directory
WORKDIR /app

# Copy requirements first (optimizes caching on rebuilds)
COPY requirements.txt .

# Install dependencies in one layer
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of your application code
COPY . .

# Run your script (matches the filename from your screenshot: four 's' letters)
CMD ["python", "useless_rsi_crossoverer_agent.py"]
