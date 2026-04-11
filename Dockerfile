FROM eclipse-temurin:11-jre

ARG JAR_VERSION=2.7.1
ARG PG_DRIVER_VERSION=42.7.5

RUN apt-get update && \
    apt-get install -y \
    jq \
    curl \
    postgresql-client && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

RUN curl -L \
    https://repo1.maven.org/maven2/org/onebusaway/onebusaway-api-key-cli/${JAR_VERSION}/onebusaway-api-key-cli-${JAR_VERSION}-withAllDependencies.jar \
    -o api-key-cli.jar && \
    curl -L \
    https://repo1.maven.org/maven2/org/postgresql/postgresql/${PG_DRIVER_VERSION}/postgresql-${PG_DRIVER_VERSION}.jar \
    -o postgresql.jar

COPY --chmod=755 entrypoint.sh /app/entrypoint.sh

RUN useradd -r -s /usr/sbin/nologin appuser
USER appuser

ENTRYPOINT ["/app/entrypoint.sh"]
