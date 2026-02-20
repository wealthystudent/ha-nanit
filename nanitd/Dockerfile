ARG BUILD_FROM=ghcr.io/home-assistant/amd64-base:3.21
FROM golang:1.24-alpine AS builder

RUN apk add --no-cache git

WORKDIR /src
COPY src/go.mod src/go.sum ./
RUN go mod download

COPY src/ .
RUN CGO_ENABLED=0 GOOS=linux go build -ldflags="-s -w" -o /nanitd ./cmd/nanitd

FROM ${BUILD_FROM}

RUN apk add --no-cache ca-certificates tzdata ffmpeg

COPY --from=builder /nanitd /usr/bin/nanitd
COPY run.sh /

RUN chmod a+x /run.sh
RUN mkdir -p /data

CMD [ "/run.sh" ]
