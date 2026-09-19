FROM flyway/flyway:12.11 AS flyway

RUN cd /flyway/lib/flyway \
 && find . -name 'flyway-*.jar' \
      \( -name 'flyway-database-*' -o -name 'flyway-nc-*' -o -name 'flyway-gcp-*' \
         -o -name 'flyway-mysql-*' -o -name 'flyway-sqlserver-*' -o -name 'flyway-firebird-*' \
         -o -name 'flyway-singlestore-*' -o -name 'flyway-locations-s3-*' \) \
      ! -name 'flyway-database-postgresql-*' ! -name 'flyway-nc-core-*' -delete

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    JAVA_HOME=/opt/java/openjdk \
    PATH=/flyway:/opt/java/openjdk/bin:$PATH \
    REDGATE_DISABLE_TELEMETRY=true

COPY --from=flyway /opt/java/openjdk /opt/java/openjdk
COPY --from=flyway /flyway/flyway /flyway/flyway
COPY --from=flyway /flyway/conf /flyway/conf
COPY --from=flyway /flyway/lib /flyway/lib
COPY --from=flyway /flyway/drivers/postgresql-*.jar /flyway/drivers/

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY db ./db
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh

RUN useradd --create-home --uid 10001 appuser
USER appuser

EXPOSE 8000

ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
