ARG BASE_IMAGE=docker.io/library/python@sha256:2784af835b90070d9d37d3aa5aa01a3e825d88436b0d58f7776d2cdcb6780f9e
FROM ${BASE_IMAGE} AS toolchain

WORKDIR /inputs
COPY OpenJDK21U-jre_x64_linux_hotspot_21.0.13_9.tar.gz \
     pyyaml-6.0.3-cp313-cp313-manylinux2014_x86_64.manylinux_2_17_x86_64.manylinux_2_28_x86_64.whl ./
RUN printf '%s\n' \
    'a535a1df9c6284d9618872984e026ae0352d1f7e9e0605425c0e6114f47351dd  OpenJDK21U-jre_x64_linux_hotspot_21.0.13_9.tar.gz' \
    '0f29edc409a6392443abf94b9cf89ce99889a1dd5376d94316ae5145dfedd5d6  pyyaml-6.0.3-cp313-cp313-manylinux2014_x86_64.manylinux_2_17_x86_64.manylinux_2_28_x86_64.whl' \
    | sha256sum -c - \
    && mkdir -p /opt/java /opt/python \
    && tar -xzf OpenJDK21U-jre_x64_linux_hotspot_21.0.13_9.tar.gz \
       --strip-components=1 --no-same-owner -C /opt/java \
    && python3 -m pip install --disable-pip-version-check --no-index --no-deps \
       --no-cache-dir --target /opt/python ./*.whl

FROM ${BASE_IMAGE}
COPY --from=toolchain /opt/java /opt/java
COPY --from=toolchain /opt/python /opt/python
ENV JAVA_HOME=/opt/java PATH=/opt/java/bin:$PATH PYTHONPATH=/opt/python \
    PYTHONDONTWRITEBYTECODE=1 HOME=/tmp JAVA_TOOL_OPTIONS=-Duser.home=/tmp
USER 1000:1000
RUN java -version && python3 -c 'import yaml; assert yaml.__version__ == "6.0.3"' \
    && git --version && command -v ssh-keygen && command -v cp
