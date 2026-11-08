"""Проверяет повторное применение ролей Postgres без живого кластера."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "src" / "simulations" / "db" / "init_app_roles.sh"
WRAPPER = ROOT / "src" / "simulations" / "db" / "postgres-wrapper.sh"
ROLES = {
    "bank_app": "BANK_DB_PASSWORD",
    "invest_app": "INVEST_DB_PASSWORD",
    "digital_app": "DIGITAL_DB_PASSWORD",
}


def render(script: str, passwords: dict[str, str]) -> str:
    """Подставляет пароли ролей в текст скрипта.

    Аргументы:
        script: Исходный shell-скрипт.
        passwords: Пароль для каждой роли.

    Возвращает:
        Текст, в котором не осталось плейсхолдеров паролей.
    """
    rendered = script
    for role, env_name in ROLES.items():
        needle = "${" + env_name + "}"
        if needle not in rendered:
            raise AssertionError(f"В скрипте нет пароля {env_name}")
        rendered = rendered.replace(needle, passwords[role])
    return rendered


def apply_roles(sql: str, catalog: dict) -> None:
    """Применяет создание ролей и гранты к каталогу. Повтор обновляет пароль.

    Аргументы:
        sql: SQL скрипта после подстановки паролей.
        catalog: Состояние ролей, грантов и публичного доступа.

    Побочные эффекты:
        Меняет каталог. Повторный вызов не создаёт роль заново и не падает.
    """
    for role in ROLES:
        create = f"CREATE ROLE {role} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE PASSWORD '"
        alter = f"ALTER ROLE {role} WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE PASSWORD '"
        if create not in sql or alter not in sql:
            raise AssertionError(f"Для {role} нет пары CREATE и ALTER")
        created_password = sql.split(create, 1)[1].split("'", 1)[0]
        altered_password = sql.split(alter, 1)[1].split("'", 1)[0]
        if created_password != altered_password:
            raise AssertionError(f"Пароли CREATE и ALTER для {role} различаются")
        guard = f"IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '{role}')"
        if guard not in sql:
            raise AssertionError(f"Создание {role} не защищено проверкой существования")
        if role not in catalog["roles"]:
            catalog["roles"][role] = {"password": created_password, "superuser": False}
        else:
            catalog["roles"][role]["password"] = altered_password
            catalog["roles"][role]["superuser"] = False

    databases = (("bank_db", "bank_app"), ("invest_db", "invest_app"), ("digital_db", "digital_app"))
    for database_name, role in databases:
        grant = f"GRANT CONNECT ON DATABASE {database_name} TO {role};"
        revoke = f"REVOKE CONNECT ON DATABASE {database_name} FROM PUBLIC;"
        usage = f"GRANT USAGE, CREATE ON SCHEMA public TO {role};"
        defaults = (
            "ALTER DEFAULT PRIVILEGES IN SCHEMA public "
            f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {role};"
        )
        for statement in (grant, revoke, usage, defaults):
            if statement not in sql:
                raise AssertionError(statement)
        catalog["connect"].add((database_name, role))
        catalog["public_connect"].discard(database_name)
        catalog["schema"].add(role)
        catalog["defaults"].add(role)


def test_initialized_volume_reapplies_roles_without_error():
    """Повтор скрипта обновляет пароль и оставляет те же гранты."""
    script = SCRIPT.read_text(encoding="utf-8")
    wrapper = WRAPPER.read_text(encoding="utf-8")
    assert "PG_VERSION" in wrapper
    assert "sh /docker-entrypoint-initdb.d/02-init_app_roles.sh" in wrapper
    assert "ADD COLUMN IF NOT EXISTS idempotency_key" in script
    assert "ix_auto_payments_idempotency_key" in script
    assert "table_name = 'auto_payments'" in script

    catalog = {
        "roles": {},
        "connect": set(),
        "public_connect": {"bank_db", "invest_db", "digital_db"},
        "schema": set(),
        "defaults": set(),
    }
    first_passwords = {role: f"first-{role}" for role in ROLES}
    apply_roles(render(script, first_passwords), catalog)
    assert catalog["roles"]["bank_app"]["password"] == "first-bank_app"
    assert catalog["public_connect"] == set()

    second_passwords = {role: f"second-{role}" for role in ROLES}
    apply_roles(render(script, second_passwords), catalog)
    assert catalog["roles"]["bank_app"]["password"] == "second-bank_app"
    assert catalog["roles"]["digital_app"]["password"] == "second-digital_app"
    assert catalog["roles"]["invest_app"]["superuser"] is False
    assert catalog["connect"] == {
        ("bank_db", "bank_app"),
        ("invest_db", "invest_app"),
        ("digital_db", "digital_app"),
    }
    assert catalog["schema"] == set(ROLES)
    assert catalog["defaults"] == set(ROLES)
