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
# Recreate the bin shims that `npm install -g` would have linked.
RUN ln -s /usr/local/lib/node_modules/@z_ai/coding-helper/dist/cli.js /usr/local/bin/coding-helper \
 && ln -s /usr/local/lib/node_modules/@z_ai/coding-helper/dist/cli.js /usr/local/bin/chelper \
 && chmod +x /usr/local/lib/node_modules/@z_ai/coding-helper/dist/cli.js
ENV PATH="/usr/local/bin:${PATH}"

# Our toolchain (python wheel).
COPY --from=py-build /wheels /wheels
RUN python -m pip install --no-cache-dir /wheels/zai_python_helper-*.whl \
 && rm -rf /wheels

# Smoke entrypoints for the parity test (and ad-hoc `docker run` checks).
CMD ["coding-helper", "--version"]
