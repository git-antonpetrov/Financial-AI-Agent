#!/usr/bin/env python3
"""
Скрипт первоначальной настройки и привязки двухфакторной аутентификации 2FA (RFC 6238 TOTP).
Позволяет:
- Сгенерировать новый секретный ключ или отобразить QR-код для уже настроенного ключа из .env;
- Вывести QR-код прямо в терминал для сканирования мобильным приложением (Google Authenticator, Apple Passwords, YubiKey);
- Сохранить автономный HTML-файл с векторным SVG QR-кодом для открытия в браузере;
- В интерактивном режиме проверить 6-значный код из аутентификатора;
- Автоматически сохранить или обновить переменную ADMIN_TOTP_SECRET в .env.
"""

import os
import sys
import argparse
import base64
import secrets
import io

# Настройка UTF-8 для корректного вывода символов QR-кода в консоли Windows
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

# Подключаем модули безопасности проекта
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if project_root not in sys.path:
    sys.path.insert(0, project_root)
fastapi_dir = os.path.join(project_root, "src", "admin_server", "fastapi")
if fastapi_dir not in sys.path:
    sys.path.insert(0, fastapi_dir)

try:
    from security import (
        generate_totp_secret,
        get_totp_uri,
        verify_totp_code,
        generate_qr_svg,
        settings,
    )
