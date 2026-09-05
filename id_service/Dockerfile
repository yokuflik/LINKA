# --- build stage ---
FROM rust:1-slim AS builder
RUN apt-get update && apt-get install -y --no-install-recommends protobuf-compiler \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /build
COPY id_service/Cargo.toml id_service/Cargo.lock id_service/build.rs ./
COPY id_service/src ./src
COPY proto /proto
# build.rs expects the proto at ../proto relative to the crate
RUN mkdir -p /parent && mv /proto /parent/proto && mv /build /parent/id_service
WORKDIR /parent/id_service
RUN cargo build --release

# --- runtime stage ---
FROM debian:bookworm-slim
ARG GRPC_HEALTH_PROBE_VERSION=v0.4.34
ADD https://github.com/grpc-ecosystem/grpc-health-probe/releases/download/${GRPC_HEALTH_PROBE_VERSION}/grpc_health_probe-linux-amd64 /bin/grpc_health_probe
RUN chmod +x /bin/grpc_health_probe
COPY --from=builder /parent/id_service/target/release/id_service /usr/local/bin/id_service
EXPOSE 50051
# NODE_ID must be set per replica (0..=1023); the process exits if it is missing.
ENV NODE_ID=""
ENTRYPOINT ["/usr/local/bin/id_service"]
