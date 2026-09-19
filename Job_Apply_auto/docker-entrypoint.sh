#!/usr/bin/env bash
# Container entrypoint.
#
# Runs migrations before starting, so a fresh volume or an upgraded image
# both arrive at the right schema. Alembic is idempotent — on an up-to-date
# database this is a no-op — so it is safe to run on every container start.
set -euo pipefail

echo "==> Applying database migrations"
alembic upgrade head

echo "==> Starting: $*"
exec "$@"
