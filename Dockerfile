# ═══════════════════════════════════════════════════════════════════════════════
# Stage 1 – Build the Kirk binary
#
# Uses the official clfoundation SBCL image.  We install Quicklisp, point ASDF
# at the full enterprise source tree (which lives alongside kirk-v2), load all
# required Quicklisp packages, then invoke (asdf:make :kirk-v2) to produce the
# self-contained "deploy-op" executable bundle at enterprise/kirk-v2/build/kirk.
# ═══════════════════════════════════════════════════════════════════════════════
FROM clfoundation/sbcl:2.2.4 AS kirk-builder

# System packages needed at build time
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        wget \
        git \
        libssl-dev \
        zlib1g-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /common-lisp

# ── Install a recent ASDF ──────────────────────────────────────────────────────
RUN wget -q https://common-lisp.net/project/asdf/archives/asdf.tar.gz \
    && tar -xf asdf.tar.gz \
    && rm asdf.tar.gz \
    # Install into SBCL's contrib area so (require :asdf) picks it up
    && cd asdf-* \
    && CL_SOURCE_REGISTRY="$(pwd)//:" \
       sbcl --non-interactive --no-userinit --no-sysinit \
            --load "tools/load-asdf.lisp" \
            --load "uiop/uiop.asd" \
            --load "tools/install-asdf.lisp" > /dev/null 2>&1 \
    && cd .. \
    && rm -rf asdf-*

# ── Configure ASDF source registry ────────────────────────────────────────────
# Points at /common-lisp so ASDF finds every .asd file in the enterprise tree.
RUN mkdir -p /root/.config/common-lisp/source-registry.conf.d \
    && printf '(:tree "/common-lisp")\n' \
       > /root/.config/common-lisp/source-registry.conf.d/30-root.conf

# ── Install Quicklisp ──────────────────────────────────────────────────────────
RUN wget -q https://beta.quicklisp.org/quicklisp.lisp \
    && sbcl --non-interactive \
            --load quicklisp.lisp \
            --eval "(quicklisp-quickstart:install)" \
            --eval "(uiop:quit)" \
    && rm quicklisp.lisp \
    # Auto-load Quicklisp in every subsequent SBCL session
    && printf '#-quicklisp\n(let ((ql (merge-pathnames "quicklisp/setup.lisp" (user-homedir-pathname))))\n  (when (probe-file ql) (load ql)))\n' \
       >> /root/.sbclrc

# ── Copy the enterprise source tree ───────────────────────────────────────────
COPY enterprise/ /common-lisp/enterprise/

# ── Pre-load Quicklisp packages ────────────────────────────────────────────────
# build-deps.lisp loads only what is needed for kirk-v2; it skips packages that
# require X11 or other heavy system deps not available in a headless builder.
COPY build-deps.lisp /common-lisp/build-deps.lisp
RUN sbcl --non-interactive \
         --eval "(load \"/root/quicklisp/setup.lisp\")" \
         --load /common-lisp/build-deps.lisp \
         --eval "(uiop:quit)"

# ── Load kirk-v2 dependencies (adopt, safe-queue, clingon) ───────────────────
WORKDIR /common-lisp/enterprise/kirk-v2
RUN sbcl --non-interactive \
         --eval "(load \"/root/quicklisp/setup.lisp\")" \
         --load /common-lisp/enterprise/dependencies.lisp \
         --eval "(uiop:quit)"

# ── Build the kirk binary ──────────────────────────────────────────────────────
RUN sbcl --non-interactive \
         --eval "(require :asdf)" \
         --eval "(declaim (sb-ext:muffle-conditions style-warning))" \
         --eval "(asdf:make :kirk-v2)" \
    && ls -lh /common-lisp/enterprise/build/kirk*


# ═══════════════════════════════════════════════════════════════════════════════
# Stage 2 – Python runtime
#
# Contains:
#   • the pykirk Python package (installed with uv)
#   • the kirk binary bundle copied from stage 1
#   • server.py – our FastAPI wrapper
#   • start.sh  – the container entrypoint
# ═══════════════════════════════════════════════════════════════════════════════
FROM python:3.13-slim AS runtime

# Runtime system packages.
# libssl / libcrypto are needed because kirk's deploy-op marks them :dont-deploy
# (they are expected to be present on the host).
# curl is needed for the NodeSource setup script.
# nodejs (>=20) is needed to run the Vite visualization dev server.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        libssl3 \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

# ── Install uv (used to run pykirk services) ──────────────────────────────────
RUN pip install --no-cache-dir uv

# ── Copy pykirk source and install it ─────────────────────────────────────────
COPY pykirk/ /app/pykirk/
WORKDIR /app/pykirk
RUN uv pip install --system -e .

# ── Pre-install visualization npm dependencies ────────────────────────────────
# The dev server is only started when ENABLE_VIS=1 at runtime, but we install
# dependencies at build time so startup is fast.
WORKDIR /app/pykirk/visualization
RUN npm install

# ── Copy the kirk binary bundle from stage 1 ──────────────────────────────────
COPY --from=kirk-builder /common-lisp/enterprise/build/ /app/kirk/

# ── Install the wrapper server's own dependencies ─────────────────────────────
RUN pip install --no-cache-dir fastapi "uvicorn[standard]" httpx websockets

# ── Copy pddl_to_sp converter ─────────────────────────────────────────────────
COPY pddl_to_sp/ /app/pddl_to_sp/

# ── Copy robust-execution monitor ────────────────────────────────────────────
COPY robust-execution/ /app/robust-execution/

# ── Copy plan visualization module ───────────────────────────────────────────
COPY plan_visualization/ /app/plan_visualization/

# ── Copy application files ────────────────────────────────────────────────────
COPY server.py /app/server.py
COPY start.sh  /app/start.sh
RUN chmod +x /app/start.sh

WORKDIR /app

# ── Generated plans output directory (bind-mount to persist on host) ─────────
RUN mkdir -p /app/generated_plans
VOLUME /app/generated_plans

# Ports: 8000 = EaaS API, 9000 = dispatcher (when oracle disabled), 8002 = telemetry (vis),
#        5173 = Vite dev server (vis), 9003 = monitor, 9004 = plan visualization
EXPOSE 8000 9000 8002 5173 9003 9004

ENV KIRK_BINARY=/app/kirk/kirk \
    PYKIRK_DIR=/app/pykirk \
    PDDL_TO_SP_DIR=/app/pddl_to_sp \
    ROBUST_EXEC_DIR=/app/robust-execution \
    KIRK_PORT=7000 \
    DISPATCHER_PORT=9000 \
    LOCAL_AGENT_PORT=9001 \
    LOCAL_ORACLE_PORT=9002 \
    MONITOR_PORT=9003 \
    PLAN_VIS_PORT=9004 \
    PLAN_VIS_DIR=/app/plan_visualization \
    ENABLE_ORACLE=1 \
    ENABLE_VIS=0 \
    TELEMETRY_PORT=8002 \
    VIS_PORT=5173 \
    VIS_WS_URL=ws://localhost:8002/ws

ENTRYPOINT ["/app/start.sh"]
