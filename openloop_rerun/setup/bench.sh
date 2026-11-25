#!/usr/bin/env bash
# Benchmark VM: OpenJDK 21 + PostgreSQL 16 from the Ubuntu archive as it was on 2026-09-12 (the versions the
# original campaign recorded: 21.0.12, 16.15), PostgreSQL configured as docker-compose.yml / the paper state.
set -euo pipefail
SNAP=20260912T000000Z
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --snapshot $SNAP openjdk-21-jdk-headless postgresql-16 postgresql-client-16 psmisc >/dev/null
HBA=/etc/postgresql/16/main/pg_hba.conf
sudo grep -q "acb-rerun" $HBA || sudo sed -i '1i # acb-rerun: local benchmark host, trust loopback\nhost all all 127.0.0.1/32 trust\nhost all all ::1/128 trust' $HBA
for s in "max_connections=200" "shared_buffers='1GB'" "work_mem='8MB'" "effective_cache_size='3GB'" "synchronous_commit=off" "max_wal_size='2GB'" "track_activity_query_size=2048"; do
  sudo -u postgres psql -qc "ALTER SYSTEM SET $s"; done
sudo systemctl restart postgresql
sudo -u postgres psql -qc "DO \$\$BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='acb') THEN CREATE ROLE acb LOGIN PASSWORD 'acb'; END IF; END\$\$; GRANT pg_monitor TO acb;"
sudo -u postgres psql -tAc "SELECT 1 FROM pg_database WHERE datname='acb'" | grep -q 1 || sudo -u postgres createdb -O acb acb
bash /tmp/common.sh /opt/acb_venv psycopg2-binary==2.9.13
sudo mkdir -p /opt/acb-testbed && sudo chown $USER /opt/acb-testbed && tar xzf /tmp/testbed.tgz -C /opt/acb-testbed
cd /opt/acb-testbed && bash services/build.sh
java -version 2>&1 | head -1; psql -U acb -h localhost -d acb -tAc "select version()" | cut -c1-60
psql -U acb -h localhost -d acb -tAc "show shared_buffers; show max_connections; show synchronous_commit"
