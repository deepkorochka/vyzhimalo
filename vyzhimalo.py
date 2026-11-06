#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Vyzhimalo — выжималка названий треков из плейлистов Яндекс Музыки. 🍋

Тянешь ссылку на любой публичный плейлист Я.Музыки — получаешь аккуратный
список треков. Можно выжать и в CSV/JSON/Markdown/M3U. Без зависимостей:
только стандартная библиотека Python 3.9+. Логин/токен не нужны.

Примеры:
    python vyzhimalo.py https://music.yandex.ru/playlists/<идентификатор>
    python vyzhimalo.py https://music.yandex.ru/users/<логин>/playlists/3 -o izryadno.txt
    python vyzhimalo.py <ссылка> --order title-artist --no-duration --number
    python vyzhimalo.py           (без аргументов — интерактивный режим)

Лицензия: MIT. Неофициальный инструмент; не связан с Яндексом.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

VERSION = "1.0.0"
DEFAULT_TIMEOUT = 30          # сек
DEFAULT_RETRIES = 3           # попыток на запрос
BATCH_SIZE = 100              # треков за один POST /tracks

API_BASE = "https://api.music.yandex.net"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


class VyzhimaloError(Exception):
    """Понятные пользователю ошибки — выводим без стектрейса."""


# ----------------------------------------------------------------------------
#  Модель трека
# ----------------------------------------------------------------------------

@dataclass
class Track:
    pos: int                                    # позиция в плейлисте (1-based)
    track_id: int
    album_id: int = 0
    title: str = ""
    artists: list = field(default_factory=list)  # список строк
    album_title: str = ""
    year: int | None = None
    duration_ms: int = 0
    available: bool = True

    @property
    def duration_str(self) -> str:
        if not self.duration_ms:
            return ""
        s = round(self.duration_ms / 1000)
        h, rem = divmod(s, 3600)
        m, sec = divmod(rem, 60)
        return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"

    @property
    def artist_line(self) -> str:
        return ", ".join(a for a in self.artists if a) or "Неизвестный исполнитель"

    @property
    def ym_url(self) -> str:
        return f"https://music.yandex.ru/album/{self.album_id or 0}/track/{self.track_id}"


# ----------------------------------------------------------------------------
#  Клиент Яндекс Музыки (публичный внутренний API, без авторизации)
# ----------------------------------------------------------------------------

def play_path(user, kind) -> str:
    """'users/<uid|логин>/playlists/<kind>' — путь для API."""
    return f"users/{user}/playlists/{kind}"


