"""Версии программы: выпуск новой, проверка, текст релиза. Нужен только Python и git.

    python3 scripts/release.py bump minor        новая версия (patch, minor, major или точный номер X.Y.Z):
                                                 callsum/__init__.py, CHANGELOG.md, бейдж в README.md,
                                                 затем коммит «Версия X.Y.Z» и метка vX.Y.Z
    git push --follow-tags                       отправить — GitHub Actions сам соберёт релиз

    python3 scripts/release.py check [--tag vX.Y.Z]   номер версии везде один (так проверяет CI)
    python3 scripts/release.py notes [X.Y.Z]          что нового в версии — текст релиза
    python3 scripts/release.py version                текущая версия
"""

import argparse
import datetime
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INIT = ROOT / "callsum" / "__init__.py"
CHANGELOG = ROOT / "CHANGELOG.md"
README = ROOT / "README.md"
REPO = "https://github.com/Dartanyun/call-summarizer"
UNRELEASED = "Не выпущено"

SEMVER = r"\d+\.\d+\.\d+"
VERSION_RE = re.compile(rf'^__version__ = "({SEMVER})"$', re.M)
BADGE_RE = re.compile(rf"(img\.shields\.io/badge/version-)({SEMVER})(-)")
SECTION_RE = re.compile(r"^## \[([^\]]+)\].*$", re.M)
LINKS_RE = re.compile(r"^\[[^\]]+\]: ", re.M)  # ссылки на сравнение версий внизу файла


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def write(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8", newline="\n")


def git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=ROOT, check=True, text=True, capture_output=True).stdout


def current_version() -> str:
    m = VERSION_RE.search(read(INIT))
    if not m:
        sys.exit(f"Ошибка: в {INIT.relative_to(ROOT)} нет строки __version__ = \"X.Y.Z\"")
    return m.group(1)


def sections(text: str) -> dict[str, str]:
    """Разделы CHANGELOG.md по порядку: {«Не выпущено» или номер версии: текст раздела}."""
    heads = list(SECTION_RE.finditer(text))
    out = {}
    for i, m in enumerate(heads):
        end = heads[i + 1].start() if i + 1 < len(heads) else len(text)
        out[m.group(1)] = LINKS_RE.split(text[m.end():end])[0].strip()
    return out


def as_tuple(version: str) -> tuple[int, ...]:
    return tuple(int(x) for x in version.split("."))


def next_version(current: str, part: str) -> str:
    if re.fullmatch(SEMVER, part):
        return part
    major, minor, patch = as_tuple(current)
    return {"major": f"{major + 1}.0.0", "minor": f"{major}.{minor + 1}.0", "patch": f"{major}.{minor}.{patch + 1}"}[part]


def check(tag: str | None = None) -> None:
    version = current_version()
    problems = []
    badge = BADGE_RE.search(read(README))
    if not badge:
        problems.append("в README.md нет бейджа версии (img.shields.io/badge/version-X.Y.Z-…)")
    elif badge.group(2) != version:
        problems.append(f"бейдж в README.md показывает {badge.group(2)}, а версия {version}")
    found = sections(read(CHANGELOG))
    if UNRELEASED not in found:
        problems.append(f"в CHANGELOG.md нет раздела «{UNRELEASED}»")
    released = [name for name in found if name != UNRELEASED]
    if not released or released[0] != version:
        problems.append(f"последняя версия в CHANGELOG.md — {released[0] if released else 'никакой'}, а версия {version}")
    if tag is not None and tag.removeprefix("v") != version:
        problems.append(f"метка {tag} не совпадает с версией {version} в callsum/__init__.py")
    if problems:
        sys.exit("Ошибка:\n  " + "\n  ".join(problems))
    print(f"ОК: версия {version} одна и та же в callsum/__init__.py, README.md и CHANGELOG.md"
          + (f", метка {tag}" if tag else ""))


def bump(part: str, commit: bool) -> None:
    current = current_version()
    new = next_version(current, part)
    if as_tuple(new) <= as_tuple(current):
        sys.exit(f"Ошибка: новая версия {new} должна быть больше текущей {current}")
    if commit:
        if git("status", "--porcelain").strip():
            sys.exit("Ошибка: есть незакоммиченные изменения — сначала закоммитьте их")
        if git("tag", "--list", f"v{new}").strip():
            sys.exit(f"Ошибка: метка v{new} уже есть")
    log = read(CHANGELOG)
    if not sections(log).get(UNRELEASED):
        sys.exit(f"Ошибка: раздел «{UNRELEASED}» в CHANGELOG.md пуст — опишите, что изменилось в версии {new}")

    today = datetime.date.today().isoformat()
    log = log.replace(f"## [{UNRELEASED}]", f"## [{UNRELEASED}]\n\n## [{new}] — {today}", 1)
    log = re.sub(rf"^\[{UNRELEASED}\]: .*$",
                 f"[{UNRELEASED}]: {REPO}/compare/v{new}...HEAD\n[{new}]: {REPO}/compare/v{current}...v{new}",
                 log, count=1, flags=re.M)
    write(CHANGELOG, log)
    write(INIT, VERSION_RE.sub(f'__version__ = "{new}"', read(INIT), count=1))
    write(README, BADGE_RE.sub(rf"\g<1>{new}\g<3>", read(README)))
    check()

    if commit:
        git("commit", "--quiet", "--all", "--message", f"Версия {new}")
        git("tag", "--annotate", f"v{new}", "--message", f"Версия {new}")
        print(f"Готово: {current} → {new}, коммит и метка v{new}.\n"
              "Отправьте на GitHub — там проверится код, соберётся релиз и пройдёт проверка на Windows:\n"
              "    git push --follow-tags")
    else:
        print(f"Готово: {current} → {new}. Коммит и метку сделайте сами.")


def notes(version: str | None) -> None:
    version = version or current_version()
    body = sections(read(CHANGELOG)).get(version)
    if body is None:
        sys.exit(f"Ошибка: в CHANGELOG.md нет раздела [{version}]")
    print(body)


def main() -> None:
    parser = argparse.ArgumentParser(description="Версии «Саммари созвонов»")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("bump", help="выпустить новую версию")
    p.add_argument("part", help="patch, minor, major или номер X.Y.Z")
    p.add_argument("--no-git", action="store_true", help="только поменять файлы, без коммита и метки")
    p = sub.add_parser("check", help="проверить, что номер версии везде один")
    p.add_argument("--tag", help="метка git, которая должна совпасть с версией (vX.Y.Z)")
    p = sub.add_parser("notes", help="раздел CHANGELOG.md для версии")
    p.add_argument("version", nargs="?")
    sub.add_parser("version", help="текущая версия")
    args = parser.parse_args()

    if args.command == "bump":
        if args.part not in ("patch", "minor", "major") and not re.fullmatch(SEMVER, args.part):
            parser.error("укажите patch, minor, major или номер вида 1.2.3")
        bump(args.part, commit=not args.no_git)
    elif args.command == "check":
        check(args.tag)
    elif args.command == "notes":
        notes(args.version)
    else:
        print(current_version())


if __name__ == "__main__":
    main()
