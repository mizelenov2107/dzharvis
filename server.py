#!/usr/bin/env python3
"""Сервер «Джарвис» для режима команд FluidVoice.

FluidVoice распознаёт речь по горячей клавише режима команд (правый Option) и шлёт текст в
OpenAI-совместимый сервер. Этот сервер слушает http://127.0.0.1:1236/v1, отдаёт одну модель
`dzharvis` и вместо ответа языковой модели передаёт последнюю фразу Джарвису. Джарвис — один
постоянно запущенный `claude -p` в режиме stream-json со своим коротким системным промптом
(harakter.md). К фразе сервер приклеивает снимок терминалов (terminaly.snimok), поэтому
на вопрос Джарвис отвечает за один ход, 2–4 секунды. Ответ уходит в FluidVoice и виден в вырезе экрана.

Скорость держится на трёх вещах: процесс Claude не запускается заново на каждую фразу,
системный промпт Claude Code заменён коротким, снимок избавляет от вызовов инструментов.
Через PAMYAT тишины или MAKS_HODOV фраз процесс перезапускается, и разговор начинается заново.

  python3 server.py                     запустить сервер
  python3 server.py --sprosit "фраза"   спросить Джарвиса напрямую, без FluidVoice
Файл `golos` рядом с сервером включает озвучку ответов (голос Дмитрия, edge-tts). Запросы — в zhurnal.jsonl.
"""
from __future__ import annotations

import hashlib
import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import terminaly

KOREN = Path(__file__).resolve().parent
PORT = 1236
IMYA_MODELI = "dzharvis"
CLAUDE = os.path.expanduser("~/.local/bin/claude")
MOZG = os.environ.get("DZHARVIS_MODEL", "haiku")
GOLOS = os.environ.get("DZHARVIS_GOLOS", "ru-RU-DmitryNeural")
ZHDAT = 24  # секунд: FluidVoice обрывает запрос на 30
PAMYAT = 30 * 60  # после получаса тишины разговор начинается заново
MAKS_HODOV = 15  # снимок в каждой фразе раздувает контекст, поэтому сессия короткая
INSTRUMENTY = "Bash,Read,Grep,SendMessage"
RAZRESHENO = "Bash(python3 terminaly.py *),Read,Grep,SendMessage"
ZHURNAL = KOREN / "zhurnal.jsonl"

zamok_ocheredi = threading.Lock()
v_rabote: dict[str, dict] = {}
golos: subprocess.Popen | None = None


def zapisat(**polya):
    with ZHURNAL.open("a", encoding="utf-8") as z:
        z.write(json.dumps({"vremya": time.strftime("%Y-%m-%d %H:%M:%S"), **polya}, ensure_ascii=False) + "\n")


