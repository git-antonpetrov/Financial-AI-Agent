# Cursor в этом репозитории

Навыки лежат в `.agents/skills`. Эта папка должна быть ссылкой на них, чтобы Cursor подхватил те же файлы и они не разъехались.

Windows не дал создать символическую ссылку без права разработчика, поэтому на этой машине стоит junction. После клонирования на другой машине ссылку создают из корня репозитория:

```bat
mklink /J .cursor\skills .agents\skills
```

На macOS и Linux:

```bash
ln -s ../.agents/skills .cursor/skills
```

Субагенты лежат в `.cursor/agents`. Правило `.cursor/rules/project.mdc` включено всегда.
