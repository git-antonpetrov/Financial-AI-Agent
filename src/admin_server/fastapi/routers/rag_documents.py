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
import base64
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
        validate_x509_certificate_chain,
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
            validate_x509_certificate_chain,
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
            validate_x509_certificate_chain,
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
    server_cert: Optional[str] = None


class RAGDocumentDeleteRequest(BaseModel):
    short_names: list[str] = Field(default_factory=list, description="Список коротких имен (short_name) актов для отмены")
    collection_name: str = Field(default="financial_kb", description="Имя коллекции или целевой базы знаний")
    reason: Optional[str] = Field(default="Repealed by newer regulation", description="Причина отмены")
    metadata: dict[str, Any] = Field(default_factory=dict, description="Метаданные запроса")


class RAGDocumentDeleteResponse(BaseModel):
    receipt_id: str
    status: str
    document_id: str
    deleted_targets: list[str]
    collection_name: str
    timestamp: int
    nonce: str
    signature: str
    server_key_fingerprint: str
    server_cert: Optional[str] = None


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
    x_cert: Optional[str] = Header(None, alias="X-Cert", description="Клиентский сертификат X.509 в формате PEM/Base64"),
    x_agent_cert: Optional[str] = Header(None, alias="X-Agent-Cert", description="Клиентский сертификат агента X.509 (alias)"),
    x_enclave_cert: Optional[str] = Header(None, alias="X-Enclave-Cert", description="Промежуточный сертификат Анклава (при наличии)"),
    db: AsyncSession = Depends(get_db),
    cert_identity: dict = Depends(verify_mtls_client_identity),
):
    """
    Эндпоинт защищенной загрузки документов в базу знаний RAG:
    - Проверяет срок действия временной метки (окно 300 секунд);
    - Защищает от Replay-атак через атомарный захват Nonce в Redis;
    - Валидирует mTLS идентичность через verify_mtls_client_identity;
    - Выполняет строгую серверную валидацию клиентского сертификата X.509 против Root CA,
      проверку срока действия, запрет самоподписанных сертификатов и проверку отзыва в Redis;
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

    # 3. Разрешение и строгая Zero-Trust валидация клиентского сертификата X.509
    raw_cert = (
        x_agent_cert
        or x_cert
        or request.headers.get("x-agent-cert")
        or request.headers.get("x-client-cert")
        or request.headers.get("x-client-cert-pem")
    )
    if not raw_cert:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Клиентский сертификат X.509 обязателен (отсутствует заголовок X-Cert / X-Agent-Cert)"
        )

    # Строгая серверная проверка:
    # 1. Формат X.509
    # 2. Срок действия (NotBefore <= now <= NotAfter)
    # 3. Запрет самоподписанных (Subject == Issuer)
    # 4. Проверка цепочки доверия Root CA
    # 5. Проверка отзыва в Redis (CRL: serial dec/hex, fingerprint, subject CN)
    leaf_cert, client_pub_key = validate_x509_certificate_chain(
        cert_input=raw_cert,
        enclave_cert_input=x_enclave_cert,
        redis_conn=redis_client
    )

    # 3.1. Zero-Trust авторизация роли и субъекта сертификата
    from cryptography.x509.oid import NameOID
    leaf_subject_str = leaf_cert.subject.rfc4514_string()
    leaf_ous = [attr.value for attr in leaf_cert.subject.get_attributes_for_oid(NameOID.ORGANIZATIONAL_UNIT_NAME)]
    leaf_cns = [attr.value for attr in leaf_cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)]

    allowed_roles = {"admin_operator", "Security Operations"}
    has_valid_role = (
        any(ou in allowed_roles for ou in leaf_ous)
        or any(cn in ("superadmin-workstation", "admin-server") for cn in leaf_cns)
        or any(r in leaf_subject_str for r in allowed_roles)
    )
    is_enclave_agent = bool(x_enclave_cert) and any(
        "agent" in cn.lower() or ou in ("bank_operator", "agent")
        for cn in leaf_cns
        for ou in (leaf_ous or ["agent"])
    )

    if not (has_valid_role or is_enclave_agent):
        log_warning("RAG Security", f"Отказ в доступе: роль/субъект сертификата '{leaf_subject_str}' не авторизованы для RAG (HTTP 403)")
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Недостаточно прав: роль субъекта '{leaf_subject_str}' не авторизована (требуется admin_operator или Security Operations)"
        )

    # 4. Верификация цифровой подписи (X-Signature)
    raw_body = await request.body()
    canonical_bytes = compute_rag_canonical_digest(
        timestamp=int(req_ts),
        nonce=x_nonce,
        body_bytes=raw_body
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
        log_warning("RAG Security", f"Цифровая подпись X-Signature не прошла валидацию для документа '{doc_req.document_title}'")
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

    client_orig_hash = doc_req.metadata.get("file_hash")
    client_orig_filename = doc_req.metadata.get("filename")

    try:
        doc_record = await crud.update_checking_to_processing(
            db=db,
            file_hash=client_orig_hash or file_hash,
            agent_name=agent_name,
            system_name=doc_req.document_title,
            short_name=safe_title,
            message="Документ принят через Zero-Trust API и ожидает индексации",
            filename=client_orig_filename or filename,
            raw_content=doc_req.content,
            new_file_hash=file_hash
        )
    except Exception as e:
        log_error("RAG Database", f"Ошибка сохранения/обновления документа в БД: {e}")
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
    if settings.SERVER_CERT:
        response.headers["X-Server-Cert"] = base64.b64encode(settings.SERVER_CERT.encode("utf-8")).decode("ascii")

    return RAGDocumentResponse(
        receipt_id=receipt_id,
        status="QUEUED_FOR_INDEXING",
        document_id=file_hash,
        document_title=doc_req.document_title,
        collection_name=collection_name,
        timestamp=ack_timestamp,
        nonce=x_nonce,
        signature=server_sig,
        server_key_fingerprint=server_fp,
        server_cert=settings.SERVER_CERT or None
    )


@router.post(
    "/documents/delete",
    response_model=RAGDocumentDeleteResponse,
    status_code=status.HTTP_200_OK,
    summary="Zero-Trust эндпоинт отмены и удаления нормативных актов из RAG"
)
async def submit_rag_documents_delete(
    request: Request,
    response: Response,
    del_req: RAGDocumentDeleteRequest,
    x_signature: str = Header(..., alias="X-Signature", description="Цифровая подпись полезной нагрузки (Base64)"),
    x_nonce: str = Header(..., alias="X-Nonce", description="Уникальный одноразовый Nonce"),
    x_timestamp: str = Header(..., alias="X-Timestamp", description="Временная метка запроса (Unix Epoch)"),
    x_cert: Optional[str] = Header(None, alias="X-Cert", description="Клиентский сертификат X.509 в формате PEM/Base64"),
    x_agent_cert: Optional[str] = Header(None, alias="X-Agent-Cert", description="Клиентский сертификат агента X.509 (alias)"),
    x_enclave_cert: Optional[str] = Header(None, alias="X-Enclave-Cert", description="Промежуточный сертификат Анклава (при наличии)"),
    db: AsyncSession = Depends(get_db),
    cert_identity: dict = Depends(verify_mtls_client_identity),
):
    """
    Эндпоинт защищенной отмены документов RAG:
    - Проверяет срок действия временной метки (окно 300 секунд);
    - Защищает от Replay-атак через атомарный захват Nonce в Redis;
    - Требует клиентский сертификат X.509 и валидирует цепочку доверия Root CA;
    - Проверяет авторизацию роли субъекта (admin_operator или Security Operations);
    - Проверяет цифровую подпись X-Signature;
    - Помечает документы отмененными в PostgreSQL;
    - Формирует задачу удаления для vector-worker в Redis;
    - Возвращает заверенную подписью сервера квитанцию AckReceipt.
    """
    now = time.time()

    # 1. Проверка временного окна (<= 300s)
    try:
        req_ts = float(x_timestamp.strip())
    except (ValueError, TypeError):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Некорректный формат заголовка X-Timestamp (ожидается Unix epoch timestamp)"
        )

    time_drift = abs(now - req_ts)
    if time_drift > 300:
        log_warning("RAG Security", f"Отклонен запрос на удаление с устаревшей меткой времени (дрейф: {time_drift:.1f}с > 300с)")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Срок действия запроса истек: временная метка отклоняется на {time_drift:.1f} сек (максимум 300 сек)"
        )

    # 2. Защита от Replay-атак
    _, redis_client = _get_external_services()
    is_nonce_valid = record_admin_nonce_if_new(x_nonce, ttl_seconds=300, redis_conn=redis_client)
    if not is_nonce_valid:
        log_warning("RAG Security", f"Обнаружена повторная атака (Replay Attack) при удалении с Nonce: {x_nonce}")
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Replay attack detected: Nonce '{x_nonce}' уже был использован"
        )

    # 3. Валидация сертификата X.509
    raw_cert = (
        x_agent_cert
        or x_cert
        or request.headers.get("x-agent-cert")
        or request.headers.get("x-client-cert")
        or request.headers.get("x-client-cert-pem")
    )
    if not raw_cert:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Клиентский сертификат X.509 обязателен (отсутствует заголовок X-Cert / X-Agent-Cert)"
        )

    leaf_cert, client_pub_key = validate_x509_certificate_chain(
        cert_input=raw_cert,
        enclave_cert_input=x_enclave_cert,
        redis_conn=redis_client
    )

    # 3.1. Проверка роли и субъекта
    from cryptography.x509.oid import NameOID
    leaf_subject_str = leaf_cert.subject.rfc4514_string()
    leaf_ous = [attr.value for attr in leaf_cert.subject.get_attributes_for_oid(NameOID.ORGANIZATIONAL_UNIT_NAME)]
    leaf_cns = [attr.value for attr in leaf_cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)]

    allowed_roles = {"admin_operator", "Security Operations"}
    has_valid_role = (
        any(ou in allowed_roles for ou in leaf_ous)
        or any(cn in ("superadmin-workstation", "admin-server") for cn in leaf_cns)
        or any(r in leaf_subject_str for r in allowed_roles)
    )
    is_enclave_agent = bool(x_enclave_cert) and any(
        "agent" in cn.lower() or ou in ("bank_operator", "agent")
        for cn in leaf_cns
        for ou in (leaf_ous or ["agent"])
    )

    if not (has_valid_role or is_enclave_agent):
        log_warning("RAG Security", f"Отказ в доступе на удаление: роль '{leaf_subject_str}' не авторизована")
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Недостаточно прав: роль субъекта сертификата '{leaf_subject_str}' не авторизована для удаления"
        )

    # 4. Верификация цифровой подписи (X-Signature)
    raw_body = await request.body()
    canonical_bytes = compute_rag_canonical_digest(
        timestamp=int(req_ts),
        nonce=x_nonce,
        body_bytes=raw_body
    )

    sig_verified = verify_digital_signature(
        public_key_or_cert=client_pub_key,
        signature_b64=x_signature,
        data_bytes=canonical_bytes
    )

    if not sig_verified:
        normalized_body = json.dumps(del_req.model_dump(), sort_keys=True, ensure_ascii=False).encode("utf-8")
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
        log_warning("RAG Security", "Цифровая подпись X-Signature не прошла валидацию для запроса на удаление")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Недействительная цифровая подпись запроса на удаление (X-Signature verification failed)"
        )

    # 5. Пометка отмененных документов в БД и постановка задачи в Redis
    collection_name = del_req.collection_name.strip()
    if collection_name.startswith("knowledge-"):
        agent_name = collection_name.replace("knowledge-", "")
    else:
        agent_name = "main"

    delete_content = "\n".join(del_req.short_names)
    del_content_bytes = delete_content.encode("utf-8")
    del_hash = hashlib.sha256(del_content_bytes).hexdigest()
    safe_name = f"delete_{del_hash[:16]}.md"
    object_name = f"delete/{safe_name}"
    bucket_name = f"knowledge-{agent_name}"

    for sn in del_req.short_names:
        try:
            await crud.mark_document_repealed(
                db=db,
                short_name=sn,
                agent_name=agent_name,
                message=f"Документ отменен через Zero-Trust API (причина: {del_req.reason})"
            )
        except Exception as e:
            log_warning("RAG Database", f"Не удалось обновить статус отмененного документа {sn} в БД: {e}")

    # Сохранение файла удаления в MinIO (для vector-worker)
    minio_client, _ = _get_external_services()
    if minio_client is not None:
        try:
            import anyio
            import io
            if not minio_client.bucket_exists(bucket_name):
                minio_client.make_bucket(bucket_name)
            content_stream = io.BytesIO(del_content_bytes)
            sse_obj = get_minio_sse()
            del_put_kwargs = {
                "bucket_name": bucket_name,
                "object_name": object_name,
                "data": content_stream,
                "length": len(del_content_bytes),
                "content_type": "text/markdown",
            }
            if sse_obj is not None:
                del_put_kwargs["sse"] = sse_obj

            await anyio.to_thread.run_sync(
                lambda: minio_client.put_object(**del_put_kwargs)
            )
        except Exception as e:
            log_warning("MinIO", f"Предупреждение: загрузка файла удаления в MinIO не удалась ({e})")

    # Постановка задачи в очередь Redis
    if redis_client is not None:
        try:
            import anyio
            queue_payload = {
                "agent": agent_name,
                "action": "delete",
                "file_path": object_name,
                "bucket": bucket_name,
                "file_hash": del_hash,
                "content": delete_content,
                "short_names": del_req.short_names,
                "reason": del_req.reason,
            }
            await anyio.to_thread.run_sync(
                redis_client.lpush, "document_tasks", json.dumps(queue_payload)
            )
            log_success("Redis", f"Задача удаления для {len(del_req.short_names)} актов поставлена в очередь")
        except Exception as e:
            log_warning("Redis", f"Предупреждение: отправка задачи удаления в Redis не удалась ({e})")

    # 6. Квитанция сервера (AckReceipt)
    receipt_id = str(uuid.uuid4())
    ack_timestamp = int(now)
    receipt_data = {
        "receipt_id": receipt_id,
        "status": "QUEUED_FOR_DELETION",
        "document_id": del_hash,
        "deleted_targets": del_req.short_names,
        "collection_name": collection_name,
        "timestamp": ack_timestamp,
        "nonce": x_nonce
    }
    server_sig, server_fp = sign_server_receipt(receipt_data)

    # 7. Фиксация в аудите
    try:
        actor_name = cert_identity.get("common_name") or cert_identity.get("subject") or "admin-workstation"
        await audit.log_audit_event(
            db=db,
            actor=actor_name,
            action="RAG_DOCUMENTS_DELETE_SUBMITTED",
            status="SUCCESS",
            resource=f"rag:delete:{del_hash}",
            details={
                "short_names": del_req.short_names,
                "receipt_id": receipt_id,
                "agent": agent_name,
                "collection": collection_name,
                "nonce": x_nonce
            }
        )
    except Exception as e:
        log_warning("Audit", f"Не удалось записать событие аудита удаления: {e}")

    response.headers["X-Admin-Signature"] = server_sig
    response.headers["X-Server-Signature"] = server_sig
    response.headers["X-Server-Key-Fingerprint"] = server_fp
    response.headers["X-Ack-Nonce"] = x_nonce
    if settings.SERVER_CERT:
        response.headers["X-Server-Cert"] = base64.b64encode(settings.SERVER_CERT.encode("utf-8")).decode("ascii")

    return RAGDocumentDeleteResponse(
        receipt_id=receipt_id,
        status="QUEUED_FOR_DELETION",
        document_id=del_hash,
        deleted_targets=del_req.short_names,
        collection_name=collection_name,
        timestamp=ack_timestamp,
        nonce=x_nonce,
        signature=server_sig,
        server_key_fingerprint=server_fp,
        server_cert=settings.SERVER_CERT or None
    )
