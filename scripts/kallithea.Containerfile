FROM docker.io/library/python@sha256:815dd89cd46d15d8677cb9ab1101450d5af2a103c2735577ade547b1735416ab AS build
COPY kallithea-976a1e77932e.tar.gz kallithea-requirements.txt /tmp/
RUN echo '307e9609a083b01188ff6d15a8b9d6b6a31bdb585f712473902258ef7c71377c  /tmp/kallithea-976a1e77932e.tar.gz' | sha256sum -c - \
    && apt-get update && apt-get install -qy --no-install-recommends nodejs npm \
    && python -m venv /opt/kallithea \
    && /opt/kallithea/bin/pip install --disable-pip-version-check 'pip==24.0' 'setuptools==66.1.1' 'wheel==0.45.1' \
    && /opt/kallithea/bin/pip install --disable-pip-version-check --no-build-isolation --no-deps \
       -r /tmp/kallithea-requirements.txt /tmp/kallithea-976a1e77932e.tar.gz \
    && /opt/kallithea/bin/pip check
ENV PATH=/opt/kallithea/bin:$PATH
RUN npm --prefix /opt/kallithea/lib/python3.11/site-packages/kallithea/front-end ci --ignore-scripts --no-audit --no-fund \
    && kallithea-cli front-end-build --no-install-deps \
    && rm -rf /opt/kallithea/lib/python3.11/site-packages/kallithea/front-end/node_modules

FROM docker.io/library/python@sha256:815dd89cd46d15d8677cb9ab1101450d5af2a103c2735577ade547b1735416ab
COPY --from=build /opt/kallithea /opt/kallithea
LABEL org.opencontainers.image.source="https://kallithea-scm.org/repos/kallithea" \
      org.opencontainers.image.revision="976a1e77932ea7de09558989eb82182c9429ed8f"
ENV PATH=/opt/kallithea/bin:$PATH PYTHONDONTWRITEBYTECODE=1
USER 1000:1000
EXPOSE 5000
ENTRYPOINT ["/opt/kallithea/bin/gearbox"]