class Mozg:
    """Один постоянно запущенный claude; фразы идут по очереди."""

    imya = "Джарвис"
    maks_hodov = MAKS_HODOV

    def __init__(self):
        self.zamok = threading.Lock()
        self.process: subprocess.Popen | None = None
        self.stroki: queue.Queue = queue.Queue()
        self.hodov = 0
        self.posledniy = 0.0

    def zapustit(self):
        self.ostanovit()
        komanda = [CLAUDE, "-p", "--name", self.imya, "--model", self.model(),
                   "--input-format", "stream-json", "--output-format", "stream-json", "--verbose",
                   "--system-prompt", self.prompt(),
                   "--settings", json.dumps({"disableAllHooks": True}),
                   "--strict-mcp-config", "--no-chrome", "--no-session-persistence", *self.instrumenty()]
        self.process = subprocess.Popen(komanda, cwd=KOREN, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=(KOREN / "claude.err").open("a"), text=True, bufsize=1)
        self.stroki = queue.Queue()
        stroki, vyhod = self.stroki, self.process.stdout
        threading.Thread(target=lambda: [stroki.put(s) for s in vyhod] and None, daemon=True).start()
        self.hodov, self.posledniy = 0, time.time()

    def model(self) -> str:
        return MOZG

    def prompt(self) -> str:
        return (KOREN / "harakter.md").read_text(encoding="utf-8")

    def instrumenty(self) -> list[str]:
        return ["--tools", INSTRUMENTY, "--allowedTools", RAZRESHENO]

    def soobshchenie(self, fraza: str) -> str:
        return f"Снимок терминалов на {time.strftime('%H:%M')}:\n{terminaly.snimok()}\n\nФраза пользователя: {fraza}"

    def ostanovit(self):
        if self.process and self.process.poll() is None:
            self.process.kill()
        self.process = None

    def sprosit(self, fraza: str, predel: float = 90) -> str:
        with self.zamok:
            zhivoj = self.process and self.process.poll() is None
            if not zhivoj or self.hodov >= self.maks_hodov or time.time() - self.posledniy > PAMYAT:
                self.zapustit()
            tekst = self.soobshchenie(fraza)
            self.process.stdin.write(json.dumps({"type": "user", "message": {"role": "user", "content": tekst}},
                                                ensure_ascii=False) + "\n")
            self.process.stdin.flush()
            konec = time.time() + predel
            while True:
                try:
                    stroka = self.stroki.get(timeout=max(0.1, konec - time.time()))
                except queue.Empty:
                    self.ostanovit()
                    return "Джарвис завис, перезапускаю. Повтори через пару секунд."
                try:
                    sobytie = json.loads(stroka)
                except json.JSONDecodeError:
                    continue
                if sobytie.get("type") == "result":
                    self.hodov += 1
                    self.posledniy = time.time()
                    if sobytie.get("is_error"):
                        zapisat(oshibka=str(sobytie)[:1500])
                        self.ostanovit()
                        return "Не получилось, посмотри журнал Джарвиса."
                    return (sobytie.get("result") or "").strip() or "Готово."


mozg = Mozg()

# Режим доклада: пока лежит файл `zashchita`, фразы режима команд уходят не диспетчеру терминалов,
# а второму постоянному Claude, у которого в системном промпте материалы своего доклада. Он отвечает на вопрос
# по докладу 1–2 фразами за ~2 с. Включается и выключается голосом: «режим защиты» / «выключи защиту».
FLAG_ZASHCHITY = KOREN / "zashchita"
# Файл с материалами доклада (текст, цифры, ответы на ожидаемые вопросы) в репозиторий не кладётся.
MATERIALY = Path(os.path.expanduser(os.environ.get("DZHARVIS_ZASHCHITA", str(KOREN / "materialy" / "zashchita.md"))))


PROVERKI = {"hi", "hello", "test", "ping", "привет", "тест", "проверка"}


class MozgZashchity(Mozg):
    imya = "Суфлёр доклада"
    maks_hodov = 60

    def model(self) -> str:
        # модель можно задать в самом флаге: echo sonnet > zashchita
        v_flage = FLAG_ZASHCHITY.read_text().strip() if FLAG_ZASHCHITY.exists() else ""
        return os.environ.get("ZASHCHITA_MODEL") or v_flage or "haiku"

    def prompt(self) -> str:
        return MATERIALY.read_text(encoding="utf-8")

    def instrumenty(self) -> list[str]:
        return ["--tools", ""]

    proshlyj = ("", 0.0)

    def soobshchenie(self, fraza: str) -> str:
        # уточнение вроде «а что это за компания?» понятно только рядом с прошлым вопросом
        pred, kogda = self.proshlyj
        self.proshlyj = (fraza, time.time())
        if pred and time.time() - kogda < 180:
            return f"Предыдущая фраза: «{pred}»\nНовая фраза из созвона: «{fraza}»"
        return f"Фраза из созвона: «{fraza}»"


mozg_zashchity = MozgZashchity()


