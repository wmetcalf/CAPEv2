FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        libnetfilter-queue-dev \
        libnfnetlink-dev \
        iptables \
        net-tools \
        iproute2 \
        git \
        openssl \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir \
    https://github.com/mandiant/flare-fakenet-ng/zipball/master

# Patch upstream bugs for DivertTraffic=No (listen-only) mode
COPY patch_fakenet.py /tmp/patch_fakenet.py
RUN python /tmp/patch_fakenet.py && rm /tmp/patch_fakenet.py

# Copy our listen-only config
COPY fakenet.ini /opt/fakenet/fakenet.ini

# FakeNet stores default files (served by HTTP/FTP/TFTP) alongside the package
RUN FAKENET_DIR=$(python -c "import fakenet; import os; print(os.path.dirname(fakenet.__file__))") \
    && ln -s "$FAKENET_DIR" /opt/fakenet/pkg

# SMTPListener uses bare relative paths for SSL certs — symlink them into workdir
RUN FAKENET_DIR=$(readlink /opt/fakenet/pkg) \
    && ln -sf "$FAKENET_DIR/listeners/ssl_utils/privkey.pem" "$FAKENET_DIR/privkey.pem" \
    && ln -sf "$FAKENET_DIR/listeners/ssl_utils/server.pem" "$FAKENET_DIR/server.pem"

WORKDIR /opt/fakenet/pkg

ENTRYPOINT ["fakenet", "-c", "/opt/fakenet/fakenet.ini", "-l", "/dev/stderr"]
