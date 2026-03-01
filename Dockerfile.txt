FROM python:3.12-slim

# Install Node and MoonPay CLI
RUN apt-get update && apt-get install -y nodejs npm \
    && npm install -g @moonpay/cli

WORKDIR /app

COPY . .

RUN pip install --no-cache-dir -r requirements.txt

CMD ["python", "useless_rsi_crossover_agent.py"]
