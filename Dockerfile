FROM python:3.12-slim

WORKDIR /app

RUN pip install --no-cache-dir \
    pandas \
    requests \
    python-dotenv \
    solana \
    solders

COPY useless_rsi_crossover_agent.py .

CMD ["python", "uselesss_rsi_crossover_agent.py"]
