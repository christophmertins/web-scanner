FROM debian:bookworm-slim

ENV DEBIAN_FRONTEND=noninteractive \
    LANG=C.UTF-8 \
    TZ=Europe/Berlin

RUN apt-get update && apt-get install -y --no-install-recommends \
    sane-utils \
    sane-airscan \
    hplip \
    avahi-daemon \
    libnss-mdns \
    poppler-utils \
    python3 \
    python3-flask \
    python3-pil \
    python3-requests \
    curl \
    && rm -rf /var/lib/apt/lists/* \
    && mkdir -p /scans /tmp/scannerjobs

WORKDIR /app
COPY app/ /app/app/
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 8000
VOLUME /scans

ENTRYPOINT ["/entrypoint.sh"]