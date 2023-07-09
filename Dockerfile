FROM debian:bookworm-slim AS base

FROM base AS build
ARG ROCKS_TESTSUITE_VERSION=0.1.0

RUN apt-get -y update && \
	apt-get -y install python3-poetry python3-pip python3-venv git

COPY . /usr/src/rocks-testsuite
WORKDIR /usr/src/rocks-testsuite

RUN poetry build && python3 -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
RUN pip3 install "/usr/src/rocks-testsuite/dist/rocks_testsuite-${ROCKS_TESTSUITE_VERSION}.tar.gz"


FROM debian:bookworm-slim AS runtime

RUN apt-get -y update && apt-get -y install python3
COPY --from=build /opt/venv /opt/venv

ENV PATH="/opt/venv/bin:$PATH"
EXPOSE 8000
CMD ["/opt/venv/bin/rocks", "--host=0.0.0.0"]
