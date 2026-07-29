FROM gcr.io/oss-fuzz-base/base-builder-python@sha256:485af05cc843949fd60d0e6e78b1a4922e661baf9af5eb595f9b96f563b42748

COPY . $SRC/platform-gitops
WORKDIR $SRC/platform-gitops
COPY .clusterfuzzlite/build.sh $SRC/
RUN chmod +x $SRC/build.sh
