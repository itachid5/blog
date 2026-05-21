FROM alpine:latest

RUN apk add --no-cache sslh wget tar ca-certificates

ENV FRP_VERSION=0.57.0

RUN wget https://github.com/fatedier/frp/releases/download/v${FRP_VERSION}/frp_${FRP_VERSION}_linux_amd64.tar.gz && \
    tar xzf frp_${FRP_VERSION}_linux_amd64.tar.gz && \
    mv frp_${FRP_VERSION}_linux_amd64 /frp && \
    rm frp_${FRP_VERSION}_linux_amd64.tar.gz

COPY frps.toml /frp/frps.toml
COPY start.sh /start.sh

RUN chmod +x /start.sh

CMD ["/start.sh"]