class YandexMusicClient:
    def __init__(self, timeout: int = DEFAULT_TIMEOUT, retries: int = DEFAULT_RETRIES):
        self.timeout = timeout
        self.retries = max(1, retries)

    # -- низкий уровень -------------------------------------------------------
    def _request(self, method: str, url: str, data: bytes | None = None) -> bytes:
        req = urllib.request.Request(url, data=data, headers={
            "User-Agent": UA,
            "Accept": "application/json, text/plain, */*",
        })
        last_err: Exception | None = None
        for attempt in range(1, self.retries + 1):
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    return resp.read()
            except urllib.error.HTTPError as e:
                body = b""
                try:
                    body = e.read()
                except Exception:
                    pass
                if e.code == 404:
                    raise VyzhimaloError(
                        "Плейлист не найден: возможно, ссылка устарела или плейлист приватный."
                    ) from e
                if e.code == 403:
                    raise VyzhimaloError(
                        "Яндекс ограничил доступ (403): попробуйте позже или через VPN."
                    ) from e
                if e.code == 429:
                    raise VyzhimaloError(
                        "Слишком много запросов (429). Подождите минуту и повторите."
                    ) from e
                last_err = VyzhimaloError(f"Сервер Яндекса: HTTP {e.code} ({body[:120]!r})")
            except (urllib.error.URLError, OSError, TimeoutError) as e:
                last_err = VyzhimaloError(f"Проблема с сетью: {e}")
            if attempt < self.retries:
                time.sleep(min(2 ** attempt, 8))
        raise last_err or VyzhimaloError("Неизвестная ошибка сети")

    def api_get(self, path: str) -> dict:
        url = API_BASE.rstrip("/") + ("" if path.startswith("/") else "/") + path
        raw = self._request("GET", url)
        try:
            data = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as e:
            raise VyzhimaloError(f"Яндекс ответил не-JSON: {e}") from e
        if isinstance(data, dict) and data.get("error"):
            raise VyzhimaloError(f"API Яндекс Музыки: {data['error']}")
        result = data.get("result", data)
        if not isinstance(result, dict):
            raise VyzhimaloError("Неожиданный ответ API Яндекс Музыки")
        return result

    def tracks_full_info(self, pairs: list[str]) -> list[dict]:
        """POST /tracks батчами; пары вида 'id:albumId' или 'id'."""
        out: list[dict] = []
        for i in range(0, len(pairs), BATCH_SIZE):
            chunk = pairs[i:i + BATCH_SIZE]
            fields = [("trackIds", p) for p in chunk]
            fields.append(("with-positions", "false"))
            body = urllib.parse.urlencode(fields, doseq=True).encode("ascii")
            raw = self._request("POST", f"{API_BASE}/tracks", data=body)
            data = json.loads(raw.decode("utf-8"))
            out.extend(data.get("result") or [])
        return out

    # -- ссылка → плейлист -----------------------------------------------------
    def fetch_playlist(self, url: str) -> tuple[dict, list[Track]]:
        kind, ident = parse_playlist_url(url)
        if kind == "uuid":
            short = self.api_get(f"/playlist/{ident}")
            owner = short.get("owner") or {}
            uid = owner.get("uid") or short.get("uid")
            p_kind = short.get("kind")
            if not uid or not p_kind:
                raise VyzhimaloError("Не удалось определить владельца плейлиста")
            ident = play_path(uid, p_kind)
            meta = {
                "title": short.get("title"),
                "owner": owner,
                "trackCount": short.get("trackCount"),
                "durationMs": short.get("durationMs"),
                "playlistUuid": short.get("playlistUuid", ""),
                "visibility": short.get("visibility", ""),
            }
        else:
            login, p_kind = ident
            ident = play_path(login, p_kind)
            meta = None

        pl = self.api_get(f"/{ident}" if not ident.startswith("/") else ident)
        if meta is None:
            meta = {k: pl.get(k) for k in ("title", "owner", "trackCount", "durationMs", "playlistUuid")}

        raw_tracks = pl.get("tracks") or []
        if not raw_tracks and pl.get("trackCount"):
            raise VyzhimaloError(
                "Плейлист доступен, но треки не отдались "
                "(вероятно, плейлист скрыт настройками приватности)."
            )
        if not raw_tracks:
            raise VyzhimaloError("В этом плейлисте нет треков.")

        info(f"→ Тянулю данные о {len(raw_tracks)} треках…")
        tracks = self._materialize(raw_tracks)
        meta.setdefault("title", "Плейлист")
        return meta, tracks

    def _materialize(self, raw_tracks: list) -> list[Track]:
        """raw_tracks: [{'id','albumId','track':{…}} | {'id','albumId'}] → Track"""
        out: list[Track] = []
        pending: list[tuple[int, str, int]] = []    # (позиция, id, albumId)
        for i, rt in enumerate(raw_tracks, 1):
            if not isinstance(rt, dict):
                continue
            t = rt.get("track") or {}
            tid = str(rt.get("id") or t.get("id") or "")
            if not tid:
                continue
            try:
                aid = int(rt.get("albumId") or t.get("albumId") or 0)
            except (TypeError, ValueError):
                aid = 0
            if t.get("title"):
                out.append(self._track_from(i, tid, aid, t))
            else:
                pending.append((i, tid, aid))

        if pending:
            pairs = [f"{tid}:{aid}" if aid else tid for _, tid, aid in pending]
            infos = self.tracks_full_info(pairs)
            by_id: dict[str, dict] = {}
            for inf in infos:
                rid = str(inf.get("realId") or inf.get("id") or "")
                if rid and rid not in by_id:
                    by_id[rid] = inf
            for pos, tid, aid in pending:
                inf = by_id.get(tid)
                if inf:
                    out.append(self._track_from(pos, tid, aid, inf))

        out.sort(key=lambda t: t.pos)
        for t in out:
            if not t.title:
                t.title = "(без названия)"
        return out

    @staticmethod
    def _track_from(pos: int, track_id: str, album_id: int, t: dict) -> Track:
        artists = [a.get("name") for a in (t.get("artists") or []) if a.get("name")]
        albums = t.get("albums") or []
        album_title = ""
        year = None
        if albums and isinstance(albums[0], dict):
            album_title = albums[0].get("title") or ""
            year = albums[0].get("year")
            if year:
                year = int(year)
            if not album_id:
                try:
                    album_id = int(albums[0].get("id") or 0)
                except (TypeError, ValueError):
                    album_id = 0
        return Track(
            pos=pos,
            track_id=int(track_id) if str(track_id).isdigit() else 0,
            album_id=album_id,
            title=t.get("title") or "",
            artists=artists,
            album_title=album_title,
            year=year,
            duration_ms=int(t.get("durationMs") or 0),
            available=bool(t.get("available", True)),
        )


