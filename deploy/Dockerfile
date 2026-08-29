# Minimal container for the shim. Build from the REPO ROOT so shim-go/ is in
# context:
#   docker build -f deploy/Dockerfile -t xet-dc-cache .
#
# distroless/static:nonroot gives us CA certs (needed for the HTTPS calls to
# huggingface.co / the CAS + CDN) and a non-root uid, with nothing else.

FROM golang:1.27 AS build
WORKDIR /src
# TARGETARCH is auto-populated by BuildKit to the build's target arch, so the
# image builds natively (e.g. arm64 on Apple Silicon). Defaults to amd64 to
# preserve the original behavior when built without BuildKit arg population.
ARG TARGETARCH
# Cache module downloads separately from source changes.
COPY shim-go/go.mod shim-go/go.sum* ./shim-go/
RUN cd shim-go && go mod download
COPY shim-go/ ./shim-go/
RUN cd shim-go && CGO_ENABLED=0 GOOS=linux GOARCH=${TARGETARCH:-amd64} \
    go build -trimpath -o /xetcache .

FROM gcr.io/distroless/static-debian12:nonroot
COPY --from=build /xetcache /usr/local/bin/xetcache
# Cache dir; mount a volume here in production (VOLUME keeps it writable as nonroot).
ENV CACHE_DIR=/cache
VOLUME ["/cache"]
EXPOSE 8000
ENTRYPOINT ["/usr/local/bin/xetcache"]
