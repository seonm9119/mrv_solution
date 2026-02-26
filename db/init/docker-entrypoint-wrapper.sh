#!/bin/sh
# Postgres 기동 후 매번 lca DB 삭제·재생성 및 01~04 스크립트 실행
set -e

# Postgres 기본 엔트리포인트로 서버 백그라운드 기동
/usr/local/bin/docker-entrypoint.sh postgres &
PID=$!

# postgres DB 접속 가능할 때까지 대기 (lca는 아직 없을 수 있음)
until pg_isready -U "${POSTGRES_USER}" -d postgres 2>/dev/null; do sleep 1; done

# lca 삭제·재생성 및 01~04 실행
/run-all.sh

# Postgres 프로세스 유지
wait $PID
