"""Проверяет advisory lock вне пула и пропуск закрытия дня без этого лока."""

import asyncio
import importlib
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.simulations.workers import advisory_lock
from src.simulations.workers.advisory_lock import acquire_advisory_lock, release_advisory_lock


class _Engine:
    """Подменяет AsyncEngine и открывает одно соединение."""

    def __init__(self, connection) -> None:
        self.connection = connection

    def connect(self):
        """Возвращает соединение, которое не сдаётся в пул сессии."""

        async def _open():
            return self.connection

        return _open()


class _Session:
    """Держит bind и фиксирует вызовы scalar, execute и commit."""

    def __init__(self, engine=None) -> None:
        self.engine = engine
        self.scalar = AsyncMock()
        self.execute = AsyncMock()
        self.commit = AsyncMock()

    def get_bind(self):
        """Возвращает движок, если тесту нужно отдельное соединение."""
        if self.engine is None:
            raise RuntimeError("bind недоступен")
        return self.engine


class _Scope:
    """Открывает одну сессию как асинхронный контекст фабрики EOD."""

    def __init__(self, session) -> None:
        self.session = session

    async def __aenter__(self):
        return self.session

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        return False


def _install_engine(monkeypatch) -> None:
    """Подменяет класс движка, чтобы isinstance узнал тестовый bind."""
    monkeypatch.setattr(advisory_lock, "AsyncEngine", _Engine)


def test_acquire_keeps_connection_when_lock_is_granted(monkeypatch):
    """Оставляет соединение открытым, если PostgreSQL отдал lock."""

    async def _test():
        _install_engine(monkeypatch)
        connection = AsyncMock()
        connection.scalar = AsyncMock(return_value=True)
        session = _Session(_Engine(connection))

        holder, locked = await acquire_advisory_lock(session, 84920481)

        assert holder is connection
        assert locked is True
        assert connection.close.await_count == 0
        session.scalar.assert_not_called()
        statement, params = connection.scalar.await_args.args
        assert "pg_try_advisory_lock" in str(statement)
        assert params == {"lock_id": 84920481}

    asyncio.run(_test())


def test_acquire_closes_connection_when_lock_is_busy(monkeypatch):
    """Закрывает соединение и сообщает отказ, если lock уже занят."""

    async def _test():
        _install_engine(monkeypatch)
        connection = AsyncMock()
        connection.scalar = AsyncMock(return_value=False)
        session = _Session(_Engine(connection))

        holder, locked = await acquire_advisory_lock(session, 84920482)

        assert holder is None
        assert locked is False
        connection.close.assert_awaited_once()
        connection.commit.assert_not_called()

    asyncio.run(_test())


def test_acquire_closes_connection_when_database_fails(monkeypatch):
    """Закрывает соединение и пробрасывает ошибку запроса lock."""

    async def _test():
        _install_engine(monkeypatch)
        connection = AsyncMock()
        connection.scalar = AsyncMock(side_effect=RuntimeError("соединение оборвалось"))
        session = _Session(_Engine(connection))

        with pytest.raises(RuntimeError, match="соединение оборвалось"):
            await acquire_advisory_lock(session, 84920483)

        connection.close.assert_awaited_once()

    asyncio.run(_test())


def test_acquire_uses_session_when_bind_is_missing():
    """Берёт lock самой сессией, если отдельного движка нет."""

    async def _test():
        session = _Session(None)
        session.scalar = AsyncMock(return_value=True)

        holder, locked = await acquire_advisory_lock(session, 15)

        assert holder is None
        assert locked is True
        statement, params = session.scalar.await_args.args
        assert "pg_try_advisory_lock" in str(statement)
        assert params == {"lock_id": 15}

    asyncio.run(_test())


def test_release_unlocks_dedicated_connection():
    """Снимает lock с отдельного соединения и закрывает его."""

    async def _test():
        connection = AsyncMock()
        session = _Session(None)

        await release_advisory_lock(connection, session, 84920481)

        statement, params = connection.execute.await_args.args
        assert "pg_advisory_unlock" in str(statement)
        assert params == {"lock_id": 84920481}
        connection.commit.assert_awaited_once()
        connection.close.assert_awaited_once()
        session.execute.assert_not_called()
        session.commit.assert_not_called()

    asyncio.run(_test())


