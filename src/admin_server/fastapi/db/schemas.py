"""
Pydantic-схемы валидации запросов и ответов API администратора и агентов.
"""

from pydantic import BaseModel, Field, ConfigDict
from typing import Optional, List
from datetime import datetime


class TokenResponse(BaseModel):
    """Схема ответа успешной аутентификации с токеном доступа и токеном обновления."""
    access_token: str = Field(..., description="JWT-токен доступа (короткоживущий)")
    token_type: str = Field(default="bearer", description="Тип токена")
    refresh_token: Optional[str] = Field(default=None, description="JWT-токен обновления для ротации (RTR)")
    expires_in: Optional[int] = Field(default=None, description="Срок действия токена доступа в секундах")


class RefreshTokenRequest(BaseModel):
    """Схема запроса обновления пары токенов по схеме RTR."""
    refresh_token: str = Field(..., min_length=10, description="JWT refresh-токен")


class LogoutRequest(BaseModel):
    """Схема запроса выхода с опциональным отзывом refresh-токена."""
    refresh_token: Optional[str] = Field(default=None, description="Опциональный refresh-токен для отзыва")


class ContentAiConfigResponse(BaseModel):
    """Схема учетных данных сервиса распознавания Content AI."""
    username: str = Field(..., description="Логин учетной записи")
    password: str = Field(..., description="Пароль учетной записи")
    api_uri: str = Field(..., description="URI SOAP-сервиса распознавания")


class UploadResponse(BaseModel):
    """Схема ответа при загрузке документа в очередь обработки."""
    status: str = Field(..., description="Статус постановки задачи в очередь")
    message: str = Field(..., description="Поясняющее сообщение")
    agent: Optional[str] = Field(default=None, description="Имя агента")
    action: Optional[str] = Field(default=None, description="Действие над документом")


class BatchActionResponse(BaseModel):
    """Схема ответа при пакетном изменении статуса заявок агентов."""
    status: str = Field(default="ok", description="Статус выполнения операции")


class CheckHashRequest(BaseModel):
    """Схема запроса проверки наличия документа по его MD5-хэшу."""
    file_hash: str = Field(..., pattern=r"^[a-fA-F0-9]{32}$", description="MD5-хэш файла (32 hex-символа)")
    filename: str = Field(..., min_length=1, max_length=255, description="Имя проверяемого файла")
    agent_name: str = Field(..., min_length=1, max_length=50, pattern=r"^[a-zA-Z0-9_-]+$", description="Имя целевого агента")


class CheckHashResponse(BaseModel):
    """Схема ответа проверки MD5-хэша документа."""
    status: str = Field(..., description="Результат проверки хэша (new, duplicate, checking)")


class CheckDateRequest(BaseModel):
    """Схема запроса проверки актуальности версии документа по дате."""
    system_name: str = Field(..., min_length=1, max_length=150, pattern=r"^[a-zA-Z0-9_]+$", description="Системное имя документа с датой")
    short_name: str = Field(..., min_length=1, max_length=150, pattern=r"^[a-zA-Z0-9_]+$", description="Короткое имя документа без даты")
    file_hash: str = Field(..., pattern=r"^[a-fA-F0-9]{32}$", description="MD5-хэш файла (32 hex-символа)")
    filename: str = Field(..., min_length=1, max_length=255, description="Имя проверяемого файла")
    agent_name: str = Field(..., min_length=1, max_length=50, pattern=r"^[a-zA-Z0-9_-]+$", description="Имя целевого агента")


class CheckDateResponse(BaseModel):
    """Схема ответа проверки актуальности версии документа."""
    status: str = Field(..., description="Результат проверки версии (newer, duplicate, out_of_date)")


class LLMAnalyzeRequest(BaseModel):
    """Схема запроса анализа текста документа языковой моделью."""
    text: str = Field(..., max_length=150000, description="Текст документа для извлечения метаданных")


class LLMAnalyzeResponse(BaseModel):
    """Схема ответа анализа метаданных документа от LLM."""
    system_name: str = Field(..., description="Сформированное системное имя с датой")
    short_name: str = Field(..., description="Сформированное короткое имя документа")


class LLMRepealedRequest(BaseModel):
    """Схема запроса поиска отмененных документов через LLM."""
    snippets: List[str] = Field(..., description="Список фрагментов текста документа")


