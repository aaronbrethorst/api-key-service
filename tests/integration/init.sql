-- Schema required by the OneBusAway API Key CLI JAR (Hibernate validation).
-- Column names are unquoted so PostgreSQL stores them as lowercase, matching
-- Hibernate's unquoted query generation.
-- Derived from onebusaway-application-modules entity mappings:
--   org.onebusaway.users.model.User        -> oba_users
--   org.onebusaway.users.model.UserIndex   -> oba_user_indices
--   org.onebusaway.users.model.UserRole    -> oba_user_roles

CREATE SEQUENCE hibernate_sequence START 1 INCREMENT 1;

CREATE TABLE oba_users (
    id             SERIAL PRIMARY KEY,
    creationtime   TIMESTAMP,
    lastaccesstime TIMESTAMP,
    temporary      BOOLEAN DEFAULT FALSE,
    properties     OID
);

CREATE TABLE oba_user_roles (
    name VARCHAR(255) PRIMARY KEY
);

CREATE TABLE oba_user_roles_mapping (
    user_id    INTEGER REFERENCES oba_users(id),
    roles_name VARCHAR(255) REFERENCES oba_user_roles(name),
    PRIMARY KEY (user_id, roles_name)
);

CREATE TABLE oba_user_indices (
    type        VARCHAR(255) NOT NULL,
    value       VARCHAR(255) NOT NULL,
    credentials VARCHAR(255),
    user_id     INTEGER REFERENCES oba_users(id),
    PRIMARY KEY (type, value)
);
