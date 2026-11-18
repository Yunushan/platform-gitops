FROM docker.io/library/alpine@sha256:9db2934886712d877c932e07249c75a59e6ca27adfbccc05261987a17bef2d63

RUN wget -q -O /etc/apk/keys/alpine@sr.ht.rsa.pub https://mirror.sr.ht/alpine/alpine@sr.ht.rsa.pub \
    && echo 'c7860d80f4d8c6e9f7aaabd4ae7f7642eee6ecb45b08064401801ee48bee5034  /etc/apk/keys/alpine@sr.ht.rsa.pub' | sha256sum -c - \
    && sed -i '1i https://mirror.sr.ht/alpine/v3.22/sr.ht' /etc/apk/repositories \
    && apk upgrade --no-cache \
    && apk add --no-cache meta.sr.ht=0.90.2-r0 git.sr.ht=0.102.13-r0 py3-srht=0.84.10-r0 \
       sourcehut-migrate=0.3.1-r0 sourcehut-ssh=0.6.1-r0 openssh-server nginx fcgiwrap tzdata git-daemon \
    && test -x /usr/libexec/git-core/git-http-backend \
    && test "$(id -u git):$(id -g git)" = 101:101 \
    && test "$(id -u meta):$(id -g meta)" = 102:102 \
    && test "$(id -u nginx):$(id -g nginx)" = 103:103

# OpenSSH rejects locked system accounts before checking their keys.
# This password is discarded and the dedicated listener accepts only keys.
RUN password=$(head -c 48 /dev/urandom | base64) \
    && test "${#password}" = 64 \
    && printf 'git:%s\n' "$password" | chpasswd

USER 101:101
