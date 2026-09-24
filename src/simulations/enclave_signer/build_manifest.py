import sys
import os

def main():
    # Указываем на наш бинарник внутри анклава
    entrypoint = "/app/signer"
    
    # Архитектурно-зависимая директория (для Ubuntu это обычно /lib/x86_64-linux-gnu)
    arch_libdir = "/lib/x86_64-linux-gnu"
    
    # Собираем все директории, которые нужны Python для работы (стандартные библиотеки + site-packages)
    python_paths = set()
    for p in sys.path:
        if p and os.path.exists(p) and os.path.isdir(p):
            # Избегаем монтирования корня или лишних системных папок
            if p not in ("/", "/usr", "/usr/lib", "/usr/local"):
                python_paths.add(p)

    # Формируем шаблон манифеста
    manifest = f"""libos.entrypoint = "{entrypoint}"

loader.argv = ["signer", "/app/signer.py"]
loader.log_level = "error"
loader.env.LD_LIBRARY_PATH = "/lib:/lib/x86_64-linux-gnu:/usr/lib/x86_64-linux-gnu"
loader.env.ENCLAVE_MASTER_KEY = {{ passthrough = true }}

sys.enable_sigterm_injection = true

fs.start_dir = "/app"
fs.mounts = [
  {{ path = "/lib", uri = "file:/usr/lib/x86_64-linux-gnu/gramine/runtime/glibc" }},
  {{ path = "{arch_libdir}", uri = "file:{arch_libdir}" }},
  {{ path = "/usr{arch_libdir}", uri = "file:/usr{arch_libdir}" }},
  {{ path = "/etc", uri = "file:/etc" }},
  {{ path = "/app", uri = "file:/app" }},
  {{ path = "/certs", uri = "file:/certs" }},
"""

    # Добавляем все директории Python в mounts
    for path in sorted(python_paths):
        manifest += f'  {{ path = "{path}", uri = "file:{path}" }},\n'

    manifest += "]\n\nsgx.enclave_size = \"256M\"\nsgx.max_threads = 32\nsgx.trusted_files = [\n"
    manifest += '  "file:/usr/lib/x86_64-linux-gnu/gramine/libsysdb.so",\n'
    manifest += '  "file:/usr/lib/x86_64-linux-gnu/gramine/runtime/glibc/",\n'
    manifest += f'  "file:{arch_libdir}/",\n'
    manifest += f'  "file:/usr{arch_libdir}/",\n'
    manifest += '  "file:/app/",\n'

    # Добавляем все директории Python в trusted_files
    for path in sorted(python_paths):
        manifest += f'  "file:{path}/",\n'

    manifest += "]\n\n"
    manifest += "sgx.allowed_files = [\n"
    manifest += '  "file:/certs/",\n'
    manifest += "]\n"

    # Сохраняем шаблон манифеста
    with open("signer.manifest.template", "w") as f:
        f.write(manifest)
    
    print("Шаблон манифеста Gramine (signer.manifest.template) успешно сгенерирован!")

if __name__ == "__main__":
    main()
