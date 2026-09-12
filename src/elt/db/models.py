import uuid
from typing import Optional
from datetime import date, time, datetime
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.dialects.postgresql import UUID

class Base(DeclarativeBase):
    pass

class ExtractState(Base):
    __tablename__ = "extract_state"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    source: Mapped[str] = mapped_column()
    source_url: Mapped[str] = mapped_column(unique=True)
    file_name: Mapped[str] = mapped_column()
    pub_date: Mapped[date] = mapped_column()
    download_date: Mapped[date] = mapped_column()
    download_start_time: Mapped[Optional[time]] = mapped_column(nullable=True)
    download_end_time: Mapped[Optional[time]] = mapped_column(nullable=True)
    status: Mapped[str] = mapped_column()
    error_message: Mapped[Optional[str]] = mapped_column(nullable=True)
    error_count: Mapped[int] = mapped_column(default=0)

class LoadState(Base):
    __tablename__ = "load_state"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    raw_file_name: Mapped[str] = mapped_column(unique=True)  # Сделаем уникальным для удобства поиска
    md5_hash: Mapped[str] = mapped_column()
    system_name: Mapped[Optional[str]] = mapped_column(nullable=True)
    is_relevant: Mapped[Optional[bool]] = mapped_column(nullable=True)
    official_name: Mapped[Optional[str]] = mapped_column(nullable=True)
    sign_date: Mapped[Optional[date]] = mapped_column(nullable=True)
    short_number: Mapped[Optional[str]] = mapped_column(nullable=True)
    repealed_docs: Mapped[Optional[str]] = mapped_column(nullable=True)
    processing_start_date: Mapped[date] = mapped_column()
    processing_start_time: Mapped[time] = mapped_column()
    processing_end_date: Mapped[Optional[date]] = mapped_column(nullable=True)
    processing_end_time: Mapped[Optional[time]] = mapped_column(nullable=True)
    status: Mapped[str] = mapped_column()
    error_message: Mapped[Optional[str]] = mapped_column(nullable=True)
    error_count: Mapped[int] = mapped_column(default=0)

class TransformState(Base):
    __tablename__ = "transform_state"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    file_name: Mapped[str] = mapped_column(unique=True) # Имя файла (с префиксом delete/ или upsert/)
    transform_type: Mapped[str] = mapped_column() # "upsert" или "delete"
    status: Mapped[str] = mapped_column()
    error_message: Mapped[Optional[str]] = mapped_column(nullable=True)
    error_count: Mapped[int] = mapped_column(default=0)
    started_at: Mapped[datetime] = mapped_column()
    completed_at: Mapped[Optional[datetime]] = mapped_column(nullable=True)

class OrchestratorRun(Base):
    __tablename__ = "orchestrator_runs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    run_date: Mapped[date] = mapped_column(unique=True) # Только один запуск в день
    started_at: Mapped[datetime] = mapped_column()
    completed_at: Mapped[Optional[datetime]] = mapped_column(nullable=True)
    has_new_files: Mapped[bool] = mapped_column(default=False)
