# Deps-only base image for the NHA API + tick pods. It bakes ONLY the pinned Python
# dependencies so pods start INSTANTLY (no more pip-install-on-boot, which made every
# rollout ~90s/pod and dropped capacity mid-deploy — the pain point during the 01.09
# spectator-spike incident). The application CODE is deliberately NOT copied in: it still
# comes from the nha-server-code / nha-engine-code ConfigMaps (projected into /app at
# runtime), so ordinary code deploys stay config-driven and fast, and this image only ever
# rebuilds when the dependency set below changes.
#
# All five deps ship manylinux wheels (incl. pydantic-core and psycopg2-binary), so no
# compiler/build stage is needed on slim.
FROM python:3.12-slim

# Keep in lockstep with requirements.txt and the (now-removed) inline pip line in
# deploy/server.yaml + deploy/server-tick.yaml.
RUN pip install --no-cache-dir \
        fastapi==0.141.1 \
        "uvicorn[standard]==0.52.4" \
        psycopg2-binary==2.9.12 \
        pydantic==2.13.4 \
        markdown==3.10.2 \
    && python -c "import fastapi, uvicorn, psycopg2, pydantic, markdown; print('nha deps baked')"

WORKDIR /app
