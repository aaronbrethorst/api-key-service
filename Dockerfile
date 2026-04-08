FROM eclipse-temurin:11-jre

ARG JAR_VERSION=2.7.1
ENV JAR_VERSION=${JAR_VERSION}

RUN apt-get update && \
    apt-get install -y \
    jq \
    curl && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Download the OneBusAway API Key CLI JAR
RUN curl -L \
    https://repo1.maven.org/maven2/org/onebusaway/onebusaway-api-key-cli/${JAR_VERSION}/onebusaway-api-key-cli-${JAR_VERSION}-withAllDependencies.jar \
    -o api-key-cli.jar

# Download PostgreSQL JDBC driver (not bundled in the fat JAR)
RUN curl -L \
    https://repo1.maven.org/maven2/org/postgresql/postgresql/42.7.5/postgresql-42.7.5.jar \
    -o postgresql.jar

COPY entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

ENTRYPOINT ["/app/entrypoint.sh"]
