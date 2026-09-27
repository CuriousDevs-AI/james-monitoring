FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY pyproject.toml README.md ./
COPY james_monitoring ./james_monitoring
RUN pip install --no-cache-dir ".[all]"
# config.yaml, .env and the team workspace are mounted at /data
WORKDIR /data
RUN git config --global user.name "james-monitoring" && git config --global user.email "jm@localhost" \
 && git config --global --add safe.directory '*'
CMD ["jm", "-c", "/data/config.yaml", "run"]