except ImportError:
    # Автономный fallback при запуске вне окружения проекта
    def generate_totp_secret() -> str:
        return base64.b32encode(secrets.token_bytes(20)).decode("utf-8").rstrip("=")

    def get_totp_uri(secret: str, username: str = "admin", issuer: str = "Financial-AI-Agent") -> str:
        import urllib.parse
        enc_issuer = urllib.parse.quote(issuer)
        enc_user = urllib.parse.quote(username)
        return f"otpauth://totp/{enc_issuer}:{enc_user}?secret={secret}&issuer={enc_issuer}&algorithm=SHA1&digits=6&period=30"

    def verify_totp_code(secret: str, code: str, window: int = 1) -> bool:
        import time, hmac, hashlib, struct
        code = (code or "").strip()
        if len(code) != 6 or not code.isdigit():
            return False
        padding = "=" * ((8 - len(secret) % 8) % 8)
        key = base64.b32decode((secret + padding).upper(), casefold=True)
        now = time.time()
        current_counter = int(now // 30)
        for drift in range(-window, window + 1):
            msg = struct.pack(">Q", current_counter + drift)
            h = hmac.new(key, msg, hashlib.sha1).digest()
            offset = h[19] & 0x0F
            val = (struct.unpack(">I", h[offset:offset+4])[0] & 0x7FFFFFFF) % 1000000
            if hmac.compare_digest(f"{val:06d}", code):
                return True
        return False

    def generate_qr_svg(uri: str) -> str:
        try:
            import qrcode, qrcode.image.svg
            img = qrcode.make(uri, image_factory=qrcode.image.svg.SvgPathImage)
            s = io.BytesIO()
            img.save(s)
            return s.getvalue().decode("utf-8")
        except Exception:
            return ""


def print_terminal_qr(uri: str):
    """Выводит QR-код псевдографикой в консоль."""
    try:
        import qrcode
        qr = qrcode.QRCode(border=2)
        qr.add_data(uri)
        qr.print_ascii(invert=True)
    except Exception as e:
        print(f"[!] Не удалось отрисовать QR в терминале ({e}). Используйте HTML-файл или текстовый ключ.")


def format_secret_chunks(secret: str) -> str:
    """Форматирует ключ блоками по 4 символа для удобного ручного ввода."""
    return " ".join([secret[i:i+4] for i in range(0, len(secret), 4)])


def save_html_qr(uri: str, secret: str, output_path: str = "admin_2fa_qr.html"):
    """Создает автономный HTML-файл с векторным SVG QR-кодом для открытия в браузере."""
    svg_data = generate_qr_svg(uri)
    html_content = f"""<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Настройка 2FA: Financial-AI-Agent</title>
    <style>
        body {{
            background: #0f0a1c;
            color: #f3f4f6;
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
            display: flex;
            align-items: center;
            justify-content: center;
            min-height: 100vh;
            margin: 0;
            padding: 20px;
        }}
        .card {{
            background: #1a1333;
            border: 1px solid rgba(139, 92, 246, 0.3);
            border-radius: 16px;
            padding: 32px;
            max-width: 480px;
            width: 100%;
            text-align: center;
            box-shadow: 0 20px 25px -5px rgba(0, 0, 0, 0.5);
        }}
        h1 {{
            font-size: 20px;
            margin-bottom: 8px;
            color: #a78bfa;
        }}
        p {{
            color: #9ca3af;
            font-size: 14px;
            line-height: 1.5;
            margin-bottom: 24px;
        }}
        .qr-box {{
            background: #ffffff;
            padding: 16px;
            border-radius: 12px;
            display: inline-block;
            margin-bottom: 24px;
        }}
        .qr-box svg {{
            display: block;
            width: 220px;
            height: 220px;
        }}
        .secret-box {{
            background: #0f0a1c;
            border: 1px solid rgba(255, 255, 255, 0.1);
            border-radius: 8px;
            padding: 12px;
            margin-bottom: 16px;
            word-break: break-all;
            font-family: monospace;
            font-size: 16px;
            color: #38bdf8;
            letter-spacing: 2px;
        }}
        .link-btn {{
            display: inline-block;
            background: #7c3aed;
            color: white;
            text-decoration: none;
            padding: 10px 20px;
            border-radius: 8px;
            font-size: 14px;
            font-weight: 500;
            transition: background 0.2s;
        }}
        .link-btn:hover {{
            background: #6d28d9;
        }}
        .note {{
            font-size: 12px;
            color: #6b7280;
            margin-top: 20px;
        }}
    </style>
</head>
<body>
    <div class="card">
        <h1>Двухфакторная аутентификация</h1>
        <p>Отсканируйте QR-код в приложении аутентификатора (Google Authenticator, Apple Passwords, YubiKey, 1Password).</p>
        <div class="qr-box">
            {svg_data}
        </div>
        <div style="font-size: 12px; color: #9ca3af; margin-bottom: 6px;">Ключ для ручного ввода:</div>
        <div class="secret-box">{format_secret_chunks(secret)}</div>
        <a href="{uri}" class="link-btn">Открыть в приложении на этом ПК</a>
        <div class="note">После привязки удалите этот HTML-файл для безопасности.</div>
    </div>
</body>
</html>"""
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html_content)
    return output_path


def update_env_file(secret: str, env_path: str = ".env"):
    """Обновляет или добавляет ADMIN_TOTP_SECRET и REQUIRE_2FA в файл .env."""
    lines = []
    found_secret = False
    found_require = False
    if os.path.exists(env_path):
        with open(env_path, "r", encoding="utf-8") as f:
            lines = f.readlines()

    new_lines = []
    for line in lines:
        if line.startswith("ADMIN_TOTP_SECRET="):
            new_lines.append(f"ADMIN_TOTP_SECRET={secret}\n")
            found_secret = True
        elif line.startswith("REQUIRE_2FA="):
            new_lines.append("REQUIRE_2FA=true\n")
            found_require = True
        else:
            new_lines.append(line)

    if not found_secret:
        new_lines.append(f"ADMIN_TOTP_SECRET={secret}\n")
    if not found_require:
        new_lines.append("REQUIRE_2FA=true\n")

    with open(env_path, "w", encoding="utf-8") as f:
        f.writelines(new_lines)
    print(f"[+] Файл {env_path} успешно обновлен (ADMIN_TOTP_SECRET, REQUIRE_2FA=true)")


