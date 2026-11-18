FROM docker.io/library/python@sha256:e683e47faa4ba2951ef04ca609b595e968c49045eacdb765b7ff10165a5fde50 AS build
COPY allura-1.22.1.tar.gz /tmp/
WORKDIR /opt/allura/source
RUN echo '2d427dad0c2f99af5175998624e853a4db8faaabad61fac401b61a567a80807427f9b18aa94e76793b6bd52274f2cecbd2ef9947aaf6819f9492881feef2d4f4  /tmp/allura-1.22.1.tar.gz' | sha512sum -c - \
    && tar -xzf /tmp/allura-1.22.1.tar.gz --strip-components=1 --exclude='._*' \
       --no-same-owner --no-same-permissions \
    && test -s Allura/allura/public/nf/js/build/transpiled.js \
    && python -m venv /opt/allura/venv \
    && /opt/allura/venv/bin/pip install --no-cache-dir --disable-pip-version-check -r requirements.txt 'wheel==0.45.1'
RUN /opt/allura/venv/bin/pip install --no-cache-dir --disable-pip-version-check \
       --no-build-isolation --no-deps --config-settings editable_mode=compat \
       -e ./Allura -e ./ForgeActivity -e ./ForgeBlog -e ./ForgeChat -e ./ForgeDiscussion \
       -e ./ForgeFeedback -e ./ForgeFiles -e ./ForgeGit -e ./ForgeImporters -e ./ForgeLink \
       -e ./ForgeShortUrl -e ./ForgeTracker -e ./ForgeUserStats -e ./ForgeWiki \
    && /opt/allura/venv/bin/pip check

FROM docker.io/library/python@sha256:ac25e5afbfdb4a94ff99cc24b0345719210133144481480bcba6085efa36a341
RUN apt-get update \
    && apt-get install -qy --no-install-recommends curl git libmagic1 libcrypt1 zip \
    && rm -rf /var/lib/apt/lists/*
COPY --from=build /opt/allura /opt/allura
COPY allura_git_http.py bounded_subprocess.py strict_json.py /opt/allura/source/Allura/
RUN chmod 0644 /opt/allura/source/Allura/allura_git_http.py /opt/allura/source/Allura/bounded_subprocess.py /opt/allura/source/Allura/strict_json.py
LABEL org.opencontainers.image.source="https://allura.apache.org/" \
      org.opencontainers.image.version="1.22.1"
ENV PATH=/opt/allura/venv/bin:$PATH PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /opt/allura/source/Allura
USER 1000:1000
EXPOSE 8080
ENTRYPOINT ["/opt/allura/venv/bin/gunicorn"]
CMD ["--bind", "0.0.0.0:8080", "--timeout", "90", "--forwarded-allow-ips=", "allura_git_http:make_app('/run/allura/allura.ini')"]
