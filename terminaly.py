#!/usr/bin/env python3
"""Что происходит в терминалах Claude Code — глаза Джарвиса.

  python3 terminaly.py spisok              живые сессии: имя, статус, давно ли, последняя просьба
  python3 terminaly.py hvost <имя> [N]     последние N реплик сессии (по умолчанию 12)

Сессии берутся из реестра ~/.claude/sessions/<pid>.json, журнал сессии лежит
в ~/.claude/projects/*/<sessionId>.jsonl. Только чтение, ничего не пишет.
"""
from __future__ import annotations

import glob
import json
import os
import re
import sys
import time
from datetime import datetime

DOM = os.path.expanduser("~")
REESTR = os.path.join(DOM, ".claude", "sessions")
ZHURNALY = os.path.join(DOM, ".claude", "projects")
SLUZHEBNOE = ("<command-", "<local-command", "<system-reminder", "<task-notification", "Caveat:")


def norm(imya: str) -> str:
    return re.sub(r"[^0-9a-zа-я]", "", imya.lower().replace("ё", "е"))


def zhiv(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except OSError:
        return False


def sessii() -> list[dict]:
    spisok = []
    for put in glob.glob(os.path.join(REESTR, "*.json")):
        try:
            with open(put, encoding="utf-8") as f:
                s = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue
        if s.get("kind") != "interactive" or not zhiv(int(s.get("pid", 0))):
            continue
        spisok.append(s)
    return sorted(spisok, key=lambda s: s.get("name", ""))


def najti(imya: str) -> dict | None:
    cel = norm(imya)
    vse = sessii()
    for s in vse:
        if norm(s.get("name", "")) == cel:
            return s
    pohozhie = [s for s in vse if cel and cel in norm(s.get("name", ""))]
    return pohozhie[0] if len(pohozhie) == 1 else None


def zhurnal(s: dict) -> str | None:
    najdeno = glob.glob(os.path.join(ZHURNALY, "*", f"{s.get('sessionId')}.jsonl"))
    return najdeno[0] if najdeno else None


def zapisi(put: str, hvost_bajt: int = 3_000_000) -> list[dict]:
    """Записи журнала; журналы весят сотни мегабайт, поэтому читаем только хвост файла."""
    with open(put, "rb") as f:
        f.seek(0, os.SEEK_END)
        razmer = f.tell()
        f.seek(max(0, razmer - hvost_bajt))
        dannye = f.read()
    stroki = dannye.decode("utf-8", errors="replace").splitlines()
    if razmer > hvost_bajt:
        stroki = stroki[1:]  # первая строка хвоста обрезана посередине
    vyhod = []
    for stroka in stroki:
        try:
            vyhod.append(json.loads(stroka))
        except json.JSONDecodeError:
            continue
    return vyhod


def tekst(soderzhanie) -> str:
    if isinstance(soderzhanie, str):
        return soderzhanie.strip()
    if isinstance(soderzhanie, list):
        return "\n".join(b.get("text", "").strip() for b in soderzhanie
                         if isinstance(b, dict) and b.get("type") == "text").strip()
    return ""


def vremya(z: dict) -> str:
    metka = z.get("timestamp")
    if not metka:
        return "     "
    try:
        return datetime.fromisoformat(metka.replace("Z", "+00:00")).astimezone().strftime("%H:%M")
    except ValueError:
        return "     "


def kratko(t: str, predel: int) -> str:
    t = re.sub(r"\s+", " ", t).strip()
    return t if len(t) <= predel else t[:predel].rstrip() + "…"


def repliki(put: str) -> list[str]:
    """Реплики сессии по порядку: просьбы пользователя, ответы Claude, сводки."""
    vyhod: list[str] = []
    instrumenty: list[str] = []

    def sbrosit():
        if instrumenty:
            vyhod.append("        · " + "; ".join(instrumenty[-4:])
                         + (f" (и ещё {len(instrumenty) - 4})" if len(instrumenty) > 4 else ""))
            instrumenty.clear()

    for z in zapisi(put):
        tip, soobshchenie = z.get("type"), z.get("message") or {}
        if tip == "user" and not z.get("isMeta"):
            t = tekst(soobshchenie.get("content"))
            if not t or t.startswith(SLUZHEBNOE):
                continue
            sbrosit()
            if t.startswith("This session is being continued"):
                vyhod.append(f"{vremya(z)} [контекст сжат, сводка прежней работы] {kratko(t, 1200)}")
            else:
                vyhod.append(f"{vremya(z)} ПОЛЬЗОВАТЕЛЬ: {kratko(t, 500)}")
        elif tip == "assistant":
            for b in soobshchenie.get("content") or []:
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "tool_use":
                    vvod = b.get("input") or {}
                    opisanie = vvod.get("description") or vvod.get("file_path") or vvod.get("command") or ""
                    imya = str(b.get("name", "")).split("__")[-1]
                    zapis = f"{imya}: {kratko(str(opisanie), 70)}" if opisanie else imya
                    if not instrumenty or instrumenty[-1] != zapis:
                        instrumenty.append(zapis)
                elif b.get("type") == "text" and b.get("text", "").strip():
                    sbrosit()
                    vyhod.append(f"{vremya(z)} CLAUDE: {kratko(b['text'], 700)}")
        elif tip == "system" and z.get("subtype") == "away_summary":
            t = z.get("content") or z.get("summary") or ""
            if isinstance(t, str) and t.strip():
                sbrosit()
                vyhod.append(f"{vremya(z)} [сводка] {kratko(t, 500)}")
    sbrosit()
    return vyhod


def poslednyaya_prosba(put: str) -> str:
    for r in reversed(repliki(put)):
        if "ПОЛЬЗОВАТЕЛЬ:" in r:
            return r.split("ПОЛЬЗОВАТЕЛЬ:", 1)[1].strip()
    return ""


def davno(ms) -> str:
    if not ms:
        return "?"
    minut = int((time.time() * 1000 - ms) / 60000)
    if minut < 1:
        return "только что"
    if minut < 60:
        return f"{minut} мин назад"
    return f"{minut // 60} ч {minut % 60} мин назад"


def komanda_spisok():
    for s in sessii():
        put = zhurnal(s)
        prosba = kratko(poslednyaya_prosba(put), 120) if put else ""
        status = {"busy": "работает", "idle": "свободна"}.get(s.get("status"), s.get("status", "?"))
        print(f"{s.get('name')}: {status}, обновлялась {davno(s.get('statusUpdatedAt'))}, "
              f"папка {s.get('cwd', '').replace(DOM, '~')}")
        if prosba:
            print(f"    последняя просьба: {prosba}")


def snimok() -> str:
    """Сводка всех терминалов: статус, последняя просьба и последний ответ. Идёт Джарвису вместе с фразой."""
    stroki = []
    for s in sessii():
        status = {"busy": "работает", "idle": "свободна"}.get(s.get("status"), s.get("status", "?"))
        stroki.append(f"■ {s.get('name')} — {status}, обновлялась {davno(s.get('statusUpdatedAt'))}")
        put = zhurnal(s)
        if not put:
            continue
        vse = repliki(put)
        nomer = max((i for i, r in enumerate(vse) if "ПОЛЬЗОВАТЕЛЬ:" in r), default=-1)
        if nomer >= 0:
            stroki.append("  просьба " + kratko(vse[nomer].strip(), 260))
        posle = [r for r in vse[nomer + 1:] if "CLAUDE:" in r or "[сводка]" in r]
        if posle:
            stroki.append("  ответ " + kratko(posle[-1].strip(), 500))
    return "\n".join(stroki)


def komanda_hvost(imya: str, n: int):
    s = najti(imya)
    if not s:
        imena = ", ".join(x.get("name", "") for x in sessii())
        print(f"Сессии «{imya}» нет. Есть: {imena}")
        sys.exit(1)
    put = zhurnal(s)
    status = {"busy": "работает", "idle": "свободна"}.get(s.get("status"), s.get("status", "?"))
    print(f"Сессия {s.get('name')}: {status}, обновлялась {davno(s.get('statusUpdatedAt'))}")
    if not put:
        print("Журнал не найден.")
        return
    for r in repliki(put)[-n:]:
        print(r)


if __name__ == "__main__":
    argi = sys.argv[1:]
    if argi[:1] == ["spisok"]:
        komanda_spisok()
    elif argi[:1] == ["snimok"]:
        print(snimok())
    elif argi[:1] == ["hvost"] and len(argi) >= 2:
        komanda_hvost(argi[1], int(argi[2]) if len(argi) > 2 else 12)
    else:
        print(__doc__)
        sys.exit(2)