def test_release_unlocks_session_when_it_holds_the_lock():
    """Снимает lock через сессию, если отдельного соединения не было."""

    async def _test():
        session = _Session(None)

        await release_advisory_lock(None, session, 22)

        statement, params = session.execute.await_args.args
        assert "pg_advisory_unlock" in str(statement)
        assert params == {"lock_id": 22}
        session.commit.assert_awaited_once()

    asyncio.run(_test())


def _runners():
    """Возвращает точки входа EOD, номер лока и шаги, которые он запускает."""
    return [
        (
            "src.simulations.workers.bank_eod.tasks",
            "run_bank_eod",
            84920481,
            ("process_auto_payments", "process_tariff_fees", "process_salaries"),
        ),
        (
            "src.simulations.workers.invest_eod.tasks",
            "run_invest_eod",
            84920482,
            ("process_savings_accounts", "process_deposits", "process_broker_accounts"),
        ),
        (
            "src.simulations.workers.digital_eod.tasks",
            "run_ruble_eod",
            84920483,
            ("process_smart_contracts",),
        ),
    ]


def _patch_runner(monkeypatch, module_path: str, session):
    """Подменяет фабрику сессии и захват лока у одного воркера."""
    module = importlib.import_module(module_path)
    monkeypatch.setattr(module, "AsyncSessionLocal", lambda: _Scope(session))
    acquire = AsyncMock()
    release = AsyncMock()
    monkeypatch.setattr(module, "acquire_advisory_lock", acquire)
    monkeypatch.setattr(module, "release_advisory_lock", release)
    return module, acquire, release


@pytest.mark.parametrize(("module_path", "runner_name", "lock_id", "steps"), _runners())
def test_eod_runs_steps_and_releases_lock(monkeypatch, module_path, runner_name, lock_id, steps):
    """Проводит шаги закрытия дня и снимает lock после них."""

    async def _test():
        session = MagicMock()
        module, acquire, release = _patch_runner(monkeypatch, module_path, session)
        connection = AsyncMock()
        acquire.return_value = (connection, True)
        called = []
        for name in steps:
            monkeypatch.setattr(module, name, AsyncMock(side_effect=lambda *args, name=name: called.append(name)))

        await getattr(module, runner_name)()

        acquire.assert_awaited_once_with(session, lock_id)
        assert called == list(steps)
        release.assert_awaited_once_with(connection, session, lock_id)

    asyncio.run(_test())


@pytest.mark.parametrize(("module_path", "runner_name", "lock_id", "steps"), _runners())
def test_eod_skips_when_lock_is_busy(monkeypatch, module_path, runner_name, lock_id, steps):
    """Не проводит день и не снимает lock, если его уже держит другой процесс."""

    async def _test():
        session = MagicMock()
        module, acquire, release = _patch_runner(monkeypatch, module_path, session)
        acquire.return_value = (None, False)
        watches = []
        for name in steps:
            watch = AsyncMock()
            watches.append(watch)
            monkeypatch.setattr(module, name, watch)

        await getattr(module, runner_name)()

        acquire.assert_awaited_once_with(session, lock_id)
        for watch in watches:
            watch.assert_not_called()
        release.assert_not_called()

    asyncio.run(_test())


@pytest.mark.parametrize(("module_path", "runner_name", "lock_id", "steps"), _runners())
def test_eod_releases_lock_when_step_fails(monkeypatch, module_path, runner_name, lock_id, steps):
    """Снимает lock, если шаг закрытия дня падает, и не запускает следующие шаги."""

    async def _test():
        session = MagicMock()
        module, acquire, release = _patch_runner(monkeypatch, module_path, session)
        connection = AsyncMock()
        acquire.return_value = (connection, True)
        monkeypatch.setattr(module, steps[0], AsyncMock(side_effect=RuntimeError("шаг закрытия дня упал")))
        rest = []
        for name in steps[1:]:
            watch = AsyncMock()
            rest.append(watch)
            monkeypatch.setattr(module, name, watch)

        with pytest.raises(RuntimeError, match="шаг закрытия дня упал"):
            await getattr(module, runner_name)()

        release.assert_awaited_once_with(connection, session, lock_id)
        for watch in rest:
            watch.assert_not_called()

    asyncio.run(_test())
