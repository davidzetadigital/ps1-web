#!/usr/bin/env python3
"""Servidor local para el emulador web de PS1.

Sirve la página (index.html) y la biblioteca de juegos desde una carpeta
(por defecto ~/Downloads/PS1). Los juegos .bin/.cue se empaquetan al vuelo
en un .zip sin compresión para que EmulatorJS reciba todas las pistas.

Uso:
    python3 server.py                 # solo este Mac, http://localhost:8080
    python3 server.py --lan           # también desde otros dispositivos de tu red
    python3 server.py --library RUTA  # otra carpeta de juegos
"""
import argparse
import json
import mimetypes
import os
import re
import shutil
import socket
import urllib.parse
import zipfile
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
GAME_EXTS = {".cue", ".chd", ".pbp", ".iso", ".m3u"}
BIOS_RE = re.compile(r"^scph.*\.bin$|^psxonpsp.*\.bin$|^ps1_rom\.bin$", re.I)


def scan_library(root):
    """Devuelve {id: info} para cada juego y la BIOS encontrada (si hay)."""
    games, bios = {}, None
    referenced = set()  # archivos .cue citados por un .m3u, para no duplicarlos
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for name in filenames:
            path = os.path.join(dirpath, name)
            ext = os.path.splitext(name)[1].lower()
            if BIOS_RE.match(name) and os.path.getsize(path) == 512 * 1024:
                bios = bios or path
            elif ext == ".m3u":
                for line in open(path, errors="ignore"):
                    line = line.strip()
                    if line and not line.startswith("#"):
                        referenced.add(os.path.normpath(os.path.join(dirpath, line)))
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for name in sorted(filenames):
            path = os.path.join(dirpath, name)
            ext = os.path.splitext(name)[1].lower()
            if ext not in GAME_EXTS or os.path.normpath(path) in referenced:
                continue
            if ext == ".iso" and any(f.lower().endswith(".cue") for f in filenames):
                continue
            rel = os.path.relpath(path, root)
            title = os.path.splitext(name)[0]
            title = re.sub(r"\s*[\(\[][^\)\]]*[\)\]]", "", title).strip() or title
            games[rel] = {
                "id": rel,
                "title": title,
                "format": ext[1:].upper(),
                "size": sum(os.path.getsize(f) for f in disc_files(path)),
            }
    return games, bios


def cue_tracks(cue_path):
    base = os.path.dirname(cue_path)
    files = []
    for line in open(cue_path, errors="ignore"):
        m = re.match(r'\s*FILE\s+"?(.+?)"?\s+\w+\s*$', line, re.I)
        if m:
            files.append(os.path.join(base, m.group(1)))
    return files


def disc_files(path):
    """Todos los archivos que forman un juego (cue + bins, m3u + discos...)."""
    ext = os.path.splitext(path)[1].lower()
    if ext == ".cue":
        return [path] + [f for f in cue_tracks(path) if os.path.exists(f)]
    if ext == ".m3u":
        out = [path]
        base = os.path.dirname(path)
        for line in open(path, errors="ignore"):
            line = line.strip()
            if line and not line.startswith("#"):
                out += disc_files(os.path.join(base, line))
        return out
    return [path]


class Handler(SimpleHTTPRequestHandler):
    library = None

    def __init__(self, *a, **kw):
        super().__init__(*a, directory=HERE, **kw)

    def log_message(self, fmt, *args):
        if "/game/" in self.path or "/bios" in self.path:
            super().log_message(fmt, *args)

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        path = urllib.parse.unquote(url.path)
        if path == "/api/games":
            games, bios = scan_library(self.library)
            body = json.dumps({
                "library": self.library,
                "bios": os.path.basename(bios) if bios else None,
                "games": sorted(games.values(), key=lambda g: g["title"].lower()),
            }).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif path.startswith("/bios/"):
            _, bios = scan_library(self.library)
            if not bios:
                return self.send_error(404, "Sin BIOS")
            self.send_file(bios)
        elif path.startswith("/game/"):
            self.send_game(path[len("/game/"):])
        else:
            super().do_GET()

    def send_file(self, path):
        self.send_response(200)
        self.send_header("Content-Type", mimetypes.guess_type(path)[0] or "application/octet-stream")
        self.send_header("Content-Length", str(os.path.getsize(path)))
        self.end_headers()
        with open(path, "rb") as f:
            shutil.copyfileobj(f, self.wfile, 1024 * 1024)

    def send_game(self, rel):
        # rel termina en ".zip" (cue/m3u empaquetados) o es el archivo tal cual
        games, _ = scan_library(self.library)
        key = rel[:-4] if rel.endswith(".zip") and rel[:-4] in games else rel
        if key not in games:
            return self.send_error(404, "Juego no encontrado")
        path = os.path.join(self.library, key)
        files = disc_files(path)
        try:
            if len(files) == 1:
                return self.send_file(path)
            base = os.path.dirname(path)
            self.send_response(200)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Length", str(stored_zip_size(files, base)))
            self.end_headers()
            with zipfile.ZipFile(self.wfile, "w", zipfile.ZIP_STORED, allowZip64=True) as z:
                for f in files:
                    z.write(f, os.path.relpath(f, base))
        except (BrokenPipeError, ConnectionResetError):
            pass


def stored_zip_size(files, base):
    """Tamaño exacto del zip sin compresión, para que el navegador muestre progreso."""
    class Counter:
        n = 0
        def write(self, b):
            self.n += len(b)
            return len(b)
        def flush(self):
            pass
    c = Counter()
    # mismo camino que send_game, para que el tamaño coincida byte a byte
    with zipfile.ZipFile(c, "w", zipfile.ZIP_STORED, allowZip64=True) as z:
        for f in files:
            z.write(f, os.path.relpath(f, base))
    return c.n


def lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--library", default=os.path.expanduser("~/Downloads/PS1"))
    p.add_argument("--port", type=int, default=8080)
    p.add_argument("--lan", action="store_true", help="aceptar conexiones de tu red local")
    args = p.parse_args()
    Handler.library = os.path.abspath(args.library)
    os.makedirs(Handler.library, exist_ok=True)
    host = "0.0.0.0" if args.lan else "127.0.0.1"
    server = ThreadingHTTPServer((host, args.port), Handler)
    print(f"Biblioteca: {Handler.library}")
    print(f"Abre http://localhost:{args.port}")
    if args.lan and lan_ip():
        print(f"Desde otro dispositivo: http://{lan_ip()}:{args.port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
