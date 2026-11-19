FROM golang:1.26 AS build
WORKDIR /src
COPY go.mod go.sum ./
RUN go mod download
COPY *.go ./
RUN CGO_ENABLED=0 go build -o /processor .

# Debian slim instead of distroless: the extractor runs a local Stockfish for
# the engine features (centipawn loss, evaluation, win chance, mate).
FROM debian:trixie-slim
RUN apt-get update \
    && apt-get install -y --no-install-recommends stockfish \
    && rm -rf /var/lib/apt/lists/*
ENV STOCKFISH_PATH=/usr/games/stockfish
COPY --from=build /processor /processor
ENTRYPOINT ["/processor"]
