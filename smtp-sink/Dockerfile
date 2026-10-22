# smtp-sink in a container. Build from this directory:
#
#     docker build -t smtp-sink:local .
#
# The base is pinned by version and digest. 3.14.7 rather than anything newer
# because every later 3.14 slim image was published too recently to use; move
# both together when bumping it.
FROM python:3.14.7-slim@sha256:51dafde81dbdb6ebde285137a295cf18a47ca95234fe388a343719cb97305b3d

# Unbuffered because stdout here is a pipe, and the line the sink prints for
# each message would otherwise sit in a buffer instead of reaching docker logs.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# A user of its own, and a directory it owns for the log. A named volume
# mounted on /data takes that ownership when it is first created; a host
# directory mounted there has to be writable by uid 10001 instead.
RUN useradd --system --uid 10001 --no-create-home --shell /usr/sbin/nologin sink \
    && mkdir /data \
    && chown sink /data

# The one file the sink needs. The tests and anything else in this directory
# stay out of the image.
COPY smtp_sink.py /app/smtp_sink.py

USER sink
WORKDIR /data
EXPOSE 2525

# Python as PID 1 ignores SIGTERM, which is what docker stop sends, so every
# stop would wait out the timeout and end in SIGKILL. Python does handle
# SIGINT, and the sink exits cleanly on it.
STOPSIGNAL SIGINT

# Every interface of the container, which is only the container's own network.
# Which host address the LAN can reach it on is decided where the port is
# published, and that is where the care the README asks for belongs.
# Anything given as the container's command, a syslog server or a webhook,
# is added after these.
ENTRYPOINT ["python", "/app/smtp_sink.py", "--bind", "0.0.0.0", "--port", "2525", "--log", "/data/smtp_sink.log"]