def derzhat_teplym():
    """В режиме доклада держит Claude и кэш материалов тёплыми: первый вопрос после 7 минут доклада не ждёт 8 с."""
    while True:
        if FLAG_ZASHCHITY.exists() and not FLAG_MATANA.exists() and time.time() - mozg_zashchity.posledniy > 4 * 60:
            pred = mozg_zashchity.proshlyj
            try:
                mozg_zashchity.sprosit("служебная проверка связи")
            except Exception as e:
                zapisat(oshibka=f"разогрев суфлёра: {e}")
            mozg_zashchity.proshlyj = pred  # служебная фраза не считается прошлым вопросом
        time.sleep(30)

# Режим матана: пока лежит файл `matan`, фразы уходят третьему постоянному Claude, у которого в системном
# промпте страницы подготовки к контрольной из папки DZHARVIS_MATAN (заметки .md, в репозиторий не кладутся).
# Страницы читаются при каждом запуске мозга: после правок достаточно сказать «режим матана» ещё раз.
# Режимы матана и доклада исключают друг друга.
FLAG_MATANA = KOREN / "matan"
PODGOTOVKA = Path(os.path.expanduser(os.environ.get("DZHARVIS_MATAN", str(KOREN / "materialy" / "matan"))))


class MozgMatana(Mozg):
    imya = "Репетитор матана"  # не «Матан»: так зовут терминал подготовки, имена не должны совпадать
    maks_hodov = 40

    def model(self) -> str:
        return os.environ.get("MATAN_MODEL", "sonnet")

    def prompt(self) -> str:
        stranicy = sorted(PODGOTOVKA.glob("*.md"))
        chasti = [(KOREN / "harakter-matan.md").read_text(encoding="utf-8")]
        for p in stranicy:
            # картинки слайдов модели не видны, а место в промпте занимают
            tekst = "\n".join(s for s in p.read_text(encoding="utf-8").split("\n") if not s.lstrip("> ").startswith("![["))
            chasti.append(f"===== {p.stem} =====\n{tekst}")
        return "\n\n".join(chasti)

    def instrumenty(self) -> list[str]:
        return ["--tools", ""]

    def soobshchenie(self, fraza: str) -> str:
        return f"Вопрос пользователя: «{fraza}»"


mozg_matana = MozgMatana()

# Пока жив терминал Claude Code с именем MATAN_SESSIYA (там идёт подготовка к КР), вопрос режима матана уходит
# туда через SendMessage, как будто пользователь написал его в терминал. Ответ сервер берёт из журнала этой сессии:
# в пересланном тексте стоит метка, и терминал начинает ответ с неё. Терминал закрыт — отвечает MozgMatana.
MATAN_SESSIYA = os.environ.get("MATAN_SESSIYA", "МАТАН")
ZHDAT_TERMINAL = 180  # секунд: терминал может быть занят другой задачей, ответ тогда догонит уведомлением


class Pochtalon(Mozg):
    """Пересылает голосовой вопрос в живой терминал Claude Code через SendMessage."""

    imya = "Почтальон матана"
    maks_hodov = 30

    def model(self) -> str:
        return "haiku"

    def prompt(self) -> str:
        # Haiku без жёстких рамок сам отвечал на вопрос вместо пересылки, поэтому правила такие подробные
        return ("Ты почтальон, а не собеседник. На вопросы не отвечаешь, ничего не объясняешь и не уточняешь. "
                "Каждое сообщение содержит адрес и текст между <tekst> и </tekst>. Вызови SendMessage ровно один раз: "
                "to — адрес, message — текст между тегами, скопированный символ в символ, без добавлений, "
                "сокращений и исправлений. После отправки ответь одним словом: отправил.")

    def instrumenty(self) -> list[str]:
        return ["--tools", "SendMessage", "--allowedTools", "SendMessage"]

    def soobshchenie(self, zadanie: str) -> str:
        return zadanie


pochtalon = Pochtalon()


def dlya_vyreza(tekst: str) -> str:
    """Вырез FluidVoice рисует markdown, и одиночный перенос склеил бы строки в одну. Поэтому строки разделяются
    пустой строкой, кроме дроби «в два этажа»: числитель, черта из ─ и знаменатель идут вплотную."""
    stroki = [s.rstrip() for s in tekst.split("\n") if s.strip()]
    cherta = lambda s: bool(s.strip()) and set(s.strip()) <= {"─"}
    out = []
    for i, s in enumerate(stroki):
        if i:
            out.append("\n" if cherta(s) or cherta(stroki[i - 1]) else "\n\n")
        out.append(s)
    return "".join(out)


