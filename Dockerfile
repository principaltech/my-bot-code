FROM python:3.11-bookworm

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && \
    apt-get install -y \
        curl \
        iproute2 \
        iptables \
        ca-certificates \
        procps && \
    (timeout 20 bash -c "curl -s https://install.zerotier.com | bash" || true) && \
    pkill -9 zerotier-one || true && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN sed -i 's/\r$//' start.sh && chmod +x start.sh

CMD ["bash", "./start.sh"]