# ----------------------------------------------------------------------------
#  Разбор ссылок
# ----------------------------------------------------------------------------

def parse_playlist_url(url: str) -> tuple[str, object]:
    """→ ('uuid', uuid) либо ('user', (логин, kind))"""
    url = url.strip().strip('"\'')
    if not re.match(r"^https?://", url, re.I):
        url = "https://music.yandex.ru" + ("" if url.startswith("/") else "/") + url
    parts = [p for p in urllib.parse.urlparse(url).path.split("/") if p]

    if "users" in parts and "playlists" in parts:
        i = parts.index("users")
        if len(parts) >= i + 4 and parts[i + 2] == "playlists":
            return "user", (parts[i + 1], parts[i + 3])
        raise VyzhimaloError(
            "Это ссылка на профиль, а не на плейлист. Нужна: /users/<логин>/playlists/<номер>"
        )

    if "playlists" in parts:
        i = parts.index("playlists")
        tail = parts[i + 1:]
        if not tail:
            raise VyzhimaloError("В ссылке нет идентификатора плейлиста.")
        first = tail[0]
        if re.fullmatch(r"lk\.[0-9a-fA-F-]+", first) or re.fullmatch(r"[0-9a-fA-F-]{16,}", first):
            return "uuid", first
        raise VyzhimaloError(
            f"Не удалось понять идентификатор плейлиста: {first!r}\n"
            "Жду ссылки вида:\n"
            "  https://music.yandex.ru/playlists/<идентификатор>\n"
            "  https://music.yandex.ru/users/<логин>/playlists/3"
        )

    raise VyzhimaloError(
        "Это не похоже на ссылку на плейлист Яндекс Музыки.\n"
        "Пример: https://music.yandex.ru/playlists/<идентификатор>"
    )


# ----------------------------------------------------------------------------
#  Форматирование
# ----------------------------------------------------------------------------

def format_line(track: Track, opts) -> str:
    sep = opts.sep
    chunks: list[str] = []
    if opts.number:
        chunks.append(f"{track.pos}.")
    if opts.order == "title-artist":
        chunks.append(f"{track.title or '(без названия)'}{sep}{track.artist_line}")
    else:
        chunks.append(f"{track.artist_line}{sep}{track.title or '(без названия)'}")
    line = " ".join(chunks)
    if opts.duration and track.duration_str:
        line += f" [{track.duration_str}]"
    if opts.album and track.album_title:
        line += f" — {track.album_title}"
    if opts.year and track.year:
        line += f" ({track.year})"
    if opts.link:
        line += f" <{track.ym_url}>"
    if opts.mark_unavailable and not track.available:
        line += " [недоступен]"
    return line


