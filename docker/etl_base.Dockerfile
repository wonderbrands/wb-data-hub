FROM python:3.12-slim

#configuraciones para evitar basura en los logs y definir la zona horaria
ENV PYTHONUNBUFFERED=1
ENV TZ=America/Mexico_City

#dependencias del sistema operativo necesarias para compilar MySQL
RUN apt-get update && apt-get install -y \
    pkg-config \
    default-libmysqlclient-dev \
    build-essential \
    python3-dev \
    && rm -rf /var/lib/apt/lists/*

#librerias Python
RUN pip install --no-cache-dir \
    mysql-connector-python==9.4.0 \
    mysqlclient==2.2.8 \
    pymysql \
    requests \
    python-dotenv \
    python-dateutil \
    tzdata \
    pandas