def main():
    parser = argparse.ArgumentParser(description="Настройка 2FA (TOTP RFC 6238) для Financial-AI-Agent")
    parser.add_argument("--new", action="store_true", help="Сгенерировать новый секретный ключ принудительно")
    parser.add_argument("--secret", type=str, default="", help="Использовать конкретный секретный Base32-ключ")
    parser.add_argument("--verify", type=str, default="", help="Проверить 6-значный код и завершить работу")
    parser.add_argument("--no-save", action="store_true", help="Не предлагать сохранение в .env")
    parser.add_argument("--html", type=str, default="admin_2fa_qr.html", help="Путь для сохранения HTML с QR-кодом")
    args = parser.parse_args()

    # Режим быстрой проверки кода
    if args.verify:
        secret = args.secret or os.getenv("ADMIN_TOTP_SECRET", "")
        if not secret:
            print("[!] Ошибка: укажите --secret для проверки кода.")
            sys.exit(1)
        if verify_totp_code(secret, args.verify):
            print("[+] Код верен! (VALID)")
            sys.exit(0)
        else:
            print("[-] Код неверен или просрочен! (INVALID)")
            sys.exit(1)

    # Определение секрета
    env_secret = os.getenv("ADMIN_TOTP_SECRET", "").strip()
    # Если .env существует, проверим значение там
    env_path = os.path.join(project_root, ".env")
    if not env_secret and os.path.exists(env_path):
        with open(env_path, "r", encoding="utf-8") as f:
            for line in f:
                if line.startswith("ADMIN_TOTP_SECRET="):
                    env_secret = line.split("=", 1)[1].strip()
                    break

    secret = ""
    if args.secret:
        secret = args.secret.strip().replace(" ", "").upper()
    elif args.new or not env_secret:
        secret = generate_totp_secret()
        print("[*] Сгенерирован новый 160-битный TOTP секрет.")
    else:
        print(f"[*] Обнаружен настроенный секрет в конфигурации (.env).")
        secret = env_secret

    uri = get_totp_uri(secret, username="admin", issuer="Financial-AI-Agent")

    print("\n" + "=" * 60)
    print("      НАСТРОЙКА ДВУХФАКТОРНОЙ АУТЕНТИФИКАЦИИ (2FA TOTP)")
    print("=" * 60)
    print("\n1. Отсканируйте QR-код в приложении аутентификатора:")
    print_terminal_qr(uri)

    print("\n2. Ключ для ручного ввода (если нет камеры):")
    print(f"   --> {format_secret_chunks(secret)}")

    print("\n3. Ссылка для приложений аутентификации (otpauth):")
    print(f"   --> {uri}")

    html_file = save_html_qr(uri, secret, os.path.join(project_root, args.html))
    print(f"\n4. Создан HTML-файл с QR-кодом для открытия в браузере:")
    print(f"   --> {html_file}")

    print("\n" + "-" * 60)

    # Интерактивная проверка
    if sys.stdin.isatty():
        try:
            user_code = input("Введите 6-значный код из аутентификатора для проверки (или Enter для пропуска): ").strip()
            if user_code:
                if verify_totp_code(secret, user_code):
                    print("\n[+] УСПЕХ: 6-значный код подтвержден! Аутентификатор настроен корректно.")
                else:
                    print("\n[-] ОШИБКА: Код неверен. Проверьте синхронизацию часов на телефоне и компьютере.")
        except (KeyboardInterrupt, EOFError):
            print("\n")

        # Сохранение в .env
        if not args.no_save and secret != env_secret:
            try:
                ans = input(f"\nСохранить этот секрет в {env_path}? [Y/n]: ").strip().lower()
                if ans in ("", "y", "yes", "д", "да"):
                    update_env_file(secret, env_path)
            except (KeyboardInterrupt, EOFError):
                pass
    else:
        if not args.no_save and secret != env_secret and not env_secret:
            update_env_file(secret, env_path)

    print("\nГотово. Теперь при входе в систему используйте 6-значные коды из приложения.\n")


if __name__ == "__main__":
    main()
