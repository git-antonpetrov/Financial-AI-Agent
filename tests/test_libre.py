import tempfile
import subprocess
import os
tmpdir = tempfile.mkdtemp()
profile_url = f"file:///{tmpdir.replace(chr(92), '/')}"
input_file = os.path.abspath("data/1_landing/fz_54.doc") 
print("Profile URL:", profile_url)
print("Input file:", input_file)
cmd = [
    "soffice",
    f"-env:UserInstallation={profile_url}",
    "--headless",
    "--convert-to", "pdf",
    "--outdir", os.path.abspath("data/1_landing"),
    input_file
]
print("Running:", cmd)
res = subprocess.run(cmd, capture_output=True, text=True)
print("STDOUT:", res.stdout)
print("STDERR:", res.stderr)
print("Code:", res.returncode)
