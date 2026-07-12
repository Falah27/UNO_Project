"""
ws.py - Implementasi WebSocket server minimal (RFC 6455), murni Python
standard library. Ditulis manual karena environment build ini tidak punya
akses internet untuk `pip install websockets`. Untuk pemakaian normal user
nanti (menjalankan game di komputer sendiri), ini juga artinya TIDAK ADA
dependensi pihak ketiga yang perlu diinstal - cukup Python bawaan.

Cuma mendukung apa yang dibutuhkan game ini: text frame (JSON), close, ping/pong.
"""

import base64
import hashlib
import struct
import threading

WS_MAGIC = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

OPCODE_CONTINUATION = 0x0
OPCODE_TEXT = 0x1
OPCODE_BINARY = 0x2
OPCODE_CLOSE = 0x8
OPCODE_PING = 0x9
OPCODE_PONG = 0xA

# HARDENING: batas atas ukuran payload 1 frame yang mau kita terima. Game ini
# cuma kirim JSON kecil (state room/kartu), jadi 1 MB sudah jauh lebih dari
# cukup. Tanpa batas ini, client yang salah kirim/nakal bisa klaim panjang
# payload raksasa (field length 64-bit) dan bikin _recv_exact menunggu tanpa
# henti / server mencoba alokasi buffer sangat besar.
MAX_FRAME_PAYLOAD = 1 << 20  # 1 MB


class WebSocketClosed(Exception):
    pass


def compute_accept_key(client_key):
    sha1 = hashlib.sha1((client_key + WS_MAGIC).encode("utf-8")).digest()
    return base64.b64encode(sha1).decode("utf-8")


def build_handshake_response(headers):
    key = headers.get("sec-websocket-key")
    if not key:
        return None
    accept = compute_accept_key(key)
    response = (
        "HTTP/1.1 101 Switching Protocols\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Accept: {accept}\r\n"
        "\r\n"
    )
    return response.encode("utf-8")


def _recv_exact(sock, n):
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise WebSocketClosed("connection closed while reading")
        buf += chunk
    return buf


def recv_frame(sock):
    """Baca satu frame WebSocket dari client (client selalu masking payload-nya
    sesuai spec). Return (opcode, payload_bytes)."""
    header = _recv_exact(sock, 2)
    b0, b1 = header[0], header[1]

    opcode = b0 & 0x0F
    masked = (b1 & 0x80) != 0
    length = b1 & 0x7F

    if length == 126:
        length = struct.unpack(">H", _recv_exact(sock, 2))[0]
    elif length == 127:
        length = struct.unpack(">Q", _recv_exact(sock, 8))[0]

    # HARDENING: tolak frame yang mengklaim payload lebih besar dari batas
    # wajar - putuskan koneksi ini secara bersih daripada mencoba membaca
    # data sebanyak itu (lihat MAX_FRAME_PAYLOAD di atas).
    if length > MAX_FRAME_PAYLOAD:
        raise WebSocketClosed("frame payload too large")

    if masked:
        mask_key = _recv_exact(sock, 4)
    else:
        mask_key = None

    payload = _recv_exact(sock, length) if length > 0 else b""

    if masked and mask_key:
        unmasked = bytearray(payload)
        for i in range(len(unmasked)):
            unmasked[i] ^= mask_key[i % 4]
        payload = bytes(unmasked)

    return opcode, payload


def _build_frame(opcode, payload_bytes):
    length = len(payload_bytes)
    first_byte = 0x80 | opcode  # FIN=1

    if length < 126:
        header = struct.pack("!BB", first_byte, length)
    elif length < (1 << 16):
        header = struct.pack("!BBH", first_byte, 126, length)
    else:
        header = struct.pack("!BBQ", first_byte, 127, length)

    return header + payload_bytes


def send_text(sock, text):
    payload = text.encode("utf-8")
    sock.sendall(_build_frame(OPCODE_TEXT, payload))


def send_close(sock, code=1000, reason=""):
    payload = struct.pack("!H", code) + reason.encode("utf-8")
    try:
        sock.sendall(_build_frame(OPCODE_CLOSE, payload))
    except OSError:
        pass


def send_pong(sock, payload=b""):
    sock.sendall(_build_frame(OPCODE_PONG, payload))


class WebSocketConnection:
    """Wrapper kecil di atas raw socket supaya kode server terasa seperti
    'recv_message() / send_message()' biasa, tanpa perlu tahu detail framing."""

    def __init__(self, sock):
        self.sock = sock
        self.closed = False
        # Lock ini melindungi penulisan frame ke socket, karena sekarang ada
        # 2 sumber yang bisa mengirim ke koneksi yang sama secara bersamaan:
        # thread heartbeat (kirim ping berkala) dan thread broadcast biasa
        # (kirim update state). Tanpa lock, dua frame bisa "nyampur" jadi satu
        # kiriman yang rusak di sisi client.
        self.write_lock = threading.Lock()

    def send(self, text):
        if self.closed:
            return
        try:
            with self.write_lock:
                send_text(self.sock, text)
        except OSError:
            self.closed = True
            raise WebSocketClosed("send failed")

    def send_ping(self):
        if self.closed:
            return
        try:
            with self.write_lock:
                self.sock.sendall(_build_frame(OPCODE_PING, b"hb"))
        except OSError:
            self.closed = True

    def recv(self):
        """Return string pesan berikutnya, atau None kalau koneksi ditutup."""
        while True:
            try:
                opcode, payload = recv_frame(self.sock)
            except (WebSocketClosed, OSError, ConnectionResetError):
                self.closed = True
                return None

            if opcode == OPCODE_CLOSE:
                send_close(self.sock)
                self.closed = True
                return None
            elif opcode == OPCODE_PING:
                try:
                    send_pong(self.sock, payload)
                except OSError:
                    self.closed = True
                    return None
                continue
            elif opcode == OPCODE_PONG:
                continue
            elif opcode == OPCODE_TEXT:
                try:
                    return payload.decode("utf-8", errors="replace")
                except Exception:
                    continue
            else:
                # binary/continuation tidak dipakai game ini, abaikan saja
                continue

    def close(self):
        if not self.closed:
            try:
                with self.write_lock:
                    send_close(self.sock)
            except OSError:
                pass
            self.closed = True
        try:
            self.sock.close()
        except OSError:
            pass
