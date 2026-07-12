"""
server.py - Jalankan game "Clash of Card" versi web di jaringan lokal.

Cara pakai (di komputer yang jadi HOST):
    python server.py [port]

Default port 8765. Setelah jalan, server akan menampilkan alamat yang bisa
dibuka teman-teman kamu yang terhubung ke WiFi/jaringan yang sama, contoh:
    http://192.168.1.5:8765

Tidak perlu install apapun (tidak ada dependensi luar) - cukup Python 3.
"""

import json
import mimetypes
import os
import socket
import socketserver
import sys
import threading
import time

import ws
from engine import GameRoom, COLORS

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")

room = GameRoom()
room_lock = threading.Lock()

# player_id -> ws.WebSocketConnection
connections = {}
connections_lock = threading.Lock()

def _enable_tcp_keepalive(sock):
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        if hasattr(socket, "TCP_KEEPIDLE"):
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, 20)
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, 5)
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT, 3)
        elif hasattr(socket, "TCP_KEEPALIVE"):
            # macOS pakai nama opsi berbeda dari Linux.
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPALIVE, 20)
    except OSError:
        pass  # kalau platform tidak dukung opsi ini, biarkan (jangan crash)

def get_local_ip():
    # Trik umum: "connect" ke alamat luar (tidak benar2 mengirim data apapun,
    # UDP connect cuma menentukan interface routing) supaya OS kasih tau IP
    # lokal kita yang dipakai untuk keluar jaringan - biasanya itu IP WiFi/LAN.
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


# ---------------- BROADCAST HELPERS ----------------

def _send_safe(player_id, payload_dict):
    with connections_lock:
        conn = connections.get(player_id)
    if conn is None:
        return
    try:
        conn.send(json.dumps(payload_dict))
    except ws.WebSocketClosed:
        pass


def broadcast_lobby():
    state = room.build_lobby_state()
    for pid in list(room.lobby_order):
        _send_safe(pid, state)


def broadcast_game():
    if not room.players:
        return
    for p in room.players:
        state = room.build_state_for(p.player_id)
        if state is not None:
            _send_safe(p.player_id, state)


def send_error(player_id, message):
    _send_safe(player_id, {"type": "error", "message": message})


# ---------------- UNO TIMEOUT WATCHER ----------------

import traceback

def uno_timeout_watcher():
    while True:
        time.sleep(0.25)
        try:
            changed = False
            with room_lock:
                if room.started and not room.game_over:
                    changed = room.check_uno_timeout()
                    changed = room.check_steal_pick_timeout() or changed
            if changed:
                broadcast_game()
        except Exception:
            print("[uno_timeout_watcher] ERROR:")
            traceback.print_exc()


def bot_watcher():
    while True:
        time.sleep(0.6)
        try:
            acted = False
            with room_lock:
                if room.started and not room.game_over:
                    acted = room.maybe_run_bot_turn()
            if acted:
                broadcast_game()
        except Exception:
            print("[bot_watcher] ERROR:")
            traceback.print_exc()


def heartbeat_watcher():
    while True:
        time.sleep(15)
        try:
            with connections_lock:
                conns = list(connections.values())
            for conn in conns:
                conn.send_ping()
        except Exception:
            print("[heartbeat_watcher] ERROR:")
            traceback.print_exc()


# ---------------- MESSAGE HANDLING ----------------

