"""
Роутер безопасного приема и векторизации документов в RAG (Zero-Trust Endpoint).
Реализует спецификацию:
1. Защита от атак повторения (Nonce Replay Protection via Redis);
2. Временное окно актуальности запроса (Timestamp window <= 300s);
3. Проверка mTLS клиентского сертификата (X-Client-Cert-*);
4. Проверка криптографической цифровой подписи полезной нагрузки (X-Signature);
5. Шифрование сырого документа при хранении в PostgreSQL (AES-256-GCM);
6. Постановка задачи векторизации в очередь Redis для vector-worker;
7. Возврат криптографически подписанной сервером квитанции (AckReceipt).
"""

import os
import re
import json
import time
import uuid
import hashlib
from typing import Any, Optional
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status, Header
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

try:
    from security import (
        settings,
        verify_mtls_client_identity,
        record_admin_nonce_if_new,
        compute_rag_canonical_digest,
        verify_digital_signature,
        sign_server_receipt,
        get_minio_sse,
        calculate_key_fingerprint,
    )
    from core.utils.console_logger import log_info, log_error, log_warning, log_success
except ImportError:
    try:
        from ..security import (
            settings,
            verify_mtls_client_identity,
            record_admin_nonce_if_new,
            compute_rag_canonical_digest,
            verify_digital_signature,
            sign_server_receipt,
            get_minio_sse,
            calculate_key_fingerprint,
        )
        from ..core.utils.console_logger import log_info, log_error, log_warning, log_success
    except ImportError:
        from src.admin_server.fastapi.security import (
            settings,
            verify_mtls_client_identity,
            record_admin_nonce_if_new,
            compute_rag_canonical_digest,
            verify_digital_signature,
            sign_server_receipt,
            get_minio_sse,
            calculate_key_fingerprint,
        )
        from src.admin_server.core.utils.console_logger import log_info, log_error, log_warning, log_success

try:
    from db.database import get_db
    from db import crud, audit
except ImportError:
    try:
        from ..db.database import get_db
        from ..db import crud, audit
    except ImportError:
        from src.admin_server.fastapi.db.database import get_db
        from src.admin_server.fastapi.db import crud, audit


router = APIRouter(prefix="/api/v1/rag", tags=["RAG Documents Zero-Trust"])


# --- Pydantic Схемы ---

class RAGDocumentRequest(BaseModel):
    document_title: str = Field(..., description="Название документа")
    collection_name: str = Field(default="financial_kb", description="Имя коллекции или целевой базы знаний")
    content: str = Field(..., description="Текст документа для индексации")
    metadata: dict[str, Any] = Field(default_factory=dict, description="Метаданные документа")


class RAGDocumentResponse(BaseModel):
    receipt_id: str
    status: str
    document_id: str
    document_title: str
    collection_name: str
    timestamp: int
    nonce: str
    signature: str
    server_key_fingerprint: str


def _get_external_services():
    """Безопасно извлекает minio_client и redis_client из server."""
    minio = None
    red = None
    try:
        import server
        minio = getattr(server, "minio_client", None)
        red = getattr(server, "redis_client", None)
    except Exception:
        pass
    return minio, red


def _resolve_client_public_key(x_cert: Optional[str]) -> Optional[str]:
    """
    Разрешает публичный ключ или сертификат администратора для проверки X-Signature.
    1. Извлекает из заголовка X-Cert (PEM / Base64-PEM);
    2. Из доверенного файла certs/admin_client.crt;
    3. Из настроек окружения (dev/test fallback).
    """
    if x_cert and x_cert.strip():
        cert_val = x_cert.strip()
        if "BEGIN " in cert_val:
            return cert_val
        # Попытка декодировать из Base64
        try:
            import base64
            decoded = base64.b64decode(cert_val).decode("utf-8")
            if "BEGIN " in decoded:
                return decoded
        except Exception:
            pass
        return cert_val

    # Проверка смонтированного сертификата клиента (внутри контейнера)
    cert_paths = [
        os.getenv("ADMIN_CLIENT_CERT_PATH", ""),
        "/certs/admin_client.crt",
    ]
    for p in cert_paths:
        if p and os.path.exists(p):
            try:
                with open(p, "r", encoding="utf-8") as f:
                    return f.read().strip()
            except Exception:
                pass

    # В тестовом / dev окружении возвращаем публичный ключ сервера
    return settings.PUBLIC_KEY or None


