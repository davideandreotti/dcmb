FROM debian:bookworm-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

ARG MIDDLEBOX_BINARY=middlebox
COPY DC/Middlebox/${MIDDLEBOX_BINARY} /app/middlebox
COPY DC/Middlebox/schemas /app/schemas
RUN chmod +x /app/middlebox

# Preserve the path expected by the current binary.
RUN mkdir -p /home/bonsai/dcmb/certs_external
COPY certs_external/ /home/bonsai/dcmb/certs_external/

# Runtime defaults can be overridden when the container is started.
ENV OPERATOR_MODE=warm
ENV OPERATOR_ID=operator_1
ENV OPERATOR_DEFAULT_SNI=

ENTRYPOINT ["/app/middlebox", "-log_level", "debug", "-minimal_logs=false"]
