# syntax=docker/dockerfile:1
#
# Rust WebSocket gateway (ADR 0033). Static musl binary -> distroless/static,
# ~a few MB. **Never built on the production host** (lto=fat + codegen-units=1
# needs >1 GB to link and OOMs the t3.micro): build on the dev machine with
#   docker buildx build --platform linux/amd64 \
#     -f deploy/ws_gateway.Dockerfile -t <registry>/linka-ws-gateway:<tag> --push .
# and the server only `docker compose pull`s. See deploy/README.md.

# ---- build stage --------------------------------------------------------
FROM rust:1-slim AS builder

# musl-tools provides musl-gcc; needed if any transitive crate has a build.rs
# that shells out to a C compiler. The gateway itself is pure Rust (no TLS,
# no ring) so this is mostly insurance.
RUN apt-get update \
    && apt-get install -y --no-install-recommends musl-tools \
    && rm -rf /var/lib/apt/lists/*
RUN rustup target add x86_64-unknown-linux-musl

WORKDIR /build
# The whole Cargo workspace: ws_gateway + linka-common are compiled; id_service
# is a workspace member so its manifest must resolve (its sources are not
# compiled by `-p ws_gateway`, but Cargo still reads the member).
COPY Cargo.toml Cargo.lock rust-toolchain.toml ./
COPY crates ./crates
COPY id_service ./id_service
COPY proto ./proto

# BuildKit cache mounts keep the registry index + compiled deps between builds.
RUN --mount=type=cache,target=/usr/local/cargo/registry \
    --mount=type=cache,target=/build/target \
    cargo build --release -p ws_gateway --target x86_64-unknown-linux-musl \
    && cp target/x86_64-unknown-linux-musl/release/ws_gateway /ws_gateway

# ---- runtime stage ----------------------------------------------------
FROM gcr.io/distroless/static-debian12:nonroot
COPY --from=builder /ws_gateway /ws_gateway
EXPOSE 8081
# No shell in the image -> the binary self-probes /healthz.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["/ws_gateway", "healthcheck"]
ENTRYPOINT ["/ws_gateway"]