def sprosit_terminal(s: dict, fraza: str) -> str:
    """Отправляет фразу в терминал `s` и ждёт в его журнале ответ, который начинается с метки."""
    put = terminaly.zhurnal(s)
    if not put:
        return mozg_matana.sprosit(fraza)
    metka = "#" + uuid.uuid4().hex[:6]
    tekst = (f"Вопрос голосом из суфлёра {metka}: «{fraza}»\n"
             f"Ответ уйдёт в вырез экрана: начни его с {metka}, дальше 1–4 коротких предложения. Каждую формулу — "
             "отдельной строкой, Unicode (x², f⁽⁵⁾, −, ·); дробь — в два этажа: числитель, черта ─ чуть короче него, "
             "знаменатель. Итог можно **жирным**.")
    nachalo = os.path.getsize(put)
    pochtalon.sprosit(f"Адрес: {s['name']}\n<tekst>{tekst}</tekst>")
    konec = time.time() + ZHDAT_TERMINAL
    with open(put, encoding="utf-8") as f:
        f.seek(nachalo)
        hvost = ""
        while time.time() < konec:
            kusok = f.read()
            if not kusok:
                time.sleep(0.3)
                continue
            hvost += kusok
            *stroki, hvost = hvost.split("\n")
            for stroka in stroki:
                try:
                    z = json.loads(stroka)
                except json.JSONDecodeError:
                    continue
                if z.get("type") != "assistant":
                    continue
                for blok in z.get("message", {}).get("content", []):
                    t = blok.get("text", "") if isinstance(blok, dict) and blok.get("type") == "text" else ""
                    if t.lstrip().startswith(metka):
                        return dlya_vyreza(t.lstrip()[len(metka):].strip(" :—-\n").replace("`", ""))
    return "Терминал МАТАН не ответил за три минуты."


def tekushchij_mozg() -> Mozg:
    if FLAG_MATANA.exists():
        return mozg_matana
    return mozg_zashchity if FLAG_ZASHCHITY.exists() else mozg


def komanda_rezhima(fraza: str) -> str | None:
    """Голосовое переключение режимов доклада и матана; None, если фраза не про режим."""
    f = fraza.lower().replace("ё", "е")
    if any(s in f for s in ("обычный режим", "режим джарвис")):
        FLAG_ZASHCHITY.unlink(missing_ok=True)
        FLAG_MATANA.unlink(missing_ok=True)
        mozg_zashchity.ostanovit()
        mozg_matana.ostanovit()
        return "Обычный режим, снова Джарвис."
    if any(s in f for s in ("выключи защит", "выключить защит", "выйди из защит")):
        FLAG_ZASHCHITY.unlink(missing_ok=True)
        mozg_zashchity.ostanovit()
        return "Режим доклада выключен, снова Джарвис."
    if any(s in f for s in ("выключи матан", "выключить матан", "выйди из матан")):
        FLAG_MATANA.unlink(missing_ok=True)
        mozg_matana.ostanovit()
        return "Режим матана выключен, снова Джарвис."
    if any(s in f for s in ("режим защит", "включи защит", "включить защит")):
        FLAG_MATANA.unlink(missing_ok=True)
        mozg_matana.ostanovit()
        FLAG_ZASHCHITY.touch()
        threading.Thread(target=mozg_zashchity.zapustit, daemon=True).start()
        return "Режим доклада включён: отвечаю на вопросы по материалам доклада."
    if any(s in f for s in ("режим матан", "включи матан", "включить матан")):
        FLAG_ZASHCHITY.unlink(missing_ok=True)
        mozg_zashchity.ostanovit()
        FLAG_MATANA.touch()
        threading.Thread(target=mozg_matana.zapustit, daemon=True).start()
        return "Режим матана включён: отвечаю по страницам подготовки к КР."
    return None


