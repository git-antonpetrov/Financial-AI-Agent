"""
Pydantic-схемы валидации запросов и ответов API администратора и агентов.
"""

from pydantic import BaseModel, Field, ConfigDict
from typing import Optional, List
from datetime import datetime


class TokenResponse(BaseModel):
    """Схема ответа успешной аутентификации с токеном доступа."""
    access_token: str = Field(..., description="JWT-токен доступа")
    token_type: str = Field(default="bearer", description="Тип токена")


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
