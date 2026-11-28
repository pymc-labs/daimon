# postgres:18-alpine plus pgvector, for the data store's embedding search. The data store
# runs without it (full-text search only), so the default compose file keeps the plain image;
# docker-compose.data.yml builds this one.
FROM postgres:18-alpine AS build
# OPTFLAGS="": pgvector defaults to -march=native, which crashes on other CPUs (cached layers).
ARG PGVECTOR_VERSION=v0.8.1
RUN apk add --no-cache build-base git postgresql-dev \
 && git clone --depth 1 --branch "${PGVECTOR_VERSION}" https://github.com/pgvector/pgvector.git /src \
 && make -C /src with_llvm=no OPTFLAGS="" \
 && make -C /src with_llvm=no OPTFLAGS="" install DESTDIR=/out

FROM postgres:18-alpine
COPY --from=build /out/ /