def sanitize_filename(name: str) -> str:
    name = re.sub(r"[\\/:*?\"<>|\n\r\t]", " ", name).strip()
    name = re.sub(r"\s+", " ", name)
    name = name.strip(". ")
    return name or "playlist"


# ----------------------------------------------------------------------------
#  Писатели файлов
# ----------------------------------------------------------------------------

def _lines(tracks, opts) -> list[str]:
    return [format_line(t, opts) for t in tracks]


def write_txt(path, tracks, meta, opts):
    owner = (meta.get("owner") or {}).get("name") or (meta.get("owner") or {}).get("login") or "—"
    header = [
        f"🍋 {meta.get('title') or 'Плейлист'}",
        f"Владелец: {owner} · Треков: {len(tracks)}",
        f"Источник: {opts.source_url}",
        "Выжато Vyzhimalo v" + VERSION,
        "",
    ]
    lines = "\n".join(header + _lines(tracks, opts)).rstrip() + "\n"
    open(path, "w", encoding=opts.encoding).write(lines)


def write_md(path, tracks, meta, opts):
    owner = (meta.get("owner") or {}).get("name") or (meta.get("owner") or {}).get("login") or "—"
    with open(path, "w", encoding=opts.encoding) as f:
        f.write(f"# 🍋 {meta.get('title') or 'Плейлист'}\n\n")
        f.write(f"Владелец: {owner} · Треков: {len(tracks)}\n\n")
        f.write(f"*Выжато [Vyzhimalo](https://github.com/deepkorochka/vyzhimalo) v{VERSION} из <{opts.source_url}>.*\n\n")
        f.write("\n".join(_lines(tracks)) + "\n")


def write_csv(path, tracks, meta, opts):
    with open(path, "w", encoding=opts.encoding, newline="") as f:
        w = csv.writer(f)
        w.writerow(["#", "авторы", "песня", "время", "альбом", "год",
                    "доступен", "track_id", "album_id", "ссылка"])
        for t in tracks:
            w.writerow([t.pos, t.artist_line, t.title, t.duration_str,
                        t.album_title, t.year or "", "нет" if not t.available else "да",
                        t.track_id, t.album_id, t.ym_url])


def write_json(path, tracks, meta, opts):
    data = {
        "title": meta.get("title"),
        "owner": meta.get("owner"),
        "source": opts.source_url,
        "generated_by": f"Vyzhimalo v{VERSION}",
        "tracks": [{
            "position": t.pos,
            "artists": t.artists,
            "title": t.title,
            "duration": t.duration_str,
            "duration_ms": t.duration_ms,
            "album": t.album_title,
            "year": t.year,
            "available": t.available,
            "track_id": t.track_id,
            "album_id": t.album_id,
        } for t in tracks],
    }
    with open(path, "w", encoding=opts.encoding) as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")


def write_m3u(path, tracks, meta, opts):
    with open(path, "w", encoding=opts.encoding, newline="\n") as f:
        f.write("#EXTM3U\n")
        for t in tracks:
            sec = round(t.duration_ms / 1000) if t.duration_ms else -1
            f.write(f"#EXTINF:{sec},{t.artist_line} - {t.title}\n")
            f.write(t.ym_url + "\n")


WRITERS = {
    "txt": (write_txt, ".txt"),
    "csv": (write_csv, ".csv"),
    "json": (write_json, ".json"),
    "md": (write_md, ".md"),
    "m3u": (write_m3u, ".m3u"),
}


# ----------------------------------------------------------------------------
#  Консоль
# ----------------------------------------------------------------------------

def setup_stdio():
    for name in ("stdout", "stderr"):
        s = getattr(sys, name)
        try:
            s.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except Exception:
            pass