def handle_message(player_id, data):
    msg_type = data.get("type")

    # STABILITY FIX: sebelumnya broadcast_lobby()/broadcast_game() dipanggil
    # DI DALAM blok "with room_lock:" - karena broadcast itu isinya kirim
    # data ke socket (blocking I/O), kalau ada 1 koneksi yang lemot/macet,
    # room_lock bisa ketahan lama dan bikin SEMUA pemain lain freeze nunggu
    # giliran mereka diproses. Sekarang: semua perubahan state dikerjakan di
    # dalam lock (cepat, murni di memori), lalu broadcast dikerjakan SESUDAH
    # lock dilepas, jadi kirim-lambat ke 1 client tidak menahan pemain lain.
    do_broadcast_lobby = False
    do_broadcast_game = False

    with room_lock:
        if msg_type == "leave_lobby":
            if room.started:
                room.mark_lobby_disconnected(player_id)
                do_broadcast_game = True
            else:
                room.remove_lobby_player(player_id)
            do_broadcast_lobby = True

        elif msg_type == "set_settings":
            if player_id == room.leader_id and not room.started:
                room.pack_count = max(1, min(4, int(data.get("pack_count", room.pack_count))))
                room.cards_each = max(3, min(20, int(data.get("cards_each", room.cards_each))))
                room.max_players = max(2, min(10, int(data.get("max_players", room.max_players))))
                if "bot_count" in data:
                    room.bot_count = max(0, min(9, int(data.get("bot_count", room.bot_count))))
                if "extreme_mode" in data:
                    room.extreme_mode = bool(data.get("extreme_mode"))
                if "extreme_pack_count" in data:
                    room.extreme_pack_count = max(1, min(8, int(data.get("extreme_pack_count", room.extreme_pack_count))))
                do_broadcast_lobby = True

        elif msg_type == "set_name":
            # FITUR BARU: ganti nama selagi masih di lobby.
            new_name = data.get("name")
            if room.rename_player(player_id, new_name):
                do_broadcast_lobby = True

        elif msg_type == "start_game":
            if player_id == room.leader_id and not room.started:
                ok = room.start_game(room.pack_count, room.cards_each, room.bot_count, room.extreme_mode, room.extreme_pack_count)
                if ok:
                    do_broadcast_game = True
                else:
                    do_broadcast_lobby = True

        elif msg_type == "rematch":
            if player_id == room.leader_id:
                room.rematch()
                do_broadcast_game = True

        elif msg_type == "back_to_lobby":
            # FITUR BARU: leader bisa membawa semua orang kembali ke lobby
            # (misalnya supaya pemain baru sempat gabung) tanpa langsung
            # rematch. Roster lobby tetap dipertahankan.
            if player_id == room.leader_id:
                room.back_to_lobby()
                do_broadcast_lobby = True

        elif room.started:
            if msg_type == "play_cards":
                indices = data.get("indices", [])
                if isinstance(indices, list):
                    room.play_cards(player_id, indices)
                    do_broadcast_game = True
            elif msg_type == "draw_card":
                room.draw_card(player_id)
                do_broadcast_game = True
            elif msg_type == "choose_wild_color":
                room.choose_wild_color(player_id, data.get("color"))
                do_broadcast_game = True
            elif msg_type == "choose_number_color":
                room.choose_number_color(player_id, data.get("color"))
                do_broadcast_game = True
            elif msg_type == "call_clash":
                room.call_clash(player_id)
                do_broadcast_game = True
            elif msg_type == "use_stash_item":
                stash_id = data.get("stash_id")
                target_player_id = data.get("target_player_id")
                if isinstance(stash_id, str):
                    room.use_stash_item(player_id, stash_id, target_player_id)
                    do_broadcast_game = True
            elif msg_type == "pick_steal_card":
                idx = data.get("card_index")
                if isinstance(idx, int):
                    room.pick_steal_card(player_id, idx)
                    do_broadcast_game = True

    # Broadcast di luar lock (lihat catatan STABILITY FIX di atas).
    if do_broadcast_lobby:
        broadcast_lobby()
    if do_broadcast_game:
        broadcast_game()


# ---------------- HTTP + WEBSOCKET HANDLER ----------------

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
}


