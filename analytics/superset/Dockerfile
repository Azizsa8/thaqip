# Thaqip analytics: Apache Superset 6.1 plus what the lean image lacks — a
# PostgreSQL driver and compiled Arabic translations — and our configuration.
FROM apache/superset:6.1.0

USER root
RUN uv pip install --python /app/.venv/bin/python --no-cache psycopg2-binary==2.9.10
COPY build_lang.py ar_overrides.json /tmp/
RUN /app/.venv/bin/python /tmp/build_lang.py ar
COPY superset_config.py /app/pythonpath/superset_config.py
COPY assets/ /app/superset/static/assets/thaqip/
USER superset