class LLMRepealedResponse(BaseModel):
    """Схема ответа со списком отмененных актов."""
    short_names: List[str] = Field(..., description="Список коротких имен отмененных актов")


class AgentRequestResponse(BaseModel):
    """Схема представления заявки агента на добавление документа."""
    id: int
    agent_name: str
    document_name_ru: str
    document_name_en: str
    justification_ru: str
    justification_en: str
    status: str
    created_at: datetime
    
    model_config = ConfigDict(from_attributes=True)


class AgentRequestCreate(BaseModel):
    """Схема создания заявки агента на добавление документа."""
    agent_name: str = Field(..., min_length=1, max_length=50, pattern=r"^[a-zA-Z0-9_-]+$", description="Имя агента")
    document_name_ru: str = Field(..., min_length=1, max_length=300, description="Название документа на русском языке")
    document_name_en: str = Field(..., min_length=1, max_length=300, description="Название документа на английском языке")
    justification_ru: str = Field(..., min_length=1, max_length=2000, description="Обоснование запроса на русском языке")
    justification_en: str = Field(..., min_length=1, max_length=2000, description="Обоснование запроса на английском языке")


class AgentRequestBatchAction(BaseModel):
    """Схема пакетного действия над заявками агентов."""
    request_ids: List[int] = Field(..., min_length=1, description="Список идентификаторов заявок")


class AgentRegisterRequest(BaseModel):
    """Схема регистрации агента в системе."""
    agent_name: str = Field(..., min_length=1, max_length=50, pattern=r"^[a-zA-Z0-9_-]+$", description="Имя агента")
    public_key: str = Field(..., min_length=10, max_length=8192, description="Публичный RSA-ключ в формате PEM")


class AgentRegisterResponse(BaseModel):
    """Схема ответа на регистрацию агента."""
    status: str = Field(..., description="Статус регистрации")
    message: str = Field(..., description="Поясняющее сообщение")


class AgentRequestJWT(BaseModel):
    """Схема запроса с JWT-токеном агента."""
    token: str = Field(..., min_length=10, max_length=4096, description="JWT-токен агента")


class AgentKeyInfo(BaseModel):
    """Схема информации о конкретном ключе агента."""
    kid: str = Field(..., description="Идентификатор ключа (Key ID)")
    status: str = Field(..., description="Статус ключа (active, superseded, revoked, expired)")
    is_revoked: bool = Field(..., description="Флаг отзыва ключа")
    revocation_reason: Optional[str] = Field(default=None, description="Причина отзыва")
    created_at: datetime = Field(..., description="Дата создания")
    expires_at: datetime = Field(..., description="Срок действия ключа")
    revoked_at: Optional[datetime] = Field(default=None, description="Дата отзыва")
    fingerprint: str = Field(..., description="SHA-256 отпечаток публичного ключа")

    model_config = ConfigDict(from_attributes=True)


class AgentDetailResponse(BaseModel):
    """Детальная информация об агенте и всех его ключах."""
    id: int
    name: str
    status: str
    registered_at: datetime
    active_key_id: Optional[str] = None
    keys: List[AgentKeyInfo] = []

    model_config = ConfigDict(from_attributes=True)


class AgentRotateKeyRequest(BaseModel):
    """Схема запроса плановой ротации открытого ключа агента."""
    agent_name: str = Field(..., min_length=1, max_length=50, pattern=r"^[a-zA-Z0-9_-]+$", description="Имя агента")
    new_public_key: str = Field(..., min_length=10, max_length=8192, description="Новый публичный RSA-ключ в формате PEM")
    ttl_days: Optional[int] = Field(default=90, ge=1, le=365, description="Срок действия ключа в днях")


class AgentRevokeKeyRequest(BaseModel):
    """Схема запроса отзыва ключа агента при компрометации."""
    agent_name: str = Field(..., min_length=1, max_length=50, pattern=r"^[a-zA-Z0-9_-]+$", description="Имя агента")
    kid: Optional[str] = Field(default=None, description="ID ключа (если не указан, отзываются все активные ключи)")
    reason: str = Field(default="Compromised", min_length=1, max_length=255, description="Причина отзыва")


