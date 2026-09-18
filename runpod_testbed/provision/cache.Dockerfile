# runpod_testbed/provision/cache.Dockerfile
# Build the binary first: make build-linux  (-> xetcache-linux-amd64 at repo root;
# `cd shim-go && go build -o ../xetcache-linux-amd64 .` lands it one level up from shim-go/)
# Then: docker build -f runpod_testbed/provision/cache.Dockerfile -t <img> .
FROM python:3.12-slim
COPY xetcache-linux-amd64 /usr/local/bin/xetcache
COPY runpod_testbed/ /app/runpod_testbed/
ENV PYTHONPATH=/app CACHE_DIR=/cache PORT=8000
RUN pip install --no-cache-dir runpod
VOLUME ["/cache"]
EXPOSE 8000
ENTRYPOINT ["python", "-m", "runpod_testbed.provision.selfconfig"]
