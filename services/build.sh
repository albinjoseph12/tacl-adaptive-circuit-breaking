#!/usr/bin/env bash
# Compile both services against the jars in ./lib (no build tool required beyond a JDK 21).
# lib/ contents (all Apache/BSD/MIT licensed, as shipped by Ubuntu 24.04 packages, plus two real
# third-party resilience libraries pulled from Maven Central for the resilience4j/concurrency-limits
# comparators -- see policies/Resilience4jPolicy.java and policies/ConcurrencyLimitsPolicy.java):
#   postgresql-42.7.2, HikariCP-2.7.9, tomcat10-embed-core/el-10.1.55, jakarta-annotation-api-2.1.1,
#   netty-{buffer,codec,codec-http,common,handler,resolver,transport,transport-native-unix-common}-4.1.48.Final,
#   jackson-{core-2.14.1,databind-2.14.0,annotations-2.14.0}, slf4j-api/slf4j-simple-1.7.32,
#   resilience4j-{circuitbreaker,core}-2.2.0, vavr-0.10.4, vavr-match-0.10.4 (resilience4j's runtime dep),
#   concurrency-limits-core-0.5.4
set -euo pipefail
cd "$(dirname "$0")"
CP="$(ls lib/*.jar | tr '\n' ':')"
for svc in catalog-service gateway; do
  rm -rf "$svc/build"; mkdir -p "$svc/build"
  javac -d "$svc/build" -cp "$CP" $(find "$svc/src" -name '*.java')
  echo "built $svc"
done
