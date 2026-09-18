# runpod_testbed/provision/cache.Dockerfile
# Build the binary first: make build-linux  (-> shim-go/xetcache-linux-amd64)
# Then: docker build -f runpod_testbed/provision/cache.Dockerfile -t <img> .
FROM python:3.12-slim
COPY shim-go/xetcache-linux-amd64 /usr/local/bin/xetcache
COPY runpod_testbed/ /app/runpod_testbed/
ENV PYTHONPATH=/app CACHE_DIR=/cache PORT=8000
RUN pip install --no-cache-dir runpod
VOLUME ["/cache"]
EXPOSE 8000
ENTRYPOINT ["python", "-m", "runpod_testbed.provision.selfconfig"]