def sintez(tekst: str) -> str | None:
    """Нейроголос Microsoft через edge-tts в mp3; None, если сети нет или edge-tts не стоит."""
    try:
        import asyncio
        import tempfile

        import edge_tts
    except ImportError:
        return None
    put = tempfile.NamedTemporaryFile(prefix="dzharvis-", suffix=".mp3", delete=False).name
    # сервис Microsoft через раз отвечает «No audio was received» на любую фразу, повтор помогает
    for popytka in range(3):
        try:
            # поправка часов edge-tts живёт в процессе, поэтому синтез внутри сервера, а не отдельной командой
            asyncio.run(edge_tts.Communicate(tekst, GOLOS, rate="+8%").save(put))
            return put
        except Exception as e:  # noqa: BLE001 — без сети говорим запасным голосом
            zapisat(oshibka_golosa=str(e)[:300], popytka=popytka + 1)
    return None


def skazat(tekst: str):
    global golos
    if not (KOREN / "golos").exists():
        return
    put = sintez(tekst)
    if golos and golos.poll() is None:
        golos.terminate()  # новый ответ перебивает старый
    golos = subprocess.Popen(["afplay", put] if put else ["say", "-v", "Milena", "-r", "190", tekst])


def uvedomit(tekst: str):
    stroka = tekst.replace("\\", "\\\\").replace('"', '\\"')
    subprocess.Popen(["osascript", "-e", f'display notification "{stroka}" with title "Джарвис"'])


def rabotat(fraza: str, zapis: dict):
    t0 = time.time()
    try:
        otvet = komanda_rezhima(fraza)
        if otvet is None:
            kto = tekushchij_mozg()
            terminal = terminaly.najti(MATAN_SESSIYA) if kto is mozg_matana else None
            otvet = sprosit_terminal(terminal, fraza) if terminal else kto.sprosit(fraza)
            if kto is mozg_zashchity:
                # Haiku иногда начинает со служебной строки «Это вопрос из зала…» — отрезаем её
                abzacy = [a.strip() for a in otvet.split("\n\n") if a.strip()]
                if len(abzacy) > 1 and ("вопрос из зала" in abzacy[0].lower() or "ответ с экрана" in abzacy[0].lower()):
                    otvet = "\n\n".join(abzacy[1:])
            if otvet.strip() in ("—", "-"):
                otvet = "— не вопрос, пропускаю"
    except Exception as e:  # noqa: BLE001 — любой сбой надо показать в вырезе, а не молча упасть
        otvet = f"Сломался: {e}"
        mozg.ostanovit()
    zapis["otvet"] = otvet
    zapis["gotovo"].set()
    skazat(otvet)
    if zapis["pozdno"]:
        uvedomit(otvet)  # вырез уже закрылся с «Ещё смотрю», ответ догоняет уведомлением
    zapisat(fraza=fraza, otvet=otvet, sekund=round(time.time() - t0, 1), pozdno=zapis["pozdno"])


def otvetit_na(fraza: str, kluch: str) -> str:
    with zamok_ocheredi:
        zapis = v_rabote.get(kluch)
        if zapis is None or time.time() - zapis["vremya"] > 300:
            zapis = {"gotovo": threading.Event(), "otvet": None, "vremya": time.time(), "pozdno": False}
            v_rabote[kluch] = zapis
            threading.Thread(target=rabotat, args=(fraza, zapis), daemon=True).start()
    if zapis["gotovo"].wait(ZHDAT):
        return zapis["otvet"]
    zapis["pozdno"] = True
    return "Ещё смотрю, отвечу уведомлением."


def tekst_soobshcheniya(soobshchenie: dict) -> str:
    soderzhanie = soobshchenie.get("content")
    if isinstance(soderzhanie, list):
        return " ".join(c.get("text", "") for c in soderzhanie if isinstance(c, dict)).strip()
    return (soderzhanie or "").strip()


