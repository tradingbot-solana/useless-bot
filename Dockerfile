# Use official slim Python 3.12 image – small and fast for Railway
FROM python:3.12-slim

# Set working directory inside the container
WORKDIR /app

# Copy requirements first → much better caching / faster rebuilds
COPY requirements.txt .

# Install all dependencies in one layer
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of your application files
COPY . .

# Run the script with unbuffered output (-u flag)
# This is very important for seeing logs in real-time on Railway
CMD ["python", "-u", "uselesss_rsi_crossoverer_agent.py"]