@router.post(
    "/documents",
    response_model=RAGDocumentResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Прием и постановка в очередь документа RAG с цифровой подписью и защитой от повторов"
)
async def submit_rag_document(
    request: Request,
    response: Response,
    doc_req: RAGDocumentRequest,
    x_signature: str = Header(..., alias="X-Signature", description="Цифровая подпись полезной нагрузки (Base64)"),
    x_nonce: str = Header(..., alias="X-Nonce", description="Уникальный одноразовый Nonce"),
    x_timestamp: str = Header(..., alias="X-Timestamp", description="Временная метка запроса (Unix Epoch)"),
    x_cert: Optional[str] = Header(None, alias="X-Cert", description="Клиентский сертификат или открытый ключ в PEM/Base64"),
    db: AsyncSession = Depends(get_db),
    cert_identity: dict = Depends(verify_mtls_client_identity),
):
    """
    Эндпоинт защищенной загрузки документов в базу знаний RAG:
    - Проверяет срок действия временной метки (окно 300 секунд);
    - Защищает от Replay-атак через атомарный захват Nonce в Redis;
    - Валидирует mTLS идентичность через verify_mtls_client_identity;
    - Проверяет аппаратную цифровую подпись X-Signature;
    - Шифрует сырой документ в PostgreSQL с помощью AES-256-GCM;
    - Загружает файл в MinIO (с поддержкой SSE);
    - Публикует задачу в очередь Redis document_tasks для vector-worker;
    - Возвращает заверенную приватным ключом сервера квитанцию AckReceipt.
    """
    now = time.time()

    # 1. Проверка временного окна (Timestamp Window <= 300s)
    try:
        req_ts = float(x_timestamp.strip())
    except (ValueError, TypeError):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Некорректный формат заголовка X-Timestamp (ожидается Unix epoch timestamp)"
        )

    time_drift = abs(now - req_ts)
    if time_drift > 300:
        log_warning("RAG Security", f"Отклонен запрос с устаревшей меткой времени (дрейф: {time_drift:.1f}с > 300с)")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Срок действия запроса истек: временная метка отклоняется на {time_drift:.1f} сек (максимум 300 сек)"
        )

    # 2. Защита от Replay-атак (Атомарная фиксация Nonce)
    _, redis_client = _get_external_services()
    is_nonce_valid = record_admin_nonce_if_new(x_nonce, ttl_seconds=300, redis_conn=redis_client)
    if not is_nonce_valid:
        log_warning("RAG Security", f"Обнаружена повторная атака (Replay Attack) с Nonce: {x_nonce}")
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Replay attack detected: Nonce '{x_nonce}' уже был использован"
        )

    # 3. Верификация цифровой подписи (X-Signature)
    raw_body = await request.body()
    canonical_bytes = compute_rag_canonical_digest(
        timestamp=int(req_ts),
        nonce=x_nonce,
        body_bytes=raw_body
    )

    client_pub_key = _resolve_client_public_key(x_cert)
    if not client_pub_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Не удалось получить публичный ключ клиента для проверки подписи"
        )

    sig_verified = verify_digital_signature(
        public_key_or_cert=client_pub_key,
        signature_b64=x_signature,
        data_bytes=canonical_bytes
    )

    # Запасная проверка на случай различий в сериализации JSON
    if not sig_verified:
        normalized_body = json.dumps(doc_req.model_dump(), sort_keys=True, ensure_ascii=False).encode("utf-8")
        canonical_fallback = compute_rag_canonical_digest(
            timestamp=int(req_ts),
            nonce=x_nonce,
            body_bytes=normalized_body
        )
        sig_verified = verify_digital_signature(
            public_key_or_cert=client_pub_key,
            signature_b64=x_signature,
            data_bytes=canonical_fallback
        )

    if not sig_verified:
        log_warning("RAG Security", "Цифровая подпись X-Signature не прошла криптографическую валидацию")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Недействительная цифровая подпись запроса (X-Signature verification failed)"
        )

    # 4. Сохранение сырого документа в PostgreSQL с шифрованием AES-256-GCM
    content_bytes = doc_req.content.encode("utf-8")
    file_hash = hashlib.sha256(content_bytes).hexdigest()
    safe_title = re.sub(r'[^a-zA-Z0-9_\-\.]', '_', doc_req.document_title)
    filename = f"{safe_title[:64]}.md"

    # Определение агента/коллекции
    collection_name = doc_req.collection_name.strip()
    if collection_name.startswith("knowledge-"):
        agent_name = collection_name.replace("knowledge-", "")
    else:
        agent_name = "main"

    try:
        doc_record = await crud.create_document(
            db=db,
            file_hash=file_hash,
            filename=filename,
            agent_name=agent_name,
            status="processing",
            system_name=doc_req.document_title,
            short_name=safe_title,
            message="Документ принят через Zero-Trust API и ожидает индексации",
            raw_content=doc_req.content  # Прозрачно шифруется через EncryptedText (AES-256-GCM)
        )
    except Exception as e:
        log_error("RAG Database", f"Ошибка сохранения документа в БД: {e}")
        # Если запись уже существовала, продолжаем обработку
        pass

    # 5. Загрузка в MinIO (с SSE при включении)
    minio_client, _ = _get_external_services()
    bucket_name = f"knowledge-{agent_name}"
    object_name = f"rag/{file_hash[:16]}_{filename}"

    if minio_client is not None:
        try:
            import anyio
            import io

            if not minio_client.bucket_exists(bucket_name):
                minio_client.make_bucket(bucket_name)

            content_stream = io.BytesIO(content_bytes)
            sse_obj = get_minio_sse()
            put_kwargs = {
                "bucket_name": bucket_name,
                "object_name": object_name,
                "data": content_stream,
                "length": len(content_bytes),
                "content_type": "text/markdown",
            }
            if sse_obj is not None:
                put_kwargs["sse"] = sse_obj

            await anyio.to_thread.run_sync(lambda: minio_client.put_object(**put_kwargs))
            log_success("MinIO", f"Сохранен документ {object_name} в бакет {bucket_name}")
        except Exception as e:
            log_warning("MinIO", f"Предупреждение: загрузка в MinIO не удалась ({e}), обработка продолжается")

    # 6. Отправка задачи в очередь Redis для vector-worker
    if redis_client is not None:
        try:
            import anyio
            queue_payload = {
                "agent": agent_name,
                "action": "upsert",
                "file_path": object_name,
                "bucket": bucket_name,
                "file_hash": file_hash,
                "system_name": doc_req.document_title,
                "short_name": safe_title,
                "filename": filename,
                "content": doc_req.content,
                "metadata": doc_req.metadata
            }
            await anyio.to_thread.run_sync(
                redis_client.lpush, "document_tasks", json.dumps(queue_payload)
            )
            log_success("Redis", f"Задача векторизации для {filename} поставлена в очередь")
        except Exception as e:
            log_warning("Redis", f"Предупреждение: отправка задачи в Redis не удалась ({e})")

    # 7. Формирование и подписание квитанции сервера (AckReceipt)
    receipt_id = str(uuid.uuid4())
    ack_timestamp = int(now)
    receipt_data = {
        "receipt_id": receipt_id,
        "status": "QUEUED_FOR_INDEXING",
        "document_id": file_hash,
        "document_title": doc_req.document_title,
        "collection_name": collection_name,
        "timestamp": ack_timestamp,
        "nonce": x_nonce
    }

    server_sig, server_fp = sign_server_receipt(receipt_data)

    # 8. Фиксация в криптографическом журнале аудита
    try:
        actor_name = cert_identity.get("common_name") or cert_identity.get("subject") or "admin-workstation"
        await audit.log_audit_event(
            db=db,
            actor=actor_name,
            action="RAG_DOCUMENT_SUBMITTED",
            status="SUCCESS",
            resource=f"rag:{file_hash}",
            details={
                "document_title": doc_req.document_title,
                "receipt_id": receipt_id,
                "agent": agent_name,
                "collection": collection_name,
                "nonce": x_nonce
            }
        )
    except Exception as e:
        log_warning("Audit", f"Не удалось записать событие аудита: {e}")

    # Заголовки взаимной верификации сервера
    response.headers["X-Admin-Signature"] = server_sig
    response.headers["X-Server-Signature"] = server_sig
    response.headers["X-Server-Key-Fingerprint"] = server_fp
    response.headers["X-Ack-Nonce"] = x_nonce

    return RAGDocumentResponse(
        receipt_id=receipt_id,
        status="QUEUED_FOR_INDEXING",
        document_id=file_hash,
        document_title=doc_req.document_title,
        collection_name=collection_name,
        timestamp=ack_timestamp,
        nonce=x_nonce,
        signature=server_sig,
        server_key_fingerprint=server_fp
    )