def info(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


# ----------------------------------------------------------------------------
#  Интерактивный режим (когда запустили без аргументов)
# ----------------------------------------------------------------------------

def ask(prompt: str, default: str) -> str:
    try:
        v = input(f"{prompt} [{default}]: ").strip()
        return v or default
    except EOFError:
        return default


def ask_yn(prompt: str, default: bool) -> bool:
    suffix = "Y/n" if default else "y/N"
    v = ask(f"{prompt} ({suffix})", "").lower()
    return default if not v else v in ("y", "yes", "д", "да", "1")


def wizard(args) -> argparse.Namespace:
    info(f"🍋 Vyzhimalo v{VERSION} — выжималка треков из плейлистов Яндекс Музыки")
    info("Выкийно ссылку на публичный плейлист — получишь аккуратный список.")
    info("")
    url = ask("Ссылка на плейлист", "")
    if not url.strip():
        raise VyzhimaloError(
            "Нужна ссылка на плейлист. Пример: https://music.yandex.ru/playlists/<идентификатор>"
        )
    args.url = url

    o = ask("Что идёт первым? (1 = Авторы - Песня, 2 = Песня - Авторы)", "1")
    args.order = "artist-title" if str(o).strip() in ("1", "", "a", "авторы", "artist") else "title-artist"

    args.sep = ask("Разделитель", " - ") or " - "
    args.duration = ask("Время трека (1 = добавлять [мм:сс], 2 = не добавлять)", "1").strip() != "2"
    args.number = ask_yn("Нумеровать строки?", False)
    args.album = ask_yn("Добавлять название альбома?", False)
    args.year = ask_yn("Добавлять год релиза?", False)
    args.link = ask_yn("Добавлять ссылку на трек?", False)
    args.mark_unavailable = ask_yn("Помечать недоступные треки?", False)

    fmt = ask("Формат (1 = txt, 2 = csv, 3 = json, 4 = md, 5 = m3u)", "1").strip()
    args.format = {"1": "txt", "2": "csv", "3": "json", "4": "md", "5": "m3u"}.get(fmt, "txt")
    return args


# ----------------------------------------------------------------------------
#  CLI
# ----------------------------------------------------------------------------

def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="vyzhimalo",
        description=(
            "🍋 Vyzhimalo — выжималка названий треков из плейлистов Яндекс Музыки.\n"
            "Подайте публичную ссылку на плейлист — получите аккуратный список.\n"
            "Без зависимостей и без авторизации."
        ),
        epilog=(
            "Примеры:\n"
            "  vyzhimalo.py https://music.yandex.ru/playlists/<идентификатор>\n"
            "  vyzhimalo.py <url> --order title-artist --sep ' — ' --no-duration --number\n"
            "  vyzhimalo.py <url> --format csv -o playlist.csv\n"
            "  vyzhimalo.py            (интерактивный режим)\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("url", nargs="?", help="ссылка на плейлист Яндекс Музыки")
    p.add_argument("-o", "--output", help="имя выходного файла (по умолчанию: <название плейлиста>)")
    p.add_argument("--format", default="txt", choices=["txt", "csv", "json", "md", "m3u"],
                   help="формат вывода (по умолчанию txt)")
    p.add_argument("--print", action="store_true",
                   help="вывести список в консоль, не писать файл")

    g = p.add_argument_group("что выводим")
    g.add_argument("--order", default="artist-title",
                   choices=["artist-title", "title-artist", "a-t", "t-a"],
                   help="авторы - песня (по умолчанию) или песня - авторы")
    g.add_argument("--sep", "--separator", dest="sep", default=" - ",
                   help="разделитель между авторами и песней (по умолчанию ' - ')")
    g.add_argument("--duration", action=argparse.BooleanOptionalAction, default=True,
                   help="время трека [мм:сс] (включено по умолчанию, отключить: --no-duration)")
    g.add_argument("--number", action=argparse.BooleanOptionalAction, default=False,
                   help="нумеровать строки (отключено по умолчанию)")
    g.add_argument("--album", action=argparse.BooleanOptionalAction, default=False,
                   help="добавлять название альбома")
    g.add_argument("--year", action=argparse.BooleanOptionalAction, default=False,
                   help="добавлять год релиза")
    g.add_argument("--link", action=argparse.BooleanOptionalAction, default=False,
                   help="добавлять ссылку на трек в Яндекс Музыке")
    g.add_argument("--mark-unavailable", action=argparse.BooleanOptionalAction, default=False,
                   help="помечать недоступные треки словом [недоступен]")

    g2 = p.add_argument_group("поведение")
    g2.add_argument("--keep-duplicates", action=argparse.BooleanOptionalAction, default=False,
                    help="не удалять повторяющиеся треки (по умолчанию дубли выжимаются)")
    g2.add_argument("--encoding", default="utf-8-sig",
                    help="кодировка выходного файла (по умолчанию UTF-8 c BOM — удобно для Excel)")
    g2.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT, help="таймаут запросов, сек")
    g2.add_argument("--retries", type=int, default=DEFAULT_RETRIES, help="число попыток на запрос")
    g2.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    return p


def _normalize(args):
    if args.order == "a-t":
        args.order = "artist-title"
    elif args.order == "t-a":
        args.order = "title-artist"
    if args.sep is None:
        args.sep = " - "
    return args


def main(argv=None) -> int:
    setup_stdio()
    parser = build_arg_parser()
    args = _normalize(parser.parse_args(argv))

    if args.url is None:
        if args.output or getattr(args, "print", False):
            info("(Без ссылки — запускаю интерактивный режим)\n")
        args = wizard(args)
    args.source_url = str(args.url).strip()

    client = YandexMusicClient(timeout=args.timeout, retries=args.retries)
    try:
        meta, tracks = client.fetch_playlist(args.source_url)
    except VyzhimaloError as e:
        info(f"✗ {e}")
        return 2

    owner = meta.get("owner") or {}
    owner_line = owner.get("name") or owner.get("login") or "—"
    tc = meta.get("trackCount") or len(tracks)
    info(f"✓ Плейлист: {meta.get('title')} · владелец: {owner_line} · треков: {len(tracks)}/{tc}")

    if not args.keep_duplicates:
        seen: set = set()
        uniq = []
        dups = 0
        for t in tracks:
            key = (t.track_id, t.album_id) if t.track_id else (t.title.lower(), t.artist_line.lower())
            if key in seen:
                dups += 1
                continue
            seen.add(key)
            uniq.append(t)
        for i, t in enumerate(uniq, 1):
            t.pos = i
        tracks = uniq
        if dups:
            info(f"→ выжато повторов: {dups}")

    if args.print:
        for t in tracks:
            try:
                print(format_line(t, args))
            except UnicodeEncodeError:
                sys.stdout.buffer.write(
                    (format_line(t, args) + "\n").encode("utf-8", "replace")
                )
                sys.stdout.flush()
        return 0

    out = args.output
    if not out:
        _, ext = WRITERS[args.format]
        out = sanitize_filename(meta.get("title") or "playlist") + ext
    if os.path.isdir(out):
        info(f"✗ «{out}» — это папка; укажите имя файла")
        return 2
    writer, _ = WRITERS[args.format]
    try:
        writer(out, tracks, meta, args)
    except PermissionError:
        info(f"✗ Не могу записать {out}: файл занят другой программой или нет прав.")
        return 2
    except OSError as e:
        info(f"✗ Не могу записать {out}: {e}")
        return 2
    info(f"🍋 Выжато в {os.path.abspath(out)}: {len(tracks)} треков.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except VyzhimaloError as e:
        setup_stdio()
        print(f"✗ {e}", file=sys.stderr)
        sys.exit(2)
    except KeyboardInterrupt:
        print("\n✗ Прервано пользователем", file=sys.stderr)
        sys.exit(130)
