-- Инициализация дополнительных баз данных симуляций для PostgreSQL
-- Скрипт выполняется автоматически при первой инициализации контейнера через /docker-entrypoint-initdb.d

SELECT 'CREATE DATABASE bank_db'
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'bank_db')\gexec

SELECT 'CREATE DATABASE invest_db'
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'invest_db')\gexec

SELECT 'CREATE DATABASE digital_db'
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'digital_db')\gexec
