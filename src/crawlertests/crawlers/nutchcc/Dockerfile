# Common Crawl's fork of Apache Nutch, built as described by
# https://github.com/commoncrawl/cc-nutch-example: crawler-commons and the CLD2
# language detector from source, then Nutch's local runtime. The *_REF build
# args are commits (or branches); the harness passes commit hashes so that a
# new commit rebuilds only the layers after it. Ivy downloads hundreds of
# jars for Nutch, so the build is retried (keeping what it got) after a network error.
FROM docker.io/library/eclipse-temurin:17-jdk-noble

RUN apt-get update \
 && apt-get install -y --no-install-recommends ant maven git libcld2-0 libcld2-dev \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /opt

ARG CRAWLER_COMMONS_REF=HEAD
RUN git clone -q https://github.com/crawler-commons/crawler-commons.git \
 && cd crawler-commons && git checkout -q "$CRAWLER_COMMONS_REF" \
 && mvn -B -q install -DskipTests -Dmaven.javadoc.skip=true -Dgpg.skip \
 && git log -1 --format='crawler-commons %H %cI' > /opt/revisions.txt \
 && cd .. && rm -rf crawler-commons

ARG CLD2_REF=HEAD
RUN git clone -q https://github.com/commoncrawl/language-detection-cld2.git \
 && cd language-detection-cld2 && git checkout -q "$CLD2_REF" \
 && mvn -B -q install -DskipTests -Dmaven.javadoc.skip=true -Dgpg.skip \
 && git log -1 --format='language-detection-cld2 %H %cI' >> /opt/revisions.txt \
 && cd .. && rm -rf language-detection-cld2

ARG EXAMPLE_REF=HEAD
RUN git clone -q https://github.com/commoncrawl/cc-nutch-example.git \
 && cd cc-nutch-example && git checkout -q "$EXAMPLE_REF" \
 && git log -1 --format='cc-nutch-example %H %cI' >> /opt/revisions.txt

ARG NUTCH_REF=cc
RUN git clone -q https://github.com/commoncrawl/nutch.git cc-nutch-example/nutch-cc \
 && cd cc-nutch-example/nutch-cc && git checkout -q "$NUTCH_REF" \
 && git log -1 --format='nutch %H %cI' >> /opt/revisions.txt \
 && sed -n 's/^version=/nutch-version /p' default.properties >> /opt/revisions.txt \
 && for attempt in 1 2 3; do ant -q runtime && break; [ $attempt -lt 3 ] || exit 1; done \
 && rm -rf build ~/.ivy2/cache

ENV NUTCH_HOME=/opt/cc-nutch-example/nutch-cc/runtime/local
