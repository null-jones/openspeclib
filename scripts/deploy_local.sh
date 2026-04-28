#!/usr/bin/env bash
# Build and serve the OpenSpecLib viewer locally.
#
# Mirrors what the deploy-viewer.yml GitHub Actions workflow does, but without
# pushing anywhere. Useful when:
#   - You want to test viewer changes against the real release artifacts
#     without waiting on CI.
#   - The release artifacts are too large to want to download repeatedly via
#     the standard `npm run dev` flow (this script preserves them between
#     runs and only re-downloads what's missing).
#
# Usage:
#   scripts/deploy_local.sh                  # build + serve at http://localhost:4173
#   scripts/deploy_local.sh --tag v0.0.6     # pin a specific release
#   scripts/deploy_local.sh --no-serve       # just build, don't run preview
#   scripts/deploy_local.sh --refetch        # force re-download release data
#   scripts/deploy_local.sh --skip-data      # skip release download (use existing public/data)
#
# Requirements: gh CLI, npm, node 20+.

set -euo pipefail

REPO="null-jones/openspeclib"
DEFAULT_TAG="v0.0.6"
TAG="$DEFAULT_TAG"
REFETCH=0
SKIP_DATA=0
SERVE=1

while (("$#")); do
    case "$1" in
        --tag)
            TAG="$2"
            shift 2
            ;;
        --refetch)
            REFETCH=1
            shift
            ;;
        --skip-data)
            SKIP_DATA=1
            shift
            ;;
        --no-serve)
            SERVE=0
            shift
            ;;
        -h | --help)
            sed -n '2,/^$/p' "$0" | sed 's/^# \?//'
            exit 0
            ;;
        *)
            echo "Unknown option: $1" >&2
            exit 2
            ;;
    esac
done

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VIEWER_DIR="$REPO_ROOT/viewer"
DATA_DIR="$VIEWER_DIR/public/data"
DUCKDB_DIR="$VIEWER_DIR/public/duckdb"

echo "==> Working directory: $VIEWER_DIR"
cd "$VIEWER_DIR"

if [[ ! -d node_modules ]]; then
    echo "==> Installing npm deps"
    npm ci
fi

# 1) Release data
mkdir -p "$DATA_DIR"
if [[ $SKIP_DATA -eq 1 ]]; then
    echo "==> Skipping release data download (--skip-data)"
elif [[ $REFETCH -eq 1 ]]; then
    echo "==> Force-refetching $TAG release artifacts into $DATA_DIR"
    rm -f "$DATA_DIR"/*.json "$DATA_DIR"/*.parquet "$DATA_DIR"/checksums.txt
    gh release download "$TAG" --repo "$REPO" \
        --pattern "*.json" --pattern "*.parquet" --pattern "checksums.txt" \
        --dir "$DATA_DIR"
else
    echo "==> Ensuring $TAG release artifacts in $DATA_DIR (skip-existing)"
    gh release download "$TAG" --repo "$REPO" \
        --pattern "*.json" --pattern "*.parquet" --pattern "checksums.txt" \
        --dir "$DATA_DIR" --skip-existing
fi

# 2) DuckDB-WASM workers + WASM (vite serve doesn't auto-copy these the way
#    the deploy workflow does)
mkdir -p "$DUCKDB_DIR"
for f in duckdb-mvp.wasm duckdb-browser-mvp.worker.js duckdb-eh.wasm duckdb-browser-eh.worker.js; do
    src="$VIEWER_DIR/node_modules/@duckdb/duckdb-wasm/dist/$f"
    dst="$DUCKDB_DIR/$f"
    if [[ ! -f "$dst" || "$src" -nt "$dst" ]]; then
        echo "==> Copying $f"
        cp "$src" "$dst"
    fi
done

# 3) Build
echo "==> Building viewer (vite build)"
npm run build

# 4) Optional: serve the built bundle locally
if [[ $SERVE -eq 1 ]]; then
    echo
    echo "==> Serving from $VIEWER_DIR/dist on http://localhost:4173"
    echo "    (Ctrl-C to stop)"
    npm run preview -- --host 127.0.0.1
fi
