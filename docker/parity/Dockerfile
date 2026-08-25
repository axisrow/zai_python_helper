# syntax=docker/dockerfile:1
#
# Parity smoke image — one image with BOTH tools so a test can diff their CLI
# surface against the upstream `@z_ai/coding-helper` (issue #17).
#
# Two stages keep the toolchains isolated and the final image slim:
#   - node-stage  : installs the upstream tool via npm (pre-fetches the tarball)
#   - py-stage    : builds our wheel from THIS repo and installs it
# The final runtime image copies both installed toolchains in.
#
# Why pre-fetch the npm tarball in the node-stage and copy the binary in:
# the parity test must run `coding-helper --version` offline-fast, and we want
# the installed upstream tool pinned to a single version (see ORIG_VERSION).

ARG PY_VERSION="3.12-slim"
ARG NODE_VERSION="22-slim"

# ----------------------------------------------------------------------------
# Stage 1: install the upstream @z_ai/coding-helper via npm.
# ----------------------------------------------------------------------------
FROM node:${NODE_VERSION} AS node-stage
ARG ORIG_VERSION="0.0.7"
# Pin the upstream version so parity is reproducible. Bump ORIG_VERSION to track
# a newer upstream release; the version-format test normalizes the number away,
# so a bump must NOT change the FORMAT asserted in tests/parity.
RUN npm install -g @z_ai/coding-helper@${ORIG_VERSION}

# ----------------------------------------------------------------------------
# Stage 2: build our wheel from this repo.
# ----------------------------------------------------------------------------
FROM python:${PY_VERSION} AS py-build
WORKDIR /build
# Copy only what's needed to build the wheel (src-layout).
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN python -m pip install --upgrade pip build \
 && python -m build --wheel --outdir /wheels

# ----------------------------------------------------------------------------
# Stage 3: slim runtime with BOTH toolchains.
# ----------------------------------------------------------------------------
FROM python:${PY_VERSION} AS runtime
# Upstream toolchain (node + the globally installed coding-helper).
COPY --from=node-stage /usr/local/lib/node_modules /usr/local/lib/node_modules
COPY --from=node-stage /usr/local/bin/node /usr/local/bin/node
# Recreate the bin shims npm `install -g` linked. We can't COPY the originals
# from node-stage because docker COPY dereferences symlinks: the shim is a
# relative symlink into ../lib/node_modules, so COPY would flatten cli.js into
# /usr/local/bin, breaking its own ./lib/command.js relative import. A fresh
# symlink (the same relative path npm used) keeps the import resolving.
RUN ln -s /usr/local/lib/node_modules/@z_ai/coding-helper/dist/cli.js /usr/local/bin/coding-helper \
 && ln -s /usr/local/lib/node_modules/@z_ai/coding-helper/dist/cli.js /usr/local/bin/chelper \
 && chmod +x /usr/local/lib/node_modules/@z_ai/coding-helper/dist/cli.js
ENV PATH="/usr/local/bin:${PATH}"

# Our toolchain (python wheel).
COPY --from=py-build /wheels /wheels
RUN python -m pip install --no-cache-dir /wheels/zai_python_helper-*.whl \
 && rm -rf /wheels

# Parity-test fixtures (issue #17). These let the upstream's headless auth path
# run fully offline inside `docker run`:
#   - claude-shim: a no-op `claude` on PATH so `chelper auth reload claude` sees
#     the tool as present (we don't ship the real Claude Code package here).
#   - fetch-mock.cjs: NODE_OPTIONS=--require this to short-circuit the upstream's
#     only network call (GET api.z.ai/api/coding/paas/v4/models) -> 200 [] so a
#     fake key validates without touching the real Z.ai endpoint.
# Both COPYs must land in the same commit as the fixtures themselves, else this
# build step fails fast (desired — no silent parity skip).
COPY docker/parity/fixtures/claude-shim /usr/local/bin/claude
RUN chmod +x /usr/local/bin/claude
COPY tests/parity/fixtures/fetch-mock.cjs /opt/parity/fetch-mock.cjs
COPY docker/parity/upstream-runner.mjs /opt/parity/upstream-runner.mjs

# Smoke entrypoints for the parity test (and ad-hoc `docker run` checks).
CMD ["coding-helper", "--version"]
