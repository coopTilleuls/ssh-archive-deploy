ARG E2E_SSH_BASE_IMAGE=debian:bookworm-slim
FROM ${E2E_SSH_BASE_IMAGE}

RUN apt-get update \
  && apt-get install -y --no-install-recommends openssh-server \
  && rm -rf /var/lib/apt/lists/*

RUN useradd --create-home --shell /bin/sh deploy \
  && mkdir -p /run/sshd

COPY entrypoint.sh /entrypoint.sh
RUN chmod 755 /entrypoint.sh

EXPOSE 22

ENTRYPOINT ["/entrypoint.sh"]
