FROM docker:28-cli AS docker-cli

FROM python:3.13-slim-bookworm AS broker
WORKDIR /opt/report-sandbox
COPY --from=docker-cli /usr/local/bin/docker /usr/local/bin/docker
COPY app/services/report_sandbox.py ./app/services/
COPY analysis/broker.py ./
ENTRYPOINT ["python", "-B", "-u", "/opt/report-sandbox/broker.py"]

FROM python:3.13-slim-bookworm AS runner
ARG EA_PYPI_INDEX_URL=https://pypi.org/simple
COPY analysis/requirements.txt /tmp/report-requirements.txt
RUN python -m pip install --no-cache-dir --only-binary=:all: --no-deps --index-url "$EA_PYPI_INDEX_URL" \
    -r /tmp/report-requirements.txt \
    && python -m pip check \
    && rm /tmp/report-requirements.txt
WORKDIR /opt/report-sandbox
COPY analysis/bootstrap.py analysis/execute.py ./
ENTRYPOINT ["python", "-I", "-B", "-u", "/opt/report-sandbox/bootstrap.py"]
