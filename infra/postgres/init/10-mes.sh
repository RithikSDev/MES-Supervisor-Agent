#!/bin/bash
# Creates the MES roles and databases. Runs once, on an empty data volume.
#   mes_owner  owns mes_core: runs migrations and seeds (DDL)
#   mes_app    runtime role for mes-core: DML only, read-only on the audit log
#   integ      owns mes_integration (erp-adapter)
#   keycloak   owns the keycloak database
set -euo pipefail

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname postgres <<EOSQL
CREATE ROLE mes_owner LOGIN PASSWORD '${MES_OWNER_PASSWORD}';
CREATE ROLE mes_app LOGIN PASSWORD '${MES_APP_PASSWORD}';
CREATE ROLE integ LOGIN PASSWORD '${INTEG_PASSWORD}';
CREATE ROLE keycloak LOGIN PASSWORD '${KEYCLOAK_DB_PASSWORD}';
CREATE DATABASE mes_core OWNER mes_owner;
CREATE DATABASE mes_integration OWNER integ;
CREATE DATABASE keycloak OWNER keycloak;
EOSQL

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname mes_core <<EOSQL
CREATE EXTENSION IF NOT EXISTS btree_gist;
CREATE EXTENSION IF NOT EXISTS timescaledb;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
GRANT CONNECT ON DATABASE mes_core TO mes_app;
EOSQL
