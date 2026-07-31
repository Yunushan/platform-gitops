ARG GO_VERSION=1.26
FROM golang:${GO_VERSION}@sha256:3aff6657219a4d9c14e27fb1d8976c49c29fddb70ba835014f477e1c70636647 AS build

# build via Makefile target helm-test-image in root
# Makefile. Building from this directory will not be
# able to access source needed in rest of repo.
COPY . /src/loki
WORKDIR /src/loki
RUN make clean && make BUILD_IN_CONTAINER=false helm-test

FROM gcr.io/distroless/static:debug@sha256:e741251ccc55dd6cec4a99ff21c0766df31891fabb4a50727104619a7e6ff4f2
COPY --from=build /src/loki/production/helm/loki/src/helm-test/helm-test /usr/bin/helm-test
ENTRYPOINT [ "/usr/bin/helm-test" ]
