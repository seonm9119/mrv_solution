# Postgres 16: 컨테이너 기동 시마다 lca DB 초기화(삭제·재생성) 후 스키마 적용
FROM postgres:16-alpine

COPY init/01-schema.sql /docker-entrypoint-initdb.d/
COPY init/02-dummy.sql /docker-entrypoint-initdb.d/
COPY init/01-mrv.sql /docker-entrypoint-initdb.d/
COPY init/run-all.sh /run-all.sh
COPY init/docker-entrypoint-wrapper.sh /docker-entrypoint-wrapper.sh
RUN chmod +x /run-all.sh /docker-entrypoint-wrapper.sh
ENTRYPOINT ["/docker-entrypoint-wrapper.sh"]
CMD ["postgres"]
