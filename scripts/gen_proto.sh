#!/usr/bin/env bash
# Regenerate the Python gRPC stubs for the Rust ID service (ADR 0011) from
# proto/snowflake.proto into infra/ids/_generated/. The stubs are checked in so
# the app runs with no build step; re-run this only when the .proto changes.
#
# Usage:  ./scripts/gen_proto.sh          (from the repo root)
# Needs:  pip install grpcio-tools        (already in requirements.txt)
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

OUT=infra/ids/_generated
mkdir -p "$OUT"

python3 -m grpc_tools.protoc \
  --proto_path=proto \
  --python_out="$OUT" \
  --grpc_python_out="$OUT" \
  proto/snowflake.proto

# grpc_tools emits `import snowflake_pb2` (flat); rewrite to a package-relative
# import so `from infra.ids._generated import snowflake_pb2_grpc` works.
if [[ "$(uname)" == "Darwin" ]]; then
  sed -i '' 's/^import snowflake_pb2/from infra.ids._generated import snowflake_pb2/' "$OUT/snowflake_pb2_grpc.py"
else
  sed -i 's/^import snowflake_pb2/from infra.ids._generated import snowflake_pb2/' "$OUT/snowflake_pb2_grpc.py"
fi

echo "Generated $OUT/snowflake_pb2.py and $OUT/snowflake_pb2_grpc.py"
