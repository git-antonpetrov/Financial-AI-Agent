"""Проверяет единые версии прямых зависимостей во всех requirements.txt."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

REQUIREMENT_FILES = (
    ROOT / "requirements.txt",
    ROOT / "scripts" / "host-requirements.txt",
    ROOT / "src" / "pki" / "requirements.txt",
    ROOT / "src" / "admin_client" / "backend" / "requirements.txt",
    ROOT / "src" / "admin_server" / "fastapi" / "requirements.txt",
    ROOT / "src" / "admin_server" / "vectors" / "requirements.txt",
    ROOT / "src" / "simulations" / "api" / "requirements.txt",
    ROOT / "src" / "simulations" / "workers" / "bank_eod" / "requirements.txt",
    ROOT / "src" / "simulations" / "workers" / "invest_eod" / "requirements.txt",
    ROOT / "src" / "simulations" / "workers" / "digital_eod" / "requirements.txt",
)


def requirement_specs(path: Path) -> dict[str, str]:
    """Возвращает имя пакета и закреплённую версию из файла зависимостей.

    Аргументы:
        path: Путь к requirements.txt.

    Возвращает:
        Словарь, где ключ — имя пакета без extras, а значение — строка версии.

    Вызывает:
        AssertionError: Строка не закрепляет версию через == или пакет повторён.
    """
    specs: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line:
            continue
        assert "==" in line, f"{path.name}: диапазон вместо точной версии: {line}"
        name, version = line.split("==", 1)
        base_name = name.split("[", 1)[0].strip().lower()
        assert base_name not in specs, f"{path}: пакет {base_name} указан дважды"
        specs[base_name] = version.strip()
    return specs


def test_requirements_share_one_exact_version() -> None:
    """Проверяет, что одна библиотека имеет одну версию во всех сервисах и в корне."""
    root_specs = requirement_specs(REQUIREMENT_FILES[0])
    assert list(root_specs) == sorted(root_specs)

    seen: dict[str, str] = {}
    for path in REQUIREMENT_FILES:
        specs = requirement_specs(path)
        assert list(specs) == sorted(specs), f"{path} не отсортирован по имени пакета"
        assert set(specs) <= set(root_specs), f"{path} содержит пакет, которого нет в корне"
        for name, version in specs.items():
            assert root_specs[name] == version, f"{path}: {name}=={version}, в корне {root_specs[name]}"
            previous = seen.get(name)
            assert previous in (None, version), f"{name}: {previous} и {version}"
            seen[name] = version

    assert seen.keys() == root_specs.keys()


def test_service_requirements_cover_direct_imports() -> None:
    """Проверяет пакеты, без которых процесс сервиса падает на импорте."""
    api = requirement_specs(ROOT / "src" / "simulations" / "api" / "requirements.txt")
    bank = requirement_specs(ROOT / "src" / "simulations" / "workers" / "bank_eod" / "requirements.txt")
    invest = requirement_specs(ROOT / "src" / "simulations" / "workers" / "invest_eod" / "requirements.txt")
    digital = requirement_specs(ROOT / "src" / "simulations" / "workers" / "digital_eod" / "requirements.txt")

    assert "redis" in api
    assert "cryptography" in bank
    assert "cryptography" in invest
    assert "cryptography" in digital
    assert "httpx" not in digital
