FROM python:3.11-alpine

LABEL maintainer="devops@anb.com.sa"
LABEL description="Vendrop - browser upload tool for vendor archives, constrained to admin-configured Nexus raw paths"
LABEL version="1.0.0"

WORKDIR /app

# ca-certificates so TLS to Nexus works through the corporate proxy.
RUN apk add --no-cache ca-certificates

COPY requirements.txt .
RUN pip install --no-cache-dir --timeout 120 -r requirements.txt

COPY app.py .
COPY templates/ ./templates/

# OCP runs containers with a random UID and GID 0. Make /app + /data group-0 readable
# so the random UID can actually exec the app and write the audit log / projects file.
RUN mkdir -p /data && \
    chgrp -R 0 /app /data && \
    chmod -R g=u /app /data

EXPOSE 8080

# Configuration via environment variables.
# NEXUS_RAW_BASE is required at runtime — no default. App refuses to start without it.
ENV NEXUS_USER=admin \
    NEXUS_PASS= \
    NEXUS_VERIFY_TLS=true \
    ADMIN_USER=admin \
    ADMIN_PASS= \
    SECRET_KEY= \
    DATA_DIR=/data \
    DEFAULT_MAX_MB=500 \
    PORT=8080

VOLUME ["/data"]

CMD ["python", "-u", "app.py"]