class AgentActionResponse(BaseModel):
    """Схема статусного ответа на действие над ключом/агентом."""
    status: str = Field(..., description="Статус операции")
    message: str = Field(..., description="Поясняющее сообщение")
    agent_name: str = Field(..., description="Имя агента")
    key_id: Optional[str] = Field(default=None, description="ID затронутого ключа")


# --- СХЕМЫ ДЛЯ ЖУРНАЛА АУДИТА (AUDIT TRAIL / HASH-CHAINING) ---

class AuditLogEntryResponse(BaseModel):
    """Схема отдельной записи в криптографически связанном журнале аудита."""
    id: int = Field(..., description="Уникальный идентификатор записи")
    timestamp: datetime = Field(..., description="Метка времени фиксации события (UTC)")
    actor: str = Field(..., description="Инициатор события (admin, agent, system)")
    actor_ip: Optional[str] = Field(default=None, description="IP-адрес инициатора")
    action: str = Field(..., description="Тип действия (например, AUTH_LOGIN_SUCCESS, KEY_ROTATED)")
    resource: Optional[str] = Field(default=None, description="Целевой ресурс")
    status: str = Field(..., description="Результат операции (SUCCESS, FAILURE, WARNING)")
    details: Optional[str] = Field(default=None, description="Дополнительный контекст/метаданные")
    prev_hash: str = Field(..., description="SHA-256 хэш предыдущей записи аудита")
    record_hash: str = Field(..., description="SHA-256 хэш текущей записи (контроль целостности)")

    model_config = ConfigDict(from_attributes=True)


class AuditLogListResponse(BaseModel):
    """Схема ответа со списком записей журнала аудита и пагинацией."""
    total: int = Field(..., description="Всего записей, удовлетворяющих фильтру")
    items: List[AuditLogEntryResponse] = Field(..., description="Список записей журнала аудита")
    limit: int = Field(..., description="Лимит выборки на страницу")
    offset: int = Field(..., description="Смещение выборки")


class AuditVerifyResponse(BaseModel):
    """Схема ответа криптографической проверки целостности журнала аудита."""
    is_valid: bool = Field(..., description="Флаг целостности цепочки хэшей (True - подделок нет)")
    total_records: int = Field(..., description="Количество проверенных записей")
    broken_record_id: Optional[int] = Field(default=None, description="ID скомпрометированной записи при обнаружении подделки")
    broken_record_index: Optional[int] = Field(default=None, description="Индекс скомпрометированной записи в цепочке")
    details: Optional[str] = Field(default=None, description="Подробный отчет проверки целостности")


class AuditSummaryResponse(BaseModel):
    """Схема сводной статистики по журналу аудита и состоянию цепочки."""
    total_records: int = Field(..., description="Всего зафиксировано событий")
    success_count: int = Field(..., description="Количество успешных операций")
    failure_count: int = Field(..., description="Количество неудачных операций/ошибок")
    warning_count: int = Field(..., description="Количество предупреждений")
    last_event: Optional[AuditLogEntryResponse] = Field(default=None, description="Последнее зафиксированное событие")
    is_chain_intact: bool = Field(..., description="Статус криптографической целостности цепочки")
    verification_details: Optional[str] = Field(default=None, description="Детализация состояния цепочки")


# --- СХЕМЫ ДЛЯ РОЛЕВОЙ МОДЕЛИ ДОСТУПА (RBAC) ---

class CurrentUserResponse(BaseModel):
    """Схема профиля текущего авторизованного пользователя с ролью и списком прав."""
    username: str = Field(..., description="Имя пользователя / субъекта")
    role: str = Field(..., description="Назначенная роль (superadmin, operator, auditor)")
    permissions: List[str] = Field(..., description="Список активных гранулярных разрешений пользователя")


class RoleInfoResponse(BaseModel):
    """Информация о роли в системе и её полномочиях."""
    role: str = Field(..., description="Название роли")
    description: str = Field(..., description="Описание назначения роли")
    permissions: List[str] = Field(..., description="Список разрешений данной роли")


class RoleMatrixResponse(BaseModel):
    """Матрица ролей и разрешений системы RBAC."""
    roles: List[RoleInfoResponse] = Field(..., description="Доступные роли и их полномочия")
    all_permissions: List[str] = Field(..., description="Полный перечень гранулярных разрешений в системе")


