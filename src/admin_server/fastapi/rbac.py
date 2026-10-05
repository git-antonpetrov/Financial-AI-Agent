"""
Модуль ролевой модели доступа (RBAC - Role-Based Access Control).
Реализует принцип наименьших привилегий (Principle of Least Privilege),
определяет роли пользователей (superadmin, operator, auditor),
гранулярные разрешения (Permission) и FastAPI зависимости авторизации.
"""

from enum import Enum
from typing import Set, List, Optional



class Role(str, Enum):
    """Роли пользователей в системе администрирования."""
    SUPERADMIN = "superadmin"
    OPERATOR = "operator"
    AUDITOR = "auditor"


class Permission(str, Enum):
    """Гранулярные разрешения на выполнение операций в системе."""
    # Управление документами
    DOCUMENTS_READ = "documents:read"
    DOCUMENTS_WRITE = "documents:write"
    DOCUMENTS_DELETE = "documents:delete"

    # Управление заявками агентов
    REQUESTS_READ = "requests:read"
    REQUESTS_MANAGE = "requests:manage"

    # Управление агентами и открытыми ключами
    AGENTS_READ = "agents:read"
    AGENTS_MANAGE = "agents:manage"
    KEYS_ROTATE = "keys:rotate"
    KEYS_REVOKE = "keys:revoke"

    # Неизменяемый журнал аудита
    AUDIT_READ = "audit:read"
    AUDIT_VERIFY = "audit:verify"

    # Безопасность и конфигурация сервера
    SECURITY_MANAGE = "security:manage"


# Матрица разрешений для каждой роли (Least Privilege Principle)
ROLE_PERMISSIONS: dict[Role, set[Permission]] = {
    Role.SUPERADMIN: {
        Permission.DOCUMENTS_READ,
        Permission.DOCUMENTS_WRITE,
        Permission.DOCUMENTS_DELETE,
        Permission.REQUESTS_READ,
        Permission.REQUESTS_MANAGE,
        Permission.AGENTS_READ,
        Permission.AGENTS_MANAGE,
        Permission.KEYS_ROTATE,
        Permission.KEYS_REVOKE,
        Permission.AUDIT_READ,
        Permission.AUDIT_VERIFY,
        Permission.SECURITY_MANAGE,
    },
    Role.OPERATOR: {
        # Оператор управляет документами, обрабатывает заявки, видит агентов и может выполнять плановую ротацию ключей
        Permission.DOCUMENTS_READ,
        Permission.DOCUMENTS_WRITE,
        Permission.DOCUMENTS_DELETE,
        Permission.REQUESTS_READ,
        Permission.REQUESTS_MANAGE,
        Permission.AGENTS_READ,
        Permission.KEYS_ROTATE,
    },
    Role.AUDITOR: {
        # Аудитор имеет исключительно Read-Only доступ и право верификации цепочки аудита
        Permission.DOCUMENTS_READ,
        Permission.REQUESTS_READ,
        Permission.AGENTS_READ,
        Permission.AUDIT_READ,
        Permission.AUDIT_VERIFY,
    },
}

ROLE_DESCRIPTIONS: dict[Role, str] = {
    Role.SUPERADMIN: "Главный администратор системы с абсолютными привилегиями управления",
    Role.OPERATOR: "Оператор документооборота и обработки входящих заявок агентов",
    Role.AUDITOR: "Офицер безопасности и аудита с правом чтения и верификации цепочки журналов",
}


def get_permissions_for_role(role: Role | str) -> set[Permission]:
    """Возвращает набор разрешений для указанной роли."""
    if isinstance(role, str):
        try:
            role = Role(role.lower().strip())
        except ValueError:
            return set()
    return ROLE_PERMISSIONS.get(role, set()).copy()


class CurrentUser(str):
    """
    Класс авторизованного пользователя, расширяющий str для 100% обратной
    совместимости со старыми зависимостями (где ожидался str username).
    Содержит роль, набор разрешений и методы проверки прав.
    """
    role: Role
    permissions: set[Permission]

    def __new__(
        cls,
        username: str,
        role: Role | str = Role.SUPERADMIN,
        permissions: Optional[set[Permission]] = None
    ):
        instance = str.__new__(cls, username)
        if isinstance(role, str):
            try:
                role_enum = Role(role.lower().strip())
            except ValueError:
                role_enum = Role.SUPERADMIN
        else:
            role_enum = role

        instance.role = role_enum
        if permissions is not None:
            instance.permissions = permissions
        else:
            instance.permissions = get_permissions_for_role(role_enum)
        return instance

    @property
    def username(self) -> str:
        """Имя пользователя."""
        return str(self)

    def has_permission(self, permission: Permission | str) -> bool:
        """Проверяет наличие конкретного разрешения у пользователя."""
        if isinstance(permission, str):
            try:
                perm_enum = Permission(permission)
            except ValueError:
                return False
        else:
            perm_enum = permission
        return perm_enum in self.permissions

    def has_role(self, role: Role | str) -> bool:
        """Проверяет совпадение роли пользователя."""
        if isinstance(role, Role):
            return self.role == role
        return self.role.value == str(role).lower().strip()

    def __repr__(self) -> str:
        return f"CurrentUser(username='{self.username}', role='{self.role.value}', permissions_count={len(self.permissions)})"