class Obrabotchik(BaseHTTPRequestHandler):
    def _json(self, kod: int, dannye: dict):
        telo = json.dumps(dannye, ensure_ascii=False).encode()
        self.send_response(kod)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(telo)))
        self.end_headers()
        self.wfile.write(telo)

    def do_GET(self):
        if self.path.rstrip("/").endswith("/models"):
            self._json(200, {"object": "list", "data": [
                {"id": IMYA_MODELI, "object": "model", "created": 0, "owned_by": "dzharvis"}]})
        else:
            self._json(404, {"error": {"message": "нет такого адреса"}})

    def do_POST(self):
        if not self.path.rstrip("/").endswith("/chat/completions"):
            self._json(404, {"error": {"message": "нет такого адреса"}})
            return
        try:
            zapros = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
        except json.JSONDecodeError:
            self._json(400, {"error": {"message": "тело не JSON"}})
            return
        soobshcheniya = zapros.get("messages") or []
        polzovatelya = [m for m in soobshcheniya if m.get("role") == "user"]
        fraza = tekst_soobshcheniya(polzovatelya[-1]) if polzovatelya else ""
        bez_obvyazki = not zapros.get("tools") and not any(m.get("role") == "system" for m in soobshcheniya)
        # проверка связи — только короткое «Hi»/«test»; настоящий вопрос без обвязки тоже получает ответ
        proverka = bez_obvyazki and (len(fraza.strip()) <= 6 or fraza.strip().lower().strip(".!?") in PROVERKI)
        if proverka or not fraza:
            # FluidVoice проверяет провайдера запросом «Hi»/«test» без инструментов
            otvet = "Джарвис на связи."
            zapisat(proverka=True, fraza=fraza)
        else:
            # FluidVoice повторяет запрос при обрыве: тот же ключ ждёт тот же ответ
            kluch = hashlib.sha1(f"{len(soobshcheniya)}|{fraza}".encode()).hexdigest()
            otvet = otvetit_na(fraza, kluch)
        self._otdat(otvet, bool(zapros.get("stream")))

    def _otdat(self, otvet: str, potok: bool):
        nomer, sozdan = f"chatcmpl-{uuid.uuid4().hex[:24]}", int(time.time())
        if not potok:
            self._json(200, {"id": nomer, "object": "chat.completion", "created": sozdan, "model": IMYA_MODELI,
                             "choices": [{"index": 0, "message": {"role": "assistant", "content": otvet},
                                          "finish_reason": "stop"}],
                             "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}})
            return
        kuski = [{"role": "assistant", "content": otvet}, {}]
        telo = b""
        for i, delta in enumerate(kuski):
            kusok = {"id": nomer, "object": "chat.completion.chunk", "created": sozdan, "model": IMYA_MODELI,
                     "choices": [{"index": 0, "delta": delta, "finish_reason": "stop" if i else None}]}
            telo += f"data: {json.dumps(kusok, ensure_ascii=False)}\n\n".encode()
        telo += b"data: [DONE]\n\n"
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(telo)))
        self.end_headers()
        self.wfile.write(telo)

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    if sys.argv[1:2] in (["--sprosit"], ["--zashchita"], ["--matan"]):
        m = {"--zashchita": mozg_zashchity, "--matan": mozg_matana}.get(sys.argv[1], mozg)
        t0 = time.time()
        terminal = terminaly.najti(MATAN_SESSIYA) if m is mozg_matana else None
        otvet = sprosit_terminal(terminal, " ".join(sys.argv[2:])) if terminal else m.sprosit(" ".join(sys.argv[2:]))
        print(f"{otvet}\n({time.time() - t0:.1f} с)")
        m.ostanovit()
        pochtalon.ostanovit()
    else:
        mozg.zapustit()  # прогрев: первая фраза не ждёт запуска Claude
        if FLAG_MATANA.exists():
            mozg_matana.zapustit()
        threading.Thread(target=derzhat_teplym, daemon=True).start()
        ThreadingHTTPServer(("127.0.0.1", PORT), Obrabotchik).serve_forever()
