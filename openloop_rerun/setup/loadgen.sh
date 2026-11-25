#!/usr/bin/env bash
set -euo pipefail
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq psmisc >/dev/null || true
bash /tmp/common.sh /opt/loadgen_venv httpx locust==2.46.5 psycopg2-binary==2.9.13
sudo mkdir -p /opt/acb-testbed && sudo chown $USER /opt/acb-testbed && tar xzf /tmp/testbed.tgz -C /opt/acb-testbed
[ -f ~/.ssh/id_ed25519 ] || ssh-keygen -q -t ed25519 -N '' -f ~/.ssh/id_ed25519
grep -E "httpx|locust|psycopg" /opt/loadgen_venv/freeze.txt
