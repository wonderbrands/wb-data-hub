# wonderbrands-python:v3.0 — la imagen v2.0 + Node.js 22, para coffee-etl (capa snap)
#
# Por qué existe: la corrida de snap de coffee-etl suma el "motor" de Coffee
# (propuestas de targets, cobertura Compass y plan de compras), que está escrito
# en Node.js. La v2.0 solo tiene Python. Esta imagen es la v2.0 con Node agregado:
# todo lo que corre hoy en la v2.0 sigue corriendo igual.
#
# Qué agrega respecto de python_librariesv2.Dockerfile:
#   1. Node.js 22.23.4 (LTS) + npm, copiados de la imagen oficial de Node.
#   2. Los dos paquetes de Node que usa el motor (mysql2 y dotenv), en /node_modules.
#   3. Las librerías de Python de coffee-etl con versión fija (las mismas de
#      database/coffee-etl/requirements.txt del repo demand-planner), para que un
#      cambio de pandas/numpy en PyPI no cambie los números de un día para otro.
#   4. Al final del build, una prueba que importa todo: si falta algo, el build falla
#      en vez de fallar la corrida de Kestra.
#
# Se construye con el flujo wonderbrands.system / build_and_publish_docker_image:
#   image_name = wonderbrands-python    semantic_tag = v3.0

# ---- Etapa 1: de acá solo se copia Node (no queda en la imagen final) ----------
FROM node:22.23.4-bookworm-slim AS node

# ---- Etapa 2: la imagen final (misma base que la v2.0) -----------------------
FROM python:3.12-slim

# Igual que la v2.0
ENV PYTHONUNBUFFERED=1
ENV TZ=America/Mexico_City

# Igual que la v2.0: compilar mysqlclient y git (el flujo clona el repo con git por ssh)
RUN apt-get update && apt-get install -y \
    pkg-config \
    default-libmysqlclient-dev \
    build-essential \
    python3-dev \
    git \
    && rm -rf /var/lib/apt/lists/*

# Librerías de Python.
# - Con versión fija: las que usa coffee-etl (database/coffee-etl/requirements.txt).
# - Sin versión, igual que en la v2.0: el resto, para que esta imagen sirva para
#   todo lo que hoy usa la v2.0.
RUN pip install --no-cache-dir \
    mysqlclient==2.2.8 \
    pymysql==1.2.0 \
    pandas==3.0.2 \
    numpy==2.4.4 \
    python-dotenv==1.2.2 \
    mysql-connector-python==9.4.0 \
    requests \
    python-dateutil \
    tzdata \
    gspread \
    google-auth \
    PyPDF2

# Node.js + npm (los mismos archivos y accesos directos que arma la imagen oficial)
COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && ln -s ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx

# Paquetes de Node del motor, con las versiones del package-lock.json del repo.
# Van en /node_modules (la raíz del disco) a propósito: Node busca los paquetes
# subiendo carpeta por carpeta desde el archivo que los importa, así que desde
# cualquier carpeta donde Kestra clone el repo termina encontrándolos acá.
# (NODE_PATH solo no alcanza: los archivos .mjs del motor lo ignoran.)
RUN npm install --prefix / --no-save --no-package-lock --omit=dev --no-audit --no-fund \
    mysql2@3.24.6 \
    dotenv@17.4.2 \
    && npm cache clean --force
ENV NODE_PATH=/node_modules

# Prueba final: si falta cualquier cosa, el build falla acá.
RUN cd /tmp \
    && python --version && node --version && npm --version && git --version \
    && python -c "import MySQLdb, pymysql, pandas, numpy, dotenv, requests, dateutil, tzdata, gspread, google.auth, PyPDF2, mysql.connector; print('python ok', pandas.__version__, numpy.__version__)" \
    && node -e "require('mysql2/promise'); require('dotenv'); console.log('node require ok')" \
    && node --input-type=module -e "import mysql from 'mysql2/promise'; import dotenv from 'dotenv'; console.log('node import ok', typeof mysql.createPool, typeof dotenv.config)"
