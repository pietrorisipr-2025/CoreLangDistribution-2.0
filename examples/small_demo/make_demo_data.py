from pathlib import Path
import shutil

ROOT = Path(__file__).resolve().parent
for name in ["release_v1", "release_v2", "install_v2", "cache"]:
    p = ROOT / name
    if p.exists():
        shutil.rmtree(p)

v1 = ROOT / "release_v1"
v2 = ROOT / "release_v2"
v1.mkdir(parents=True)
v2.mkdir(parents=True)

(v1 / "hello.txt").write_text("hello CLD2\nversion 1\n", encoding="utf-8")
(v1 / "data.txt").write_text("A" * 4096 + "\n", encoding="utf-8")
(v1 / "config.json").write_text('{"version":1,"feature":"old"}\n', encoding="utf-8")

(v2 / "hello.txt").write_text("hello CLD2\nversion 2\n", encoding="utf-8")
(v2 / "data.txt").write_text("A" * 4096 + "small changed tail\n", encoding="utf-8")
(v2 / "config.json").write_text('{"version":2,"feature":"new"}\n', encoding="utf-8")
(v2 / "new_file.txt").write_text("new file in v2\n", encoding="utf-8")

print("Demo data created:", v1, v2)
