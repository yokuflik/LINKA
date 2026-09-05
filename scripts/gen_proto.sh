#!/usr/bin/env bash
# Regenerate the Python gRPC stubs for the Rust ID service (ADR 0011) from
# proto/snowflake.proto into utils/. The stubs are checked in so the app runs
# with no build step; re-run this only when the .proto changes.
#
# Usage:  ./scripts/gen_proto.sh          (from the repo root)
# Needs:  pip install grpcio-tools        (already in requirements.txt)
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

python3 -m grpc_tools.protoc \
  --proto_path=proto \
  --python_out=utils \
  --grpc_python_out=utils \
  proto/snowflake.proto

# grpc_tools emits `import snowflake_pb2` (flat); rewrite to a package-relative
# import so `from utils import snowflake_pb2_grpc` works.
if [[ "$(uname)" == "Darwin" ]]; then
  sed -i '' 's/^import snowflake_pb2/from utils import snowflake_pb2/' utils/snowflake_pb2_grpc.py
else
  sed -i 's/^import snowflake_pb2/from utils import snowflake_pb2/' utils/snowflake_pb2_grpc.py
fi

echo "Generated utils/snowflake_pb2.py and utils/snowflake_pb2_grpc.py"
