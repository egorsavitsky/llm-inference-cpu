# Pin CPU wheels and native libraries together.
FROM vllm/vllm-openai-cpu@sha256:ad2d073b2ff1b755e0d0b9e33a581bfcbadde22c13a073af1dfff1d553119d38
USER root
WORKDIR /app
COPY requirements.txt /app/requirements.txt
COPY scripts/check_environment.py /app/scripts/check_environment.py
# Use installed wheels; fail if any pin is missing or differs from the lock.
RUN python -m pip install --no-index --no-deps -r /app/requirements.txt \
    && python -m pip check \
    && python /app/scripts/check_environment.py \
    && mkdir -p /models /home/app \
    && chown 10001:10001 /models /home/app
COPY service /app/service
COPY config /app/config
ENV HOME=/home/app \
    PYTHONPATH=/app \
    PYTHONUNBUFFERED=1 \
    HF_HOME=/tmp/hf-cache \
    XDG_CACHE_HOME=/tmp/cache \
    VLLM_CACHE_ROOT=/tmp/vllm-cache \
    HF_HUB_DISABLE_TELEMETRY=1 \
    HF_HUB_DISABLE_PROGRESS_BARS=1 \
    HF_HUB_OFFLINE=1 \
    TRANSFORMERS_OFFLINE=1 \
    VLLM_NO_USAGE_STATS=1 \
    DO_NOT_TRACK=1 \
    OMP_NUM_THREADS=2 \
    VLLM_CPU_OMP_THREADS_BIND=nobind \
    VLLM_CPU_KVCACHE_SPACE=1
USER 10001:10001
EXPOSE 8000
ENTRYPOINT ["python", "-m", "service.launch"]
