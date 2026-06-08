#!/usr/bin/env bash
# =============================================================================
# Bulwark — Base Image Pre-Warmer
# =============================================================================
# Pulls Python 3.12-slim and Node.js 20-slim base images and pushes them
# to GCP Artifact Registry so Cloud Build can cache the layers.
#
# This reduces Docker build time for MR simulations from 3–7 minutes
# (cold pull) to 60–90 seconds (application layer only).
#
# Usage:
#   ./scripts/prewarm_images.sh
#
# Prerequisites:
#   - gcloud CLI authenticated: gcloud auth login && gcloud auth configure-docker
#   - GCP_PROJECT_ID and GCP_REGION set in environment or .env
#
# PRD reference: §13 Day 6, §9.4 "Pre-warming base images"
# =============================================================================

set -euo pipefail

# ─── Load config ──────────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="${SCRIPT_DIR}/../.env"

if [[ -f "$ENV_FILE" ]]; then
    # Export only the GCP vars we need (skip lines with invalid shell syntax)
    while IFS= read -r line; do
        if [[ "$line" =~ ^GCP_ ]]; then
            export "${line?}"
        fi
    done < <(grep -E '^GCP_' "$ENV_FILE" | tr -d '"')
fi

GCP_PROJECT_ID="${GCP_PROJECT_ID:-}"
GCP_REGION="${GCP_REGION:-us-central1}"
GCP_ARTIFACT_REGISTRY="${GCP_ARTIFACT_REGISTRY:-bulwark-images}"

if [[ -z "$GCP_PROJECT_ID" ]]; then
    echo "❌ GCP_PROJECT_ID is not set. Export it or add it to .env" >&2
    exit 1
fi

REGISTRY="${GCP_REGION}-docker.pkg.dev/${GCP_PROJECT_ID}/${GCP_ARTIFACT_REGISTRY}"

# ─── Images to pre-warm ───────────────────────────────────────────────────────
declare -A BASE_IMAGES=(
    ["python:3.12-slim"]="python312-slim-base"
    ["node:20-slim"]="node20-slim-base"
)

# ─── Dev mode guard ───────────────────────────────────────────────────────────
BULWARK_MODE="${BULWARK_MODE:-development}"
if [[ "$BULWARK_MODE" == "development" ]]; then
    echo "ℹ️  BULWARK_MODE=development — skipping actual GCP push (dry run)"
    echo "   Images that would be pushed:"
    for source_image in "${!BASE_IMAGES[@]}"; do
        target_tag="${BASE_IMAGES[$source_image]}"
        echo "   • ${source_image} → ${REGISTRY}/${target_tag}:latest"
    done
    echo ""
    echo "   To run for real, set BULWARK_MODE=production and ensure gcloud is authenticated."
    exit 0
fi

# ─── Verify gcloud auth ───────────────────────────────────────────────────────
if ! command -v gcloud &> /dev/null; then
    echo "❌ gcloud CLI not found. Install from: https://cloud.google.com/sdk/docs/install" >&2
    exit 1
fi

if ! gcloud auth list --filter=status:ACTIVE --format="value(account)" 2>/dev/null | grep -q .; then
    echo "❌ No active gcloud auth. Run: gcloud auth login" >&2
    exit 1
fi

# Configure Docker auth for Artifact Registry
echo "🔐 Configuring Docker authentication for Artifact Registry..."
gcloud auth configure-docker "${GCP_REGION}-docker.pkg.dev" --quiet

# ─── Pull, tag, and push each base image ──────────────────────────────────────
TOTAL_START=$(date +%s)
FAILED=0

for source_image in "${!BASE_IMAGES[@]}"; do
    target_tag="${BASE_IMAGES[$source_image]}"
    target_image="${REGISTRY}/${target_tag}:latest"

    echo ""
    echo "━━━ Pre-warming: ${source_image} ━━━"
    echo "    Target: ${target_image}"

    step_start=$(date +%s)

    # Pull from Docker Hub
    echo "  📥 Pulling ${source_image}..."
    if ! docker pull "${source_image}"; then
        echo "  ❌ Pull failed for ${source_image}" >&2
        FAILED=$((FAILED + 1))
        continue
    fi

    # Tag for Artifact Registry
    echo "  🏷️  Tagging as ${target_image}..."
    docker tag "${source_image}" "${target_image}"

    # Push to Artifact Registry
    echo "  📤 Pushing to Artifact Registry..."
    if ! docker push "${target_image}"; then
        echo "  ❌ Push failed for ${target_image}" >&2
        FAILED=$((FAILED + 1))
        continue
    fi

    step_end=$(date +%s)
    step_duration=$((step_end - step_start))
    echo "  ✅ Done in ${step_duration}s"
done

# ─── Summary ──────────────────────────────────────────────────────────────────
TOTAL_END=$(date +%s)
TOTAL_DURATION=$((TOTAL_END - TOTAL_START))
TOTAL_IMAGES="${#BASE_IMAGES[@]}"
SUCCEEDED=$((TOTAL_IMAGES - FAILED))

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "Pre-warm complete: ${SUCCEEDED}/${TOTAL_IMAGES} images pushed in ${TOTAL_DURATION}s"
echo "Registry: ${REGISTRY}"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

if [[ $FAILED -gt 0 ]]; then
    echo "⚠️  ${FAILED} image(s) failed — check errors above"
    exit 1
fi