class Handler(socketserver.BaseRequestHandler):
    def handle(self):
        self.request.settimeout(30)
        try:
            data = b""
            while b"\r\n\r\n" not in data:
                chunk = self.request.recv(4096)
                if not chunk:
                    return
                data += chunk
                if len(data) > 16384:
                    return
            header_blob, _, _rest = data.partition(b"\r\n\r\n")
            lines = header_blob.decode("utf-8", errors="replace").split("\r\n")
            if not lines:
                return
            request_line = lines[0]
            parts = request_line.split(" ")
            if len(parts) < 2:
                return
            method, path = parts[0], parts[1]

            headers = {}
            for line in lines[1:]:
                if ":" in line:
                    k, v = line.split(":", 1)
                    headers[k.strip().lower()] = v.strip()

            if headers.get("upgrade", "").lower() == "websocket":
                self.handle_websocket(headers)
            else:
                self.handle_http(method, path)
        except (ConnectionResetError, BrokenPipeError, socket.timeout, OSError):
            pass

    # ---- static file serving ----

    def handle_http(self, method, path):
        if path == "/":
            path = "/index.html"
        # Sanitasi path sederhana biar tidak bisa keluar dari folder static/.
        safe_path = os.path.normpath(path).lstrip(os.sep)
        full_path = os.path.join(STATIC_DIR, safe_path)
        if not os.path.abspath(full_path).startswith(os.path.abspath(STATIC_DIR)):
            self._respond(403, b"Forbidden", "text/plain")
            return
        if not os.path.isfile(full_path):
            self._respond(404, b"Not found", "text/plain")
            return

        ext = os.path.splitext(full_path)[1]
        content_type = CONTENT_TYPES.get(ext) or mimetypes.guess_type(full_path)[0] or "application/octet-stream"
        with open(full_path, "rb") as f:
            body = f.read()
        self._respond(200, body, content_type)

    def _respond(self, status, body, content_type):
        status_text = {200: "OK", 403: "Forbidden", 404: "Not Found"}.get(status, "OK")
        response = (
            f"HTTP/1.1 {status} {status_text}\r\n"
            f"Content-Type: {content_type}\r\n"
            f"Content-Length: {len(body)}\r\n"
            "Connection: close\r\n"
            "\r\n"
        ).encode("utf-8") + body
        self.request.sendall(response)

    # ---- websocket lifecycle ----

    def handle_websocket(self, headers):
        response = ws.build_handshake_response(headers)
        if response is None:
            return
        self.request.sendall(response)
        self.request.settimeout(None)
        _enable_tcp_keepalive(self.request)

        conn = ws.WebSocketConnection(self.request)

        player_id = None
        try:
            first_msg = conn.recv()
            if first_msg is None:
                return
            data = json.loads(first_msg)

            with room_lock:
                incoming_pid = data.get("player_id")
                incoming_session = data.get("session_id")
                is_valid_reconnect = (
                    data.get("type") in ("join", "rejoin")
                    and incoming_pid in room.lobby_players
                    and incoming_session == room.session_id
                )
                if is_valid_reconnect:
                    player_id = incoming_pid
                    room.mark_lobby_reconnected(player_id)
                else:
                    if room.started:
                        conn.send(json.dumps({"type": "error", "message": "Game sudah berjalan, tidak bisa join baru."}))
                        return
                    name = str(data.get("name") or "Player")[:20]
                    reclaimed_id = room.find_disconnected_lobby_player_by_name(name)
                    if reclaimed_id is not None:
                        player_id = reclaimed_id
                        room.mark_lobby_reconnected(player_id)
                        room.rename_player(player_id, name)
                    else:
                        if room.connected_lobby_count() >= room.max_players:
                            conn.send(json.dumps({"type": "error", "message": "Lobby penuh."}))
                            return
                        player_id = room.add_lobby_player(name)

            with connections_lock:
                connections[player_id] = conn

            conn.send(json.dumps({"type": "joined", "player_id": player_id, "session_id": room.session_id, "name": room.lobby_players.get(player_id, {}).get("name", "Player")}))

            with room_lock:
                if room.started:
                    state = room.build_state_for(player_id)
                    if state:
                        conn.send(json.dumps(state))
                else:
                    pass

            broadcast_lobby()
            if room.started:
                broadcast_game()

            while True:
                message = conn.recv()
                if message is None:
                    break
                try:
                    data = json.loads(message)
                except (ValueError, TypeError):
                    continue
                handle_message(player_id, data)

        except ws.WebSocketClosed:
            pass
        except (ConnectionResetError, BrokenPipeError, OSError):
            pass
        finally:
            if player_id is not None:
                with connections_lock:
                    connections.pop(player_id, None)
                with room_lock:
                    room.mark_lobby_disconnected(player_id)
                broadcast_lobby()
                if room.started:
                    broadcast_game()
            conn.close()


class ThreadingServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    daemon_threads = True
    allow_reuse_address = True


def main():
    port = 8765
    if len(sys.argv) > 1:
        try:
            port = int(sys.argv[1])
        except ValueError:
            pass

    threading.Thread(target=uno_timeout_watcher, daemon=True).start()
    threading.Thread(target=heartbeat_watcher, daemon=True).start()
    threading.Thread(target=bot_watcher, daemon=True).start()

    server = ThreadingServer(("0.0.0.0", port), Handler)
    local_ip = get_local_ip()

    print("=" * 52)
    print(" CLASH OF CARD - Web Server")
    print("=" * 52)
    print(f" Buka di komputer ini : http://localhost:{port}")
    print(f" Buka di HP/laptop lain di WiFi yang sama:")
    print(f"     http://{local_ip}:{port}")
    print("=" * 52)
    print(" Tekan CTRL+C untuk stop server.")
    print()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nServer dihentikan.")
        server.shutdown()


if __name__ == "__main__":
    main()
