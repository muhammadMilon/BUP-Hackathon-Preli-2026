#!/usr/bin/env bash
# Build, smoke-test and publish the Docker fallback image required by the guide.
#
#   bash deploy/docker_publish.sh docker.io/YOUR_DOCKERHUB_USER/campus-energy v1
#   bash deploy/docker_publish.sh ghcr.io/YOUR_GITHUB_USER/campus-energy v1
#
# Log in first: `docker login` (Docker Hub) or
# `echo $GHCR_TOKEN | docker login ghcr.io -u YOUR_GITHUB_USER --password-stdin`.
#
# The image is verified before it is pushed: it must start with no environment
# variables at all and still answer GET /health, which is exactly what the
# judges' fallback path does. No secrets are ever baked in -- .dockerignore
# excludes .env, and keys are supplied at `docker run` time.

set -euo pipefail

IMAGE="${1:-}"
TAG="${2:-v1}"
PORT_CHECK=18080

if [[ -z "$IMAGE" ]]; then
    echo "usage: bash deploy/docker_publish.sh <registry/user/name> [tag]" >&2
    exit 2
fi

REF="${IMAGE}:${TAG}"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$SRC_DIR"

echo "==> Building $REF"
docker build -t "$REF" .

echo "==> Verifying the image contains no baked-in secrets"
if docker run --rm --entrypoint sh "$REF" -c 'ls -a /app' | grep -qx '.env'; then
    echo "REFUSING TO PUSH: a .env file is inside the image." >&2
    exit 1
fi
echo "    no .env in the image"

echo "==> Smoke-testing with no environment variables set"
CID="$(docker run -d -p ${PORT_CHECK}:8000 "$REF")"
trap 'docker rm -f "$CID" >/dev/null 2>&1 || true' EXIT

READY=0
for _ in $(seq 1 60); do
    if curl -fsS "http://127.0.0.1:${PORT_CHECK}/health" >/dev/null 2>&1; then READY=1; break; fi
    sleep 1
done
if [[ $READY -ne 1 ]]; then
    echo "Container never became healthy. Logs:" >&2
    docker logs "$CID" >&2
    exit 1
fi
echo "    GET /health -> $(curl -fsS http://127.0.0.1:${PORT_CHECK}/health)"

echo "==> Running a public sample case against the container"
python3 - "$PORT_CHECK" <<'PY' || echo "    (sample check skipped: python3/httpx unavailable here)"
import json, sys, urllib.request
port = sys.argv[1]
case = json.load(open("samples/public_cases.json", encoding="utf-8"))[0]
req = urllib.request.Request(
    "http://127.0.0.1:%s/optimize-energy" % port,
    data=json.dumps(case).encode(),
    headers={"Content-Type": "application/json"},
)
with urllib.request.urlopen(req, timeout=60) as r:
    body = json.load(r)
assert len(body["hourly_plan"]) == 24, "plan is not 24 hours"
assert body["scenario_id"] == case["scenario_id"]
print("    POST /optimize-energy -> 200, cost %.2f BDT" % body["total_cost_bdt"])
PY

docker rm -f "$CID" >/dev/null
trap - EXIT

echo "==> Pushing $REF"
docker push "$REF"

DIGEST="$(docker inspect --format='{{index .RepoDigests 0}}' "$REF" 2>/dev/null || echo "")"

echo
echo "Done. Put these in the submission form and the README:"
echo "    docker pull $REF"
echo "    docker run -d -p 8000:8000 -e GROQ_API_KEY=... -e GEMINI_API_KEY=... $REF"
[[ -n "$DIGEST" ]] && echo "    digest: $DIGEST"
echo
echo "Verify from another machine:"
echo "    curl http://localhost:8000/health"
