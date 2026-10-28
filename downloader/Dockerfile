FROM python:3.12-slim-bookworm@sha256:6a0e06f5ba0b373d051aabd3fffb9b873f27f9a2a3a14bcbe37dee6f0e3bbc70
RUN mkdir -p /app /models /home/app && chown 10001:10001 /models /home/app
WORKDIR /app
COPY download.py model-manifest.json /app/
ENV HOME=/home/app PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
USER 10001:10001
ENTRYPOINT ["python", "/app/download.py"]
