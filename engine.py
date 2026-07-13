"""
engine.py - Logic murni "Clash of Card" (UNO-like), tanpa dependensi pygame.

Ini porting dari MultiPlayerGameEngine di versi desktop (unogame_fixed2.py),
supaya bisa dipakai di server web. Semua rule sudah pernah diuji di versi
desktop; dua penyesuaian yang beda dari versi desktop:

1. Kartu angka kembar TIDAK otomatis semuanya keluar. Pemain memilih sendiri
   berapa banyak yang mau dimainkan sekaligus lewat play_cards(indices).
2. Kalau giliran seorang pemain dimulai dan dia BENAR-BENAR tidak punya kartu
   valid, dia otomatis dapat kartu baru (tidak perlu klik tombol DRAW lagi).
   Tombol DRAW manual tetap ada untuk yang mau narik kartu secara sukarela.
"""

import random
import time
import uuid

COLORS = ["Red", "Blue", "Green", "Yellow"]
VALUES = [
    "0", "1", "2", "3", "4",
    "5", "6", "7", "8", "9",
    "Skip", "Reverse", "Draw Two"
]

# FITUR BARU: MODE EXTREME. 5 jenis kartu spesial yang, kalau mode Extreme
# aktif, dicampur acak ke deck utama (jadi ke-draw seperti kartu biasa).
# Begitu ke-draw, kartu ini TIDAK masuk ke tangan (hand) biasa - otomatis
# masuk ke "stash" milik pemain (lihat Player.draw_card), supaya tidak ganggu
# hitungan CLASH/UNO dan tidak bisa dimainkan lewat pencocokan warna/angka
# biasa. Pemain memilih sendiri kapan mau memakainya, tapi cuma di giliran
# sendiri, sebagai pengganti aksi normal (main kartu / tarik kartu).
EXTREME_KINDS = ["Swap Rotasi", "+2 Skip", "+2 Reverse", "Curi", "Bom Waktu", "Recall"]
EXTREME_CARD_COPIES = {
    "Swap Rotasi": 2,
    "+2 Skip": 2,
    "+2 Reverse": 2,
    "Curi": 3,
    "Recall": 3,
    "Bom Waktu": 1,
}  
RECALL_START_CARDS = 2
BOM_WAKTU_TURNS = 5


def now_ms():
    return int(time.time() * 1000)


class Card:
    __slots__ = ("color", "value")

    def __init__(self, color, value):
        self.color = color
        self.value = value

    def can_play_on(self, top_card, current_color):
        if self.color == "Wild":
            return True
        return self.color == current_color or self.value == top_card.value

    def is_extreme(self):
        return self.color == "Extreme"

    def to_dict(self):
        return {"color": self.color, "value": self.value}

    def __repr__(self):
        return f"{self.color} {self.value}"


def dict_to_card(data):
    return Card(data["color"], data["value"])


class Deck:
    def __init__(self, pack_count=1, extreme_mode=False, extreme_pack_count=1):
        self.cards = []
        self.discard_pile = []
        self.pack_count = max(1, int(pack_count))
        self.extreme_mode = extreme_mode
        self.extreme_pack_count = max(1, int(extreme_pack_count))
        self.build_deck()
        self.shuffle()

    def build_deck(self):
        self.cards = []
        for _pack in range(self.pack_count):
            for color in COLORS:
                # FIX: angka "0" cuma 1 lembar per warna (standar UNO), beda
                # dari angka 1-9 dan kartu aksi yang masing-masing 2 lembar
                # per warna. Sebelumnya "0" ikut digandakan jadi 2 lembar,
                # bikin total 1 pack jadi 112 kartu, harusnya 108.
                self.cards.append(Card(color, "0"))
                for value in VALUES:
                    if value == "0":
                        continue
                    self.cards.append(Card(color, value))
                    self.cards.append(Card(color, value))
            for _ in range(4):
                self.cards.append(Card("Wild", "Wild"))
            for _ in range(4):
                self.cards.append(Card("Wild", "Wild Draw Four"))

            if self.extreme_mode:
                for _extreme_pack in range(self.extreme_pack_count):
                    for kind in EXTREME_KINDS:
                        copies = EXTREME_CARD_COPIES.get(kind, 2)
                        for _ in range(copies):
                            self.cards.append(Card("Extreme", kind))

    def shuffle(self):
        random.shuffle(self.cards)

    def draw(self):
        # Kalau draw pile habis, kocok ulang dari discard pile dulu sebelum menyerah.
        if len(self.cards) == 0 and self.discard_pile:
            self.cards.extend(self.discard_pile)
            self.discard_pile = []
            self.shuffle()
        if len(self.cards) > 0:
            return self.cards.pop()
        return None

    def discard(self, card):
        if card is not None:
            self.discard_pile.append(card)


class Player:
    def __init__(self, player_id, name, is_bot=False):
        self.player_id = player_id
        self.name = name
        self.hand = []
        self.connected = True
        self.finished = False
        self.uno_called = False
        self.is_bot = is_bot  # FITUR BARU: main vs bot
        # FITUR BARU: MODE EXTREME. Kartu spesial yang ke-draw disimpan di
        # sini, terpisah dari hand biasa. Tiap item: {"id", "kind", "turns_left"}.
        # "turns_left" cuma dipakai Bom Waktu (None untuk kartu lain).
        self.stash = []
        self.recall_used = False

    def draw_card(self, deck, amount=1):
        """Menarik `amount` kartu dari deck. Kartu Extreme TIDAK masuk ke
        hand - otomatis dipisah ke stash (lihat EXTREME_KINDS di atas)."""
        drawn = []
        for _ in range(amount):
            card = deck.draw()
            if card is None:
                break
            drawn.append(card)
            if card.is_extreme():
                self.stash.append({
                    "id": uuid.uuid4().hex[:8],
                    "kind": card.value,
                    "turns_left": BOM_WAKTU_TURNS if card.value == "Bom Waktu" else None,
                })
            else:
                self.hand.append(card)
        return drawn


class GameRoom:
    """Satu ruang permainan (server ini cuma menjalankan satu room sekaligus,
    persis seperti versi desktop yang cuma bisa hosting 1 game per proses)."""

    def __init__(self):
        # STABILITY FIX: session_id unik dibuat ulang setiap kali proses server
        # dijalankan/direstart. Browser menyimpan session_id ini bersama
        # player_id-nya di localStorage. Kalau server sempat direstart (atau
        # user buka game dari sesi lama), session_id yang tersimpan di browser
        # tidak akan cocok lagi dengan session_id server yang baru, jadi browser
        # otomatis dianggap "pemain baru" - TIDAK BISA nabrak/nyangkut ke id
        # pemain lain yang kebetulan dapat nomor yang sama di sesi baru.
        self.session_id = uuid.uuid4().hex[:10]

        self.players = []          # list[Player], urutan = urutan giliran
        self.deck = Deck()
        self.top_card = None
        self.top_card_history = []  # kartu2 top_card sebelumnya, buat tampilan "riwayat"
        self.current_color = None
        self.current_player_index = 0
        self.direction = 1         # 1 = searah jarum jam, -1 = berlawanan
        self.game_over = False
        self.winner_id = None
        self.message = ""
        self.started = False

        self.awaiting_wild_color = False
        self.wild_player_id = None

        self.awaiting_number_color = False
        self.number_color_player_id = None
        self.pending_number_value = None
        self.pending_number_color_options = []
        
        self.awaiting_steal_pick = False
        self.steal_player_id = None
        self.steal_target_id = None
        self.steal_pick_deadline_ms = 0

        self.finished_order = []

        self.pending_uno_player_id = None
        self.pending_uno_deadline_ms = 0
        self.uno_timeout_ms = 4000

        self.pending_uno_deadlines = {}  # {player_id: deadline_ms}
        self.uno_timeout_ms = 4000

        self.pack_count = 1
        self.cards_each = 7
        self.max_players = 6
        self.bot_count = 0  # FITUR BARU: jumlah bot yang mengisi kursi kosong
        self.extreme_mode = False  # FITUR BARU: mode Extreme (kartu spesial)
        self.extreme_pack_count = 1  # jumlah paket kartu spesial saat Extreme aktif

        # FITUR BARU: penanda "kejadian" kartu Extreme dipakai (Swap Rotasi,
        # +2 Skip, dst) supaya frontend bisa nampilin animasi efek yang beda2
        # per jenis kartu ke SEMUA pemain, bukan cuma yang makai. game_session_id
        # dipakai buat tahu "game baru dimulai" (popup penjelasan Extreme mode).
        self.game_session_id = 0
        self.event_seq = 0
        self.last_event = None

        # Lobby: player_id -> {"name":..., "connected":...}
        self.lobby_order = []      # urutan join, elemen = player_id
        self.lobby_players = {}
        self.next_player_id = 1
        self.leader_id = None      # player pertama yang masih connect = "leader" lobby

    def kick_lobby_player(self, requester_id, target_id):
        # FITUR BARU: leader bisa buang slot pemain yang statusnya Offline di
        # lobby - mencegah lobby menumpuk baris "Offline" yang tidak pernah
        # balik, tanpa perlu tunggu mereka reconnect dulu untuk bisa KELUAR.
        if requester_id != self.leader_id:
            self.message = "Cuma leader yang bisa kick pemain."
            return False
        if self.started:
            self.message = "Tidak bisa kick saat game sedang berjalan."
            return False
        info = self.lobby_players.get(target_id)
        if info is None:
            self.message = "Pemain tidak ditemukan."
            return False
        if info.get("connected"):
            self.message = "Cuma bisa kick pemain yang statusnya Offline."
            return False
        self.remove_lobby_player(target_id)
        self.message = f"{info.get('name', 'Pemain')} dikeluarkan dari lobby."
        return True
    # ---------------- LOBBY ----------------

    def add_lobby_player(self, name):
        base_name = (name or "Player").strip()[:20] or "Player"
        existing_names = {
            info["name"].strip().lower()
            for pid, info in self.lobby_players.items()
            if info.get("connected")
        }
        final_name = base_name
        suffix = 2
        while final_name.strip().lower() in existing_names:
            final_name = f"{base_name} ({suffix})"
            suffix += 1

        player_id = self.next_player_id
        self.next_player_id += 1
        self.lobby_order.append(player_id)
        self.lobby_players[player_id] = {"name": final_name, "connected": True}
        if self.leader_id is None:
            self.leader_id = player_id
        return player_id

    def find_disconnected_lobby_player_by_name(self, name):
        """Reclaim slot offline dengan nama sama supaya lobby tidak menumpuk
        user lama sebagai Offline saat orang yang sama masuk lagi tanpa sesi
        browser lama."""
        target = (name or "").strip().lower()
        if not target:
            return None
        for pid in self.lobby_order:
            info = self.lobby_players.get(pid)
            if info and not info.get("connected") and info.get("name", "").strip().lower() == target:
                return pid
        return None

    def remove_lobby_player(self, player_id):
        """Keluar bersih dari lobby. Dipakai tombol KELUAR supaya tidak
        meninggalkan baris Offline yang menumpuk."""
        existed = player_id in self.lobby_players
        if existed:
            self.lobby_players.pop(player_id, None)
            self.lobby_order = [pid for pid in self.lobby_order if pid != player_id]
            if self.leader_id == player_id:
                self.leader_id = self._pick_new_leader()
        return existed

    def mark_lobby_disconnected(self, player_id):
        if self.started:
            if player_id in self.lobby_players:
                self.lobby_players[player_id]["connected"] = False
            if self.leader_id == player_id:
                self.leader_id = self._pick_new_leader()
            self.mark_player_disconnected(player_id)
            return
        self.remove_lobby_player(player_id)

    def mark_lobby_reconnected(self, player_id):
        if player_id in self.lobby_players:
            self.lobby_players[player_id]["connected"] = True
        if self.leader_id is None:
            self.leader_id = self._pick_new_leader()
        if self.started:
            p = self.get_player(player_id)
            if p is not None:
                p.connected = True

    def _pick_new_leader(self):
        for pid in self.lobby_order:
            info = self.lobby_players.get(pid)
            if info and info.get("connected"):
                return pid
        return None

    def rename_player(self, player_id, new_name):
        # FITUR BARU: ganti nama selagi masih di lobby (belum game dimulai).
        if self.started:
            return False
        new_name = (new_name or "").strip()[:20]
        if not new_name or player_id not in self.lobby_players:
            return False
        self.lobby_players[player_id]["name"] = new_name
        return True

    def build_lobby_state(self):
        players = []
        for pid in self.lobby_order:
            info = self.lobby_players.get(pid, {})
            players.append({
                "id": pid,
                "name": info.get("name", f"Player {pid}"),
                "connected": info.get("connected", False),
                "is_leader": pid == self.leader_id,
            })
        return {
            "type": "lobby_state",
            "players": players,
            "max_players": self.max_players,
            "pack_count": self.pack_count,
            "cards_each": self.cards_each,
            "bot_count": self.bot_count,
            "extreme_mode": self.extreme_mode,
            "extreme_pack_count": self.extreme_pack_count,
            "leader_id": self.leader_id,
            "started": self.started,
        }

    def connected_lobby_count(self):
        return len([1 for info in self.lobby_players.values() if info.get("connected")])

    # ---------------- START GAME ----------------

    def start_game(self, pack_count=1, cards_each=7, bot_count=None, extreme_mode=None, extreme_pack_count=None):
        active_ids = [pid for pid in self.lobby_order if self.lobby_players.get(pid, {}).get("connected")]

        if bot_count is None:
            bot_count = self.bot_count
        bot_count = max(0, int(bot_count))

        if extreme_mode is None:
            extreme_mode = self.extreme_mode
        if extreme_pack_count is None:
            extreme_pack_count = self.extreme_pack_count

        total_seats = len(active_ids) + bot_count
        if total_seats < 2:
            self.message = "Minimal 2 pemain (manusia + bot) untuk mulai."
            return False
        if len(active_ids) < 1:
            self.message = "Minimal 1 pemain manusia untuk mulai."
            return False

        self.pack_count = max(1, int(pack_count))
        self.cards_each = max(1, int(cards_each))
        self.bot_count = bot_count
        self.extreme_mode = bool(extreme_mode)
        self.extreme_pack_count = max(1, int(extreme_pack_count))
        self.deck = Deck(pack_count=self.pack_count, extreme_mode=self.extreme_mode, extreme_pack_count=self.extreme_pack_count)

        self.players = []
        for pid in active_ids:
            info = self.lobby_players[pid]
            p = Player(pid, info.get("name", f"Player {pid}"))
            self.players.append(p)

        # FITUR BARU: main vs bot. Bot dapat player_id NEGATIF supaya tidak
        # akan pernah bentrok dengan id pemain asli (yang selalu positif dan
        # naik terus dari next_player_id).
        for i in range(1, bot_count + 1):
            bot = Player(-i, f"Bot {i}", is_bot=True)
            self.players.append(bot)

        # Acak urutan duduk supaya siapa jalan duluan & posisi bot bervariasi
        # tiap game, bukan selalu urutan join / selalu bot di belakang.
        random.shuffle(self.players)

        # FITUR BARU: MODE EXTREME. Kartu extreme SENGAJA disisihkan dulu
        # sebelum bagi-bagi kartu awal, supaya tangan awal (7 kartu pertama)
        # selalu bersih dari kartu spesial - kartu extreme baru bisa didapat
        # dari nge-draw setelah game benar-benar berjalan, bukan dari deal
        # awal. Sesudah semua kebagian, kartu-kartu ini dicampur balik ke
        # sisa deck (dikocok) supaya nanti ke-draw seperti kartu biasa.
        held_out_extreme = [c for c in self.deck.cards if c.is_extreme()]
        if held_out_extreme:
            self.deck.cards = [c for c in self.deck.cards if not c.is_extreme()]

        for p in self.players:
            p.draw_card(self.deck, self.cards_each)

        if held_out_extreme:
            self.deck.cards.extend(held_out_extreme)
            self.deck.shuffle()

        self.top_card = self.deck.draw()
        # JAGA-JAGA: top_card awal tidak boleh kartu Extreme (kartu itu tidak
        # bisa "dicocokkan" seperti kartu normal). Kalau ke-draw, taruh ke
        # discard pile (tetap ada di permainan, ikut terkocok ulang nanti)
        # dan ambil kartu berikutnya.
        safety = 0
        while self.top_card is not None and self.top_card.is_extreme() and safety < 500:
            self.deck.discard(self.top_card)
            self.top_card = self.deck.draw()
            safety += 1
        self.top_card_history = []
        self.current_color = random.choice(COLORS) if self.top_card.color == "Wild" else self.top_card.color
        self.current_player_index = 0
        self.direction = 1
        self.game_over = False
        self.winner_id = None
        self.finished_order = []
        self.started = True

        self.pending_uno_player_id = None
        self.pending_uno_deadlines = {}

        self.awaiting_wild_color = False
        self.wild_player_id = None
        self.awaiting_number_color = False
        self.number_color_player_id = None
        self.pending_number_value = None
        self.pending_number_color_options = []

        self.awaiting_steal_pick = False
        self.steal_player_id = None
        self.steal_target_id = None

        # FITUR BARU: id sesi game, naik tiap kali game baru benar2 dimulai
        # (start awal maupun rematch). Dipakai frontend buat tahu "ini game
        # session yang baru" - misalnya buat munculin popup penjelasan Extreme
        # mode sekali di awal tiap sesi, bukan tiap kali ada broadcast state.
        self.game_session_id += 1
        self.last_event = None

        self.message = f"Game dimulai. Giliran {self.current_player().name}."
        self.ensure_current_player_can_act()
        return True

    def rematch(self):
        return self.start_game(self.pack_count, self.cards_each, self.bot_count, self.extreme_mode, self.extreme_pack_count)

    def back_to_lobby(self):
        # FITUR BARU: kembali ke lobby tanpa langsung main lagi - supaya
        # pemain baru sempat gabung dulu sebelum leader mulai game berikutnya.
        # Roster lobby (lobby_players/lobby_order/leader) TIDAK direset,
        # cuma status "sedang main"-nya yang dimatikan.
        self.started = False
        self.game_over = False
        self.players = []
        self.winner_id = None
        self.finished_order = []
        self.top_card = None
        self.top_card_history = []
        self.awaiting_wild_color = False
        self.wild_player_id = None
        self.awaiting_number_color = False
        self.number_color_player_id = None
        self.pending_uno_player_id = None

        self.awaiting_steal_pick = False
        self.steal_player_id = None
        self.steal_target_id = None

        self.pending_uno_deadlines = {}
        self.message = "Kembali ke lobby."

    # ---------------- HELPERS ----------------

    def current_player(self):
        return self.players[self.current_player_index]

    def is_player_active(self, player):
        return player is not None and not player.finished and player.connected

    def active_players(self):
        return [p for p in self.players if self.is_player_active(p)]

    def active_player_count(self):
        return len(self.active_players())

    def get_player(self, player_id):
        for p in self.players:
            if p.player_id == player_id:
                return p
        return None

    def next_index(self, steps=1):
        return (self.current_player_index + self.direction * steps) % len(self.players)

    def advance_turn(self, steps=1):
        if self.game_over or not self.players:
            return
        if self.active_player_count() <= 1:
            self.finish_game_if_needed()
            return
        moved = 0
        safety = 0
        while moved < steps and safety < len(self.players) * 3:
            self.current_player_index = self.next_index(1)
            current = self.current_player()
            if self.is_player_active(current):
                moved += 1
            safety += 1
        self.message = f"Giliran {self.current_player().name}."
        self._tick_all_bombs()

    def finish_game_if_needed(self):
        active = self.active_players()
        if len(active) <= 1:
            if active:
                last_player = active[0]
                last_player.finished = True
                if last_player.player_id not in self.finished_order:
                    self.finished_order.append(last_player.player_id)
            self.game_over = True
            self.winner_id = self.finished_order[0] if self.finished_order else None
            self.message = "Game selesai."
            return True
        return False

    def has_playable_card(self, player):
        return any(c.can_play_on(self.top_card, self.current_color) for c in player.hand)

    def _has_usable_stash_item(self, player):
        if not player.stash:
            return False
        for item in player.stash:
            kind = item["kind"]
            if kind in ("Curi", "Bom Waktu"):
                if any(p.player_id != player.player_id and self.is_player_active(p) for p in self.players):
                    return True
            elif kind == "Recall":
                if any(
                    p.player_id != player.player_id and p.finished and not p.recall_used and (p.connected or p.is_bot)
                    for p in self.players
                ):
                    return True
            else:
                return True
        return False

    def has_any_action(self, player):
        return self.has_playable_card(player) or self._has_usable_stash_item(player)

    def ensure_current_player_can_act(self):
        """FITUR BARU: kalau giliran seorang pemain mulai dan dia benar-benar
        tidak punya kartu valid, dia otomatis dapat kartu baru - tidak perlu
        klik tombol DRAW. Kalau kartu barunya tetap tidak valid, giliran
        otomatis lanjut ke pemain berikutnya (juga tanpa perlu klik apapun)."""
        safety = 0
        limit = len(self.players) + 3
        while (
            not self.game_over
            and not self.awaiting_wild_color
            and not self.awaiting_number_color
            and not self.awaiting_steal_pick
            and self.players
            and safety < limit
        ):
            player = self.current_player()
            if not self.is_player_active(player):
                self.advance_turn(1)
                safety += 1
                continue
            if self.has_any_action(player):
                return
            drawn = player.draw_card(self.deck, 1)
            if drawn and self.has_any_action(player):
                self.message = f"{player.name} tidak punya kartu valid, otomatis dapat 1 kartu baru."
                return
            if drawn:
                self.message = f"{player.name} otomatis dapat kartu, masih tidak valid. Giliran lewat."
            self.advance_turn(1)
            safety += 1

    def get_random_uno_button_pos(self):
        return {"x": random.randint(5, 70), "y": random.randint(5, 70)}

    def start_uno_check_if_needed(self, player):
        if self.game_over or player is None or player.finished:
            return
        if len(player.hand) == 1:
            if getattr(player, "is_bot", False):
                player.uno_called = True
                self.pending_uno_deadlines.pop(player.player_id, None)
            else:
                player.uno_called = False
                self.pending_uno_deadlines[player.player_id] = now_ms() + self.uno_timeout_ms
        else:
            self.pending_uno_deadlines.pop(player.player_id, None)
            player.uno_called = False

    def call_clash(self, player_id):
        player = self.get_player(player_id)
        if player is None or player.finished:
            return False
        if len(player.hand) != 1:
            self.message = "CLASH tidak tersedia sekarang."
            return False
        # CATATAN: sengaja TIDAK mengecek `pending_uno_player_id == player_id`
        # di sini. pending_uno_player_id cuma 1 slot global buat penalti telat
        # (timer), tapi sekarang CLASH itu WAJIB (lihat play_cards) - kalau ada
        # 2 pemain nyangkut di 1 kartu bersamaan (misal gara-gara Swap Rotasi),
        # keduanya harus tetap bisa CLASH sendiri-sendiri, bukan cuma yang
        # kebetulan lagi pegang slot pending itu.
        player.uno_called = True
        if self.pending_uno_player_id == player_id:
            self.pending_uno_deadlines.pop(player_id, None)
        self.message = f"{player.name} klik CLASH!"
        return True

    def check_uno_timeout(self):
        if not self.pending_uno_deadlines or self.game_over:
            return False
        now = now_ms()
        expired = [pid for pid, dl in self.pending_uno_deadlines.items() if now >= dl]
        if not expired:
            return False
        changed = False
        for pid in expired:
            self.pending_uno_deadlines.pop(pid, None)
            player = self.get_player(pid)
            if player is None or player.finished:
                continue
            if len(player.hand) == 1 and not player.uno_called:
                player.draw_card(self.deck, 1)
                player.uno_called = False
                self.message = f"{player.name} kelewatan CLASH! Kena +1 kartu."
                changed = True
        return changed

    def check_steal_pick_timeout(self):
        """STABILITY FIX: kalau pemain yang sedang milih kartu curian tidak
        klik apa-apa (misal karena bug UI di client, atau dia AFK), server
        otomatis pilihkan kartu acak setelah timeout - supaya game TIDAK
        BISA macet selamanya menunggu 1 klik yang tidak pernah datang."""
        if not self.awaiting_steal_pick or self.game_over:
            return False
        if now_ms() < self.steal_pick_deadline_ms:
            return False
        player = self.get_player(self.steal_player_id)
        target = self.get_player(self.steal_target_id)
        if player is None or target is None or not target.hand:
            self.awaiting_steal_pick = False
            self.steal_player_id = None
            self.steal_target_id = None
            return False
        idx = random.randrange(len(target.hand))
        self._finish_steal(player, target, idx)
        return True
    
    def can_act(self, player_id):
        if self.game_over:
            self.message = "Game sudah selesai."
            return False
        player = self.get_player(player_id)
        if player is None:
            self.message = "Player tidak ditemukan."
            return False
        if player.finished:
            self.message = "Kamu sudah selesai main."
            return False
        if self.awaiting_wild_color:
            self.message = "Menunggu pemilihan warna Wild."
            return False
        if self.awaiting_number_color:
            self.message = "Menunggu pemilihan warna top card."
            return False
        if self.awaiting_steal_pick:
            self.message = "Menunggu kartu curian dipilih."
            return False
        if self.current_player().player_id != player_id:
            self.message = "Bukan giliranmu."
            return False
        return True

    def is_number_card(self, card):
        return card.value.isdigit()

    # ---------------- ACTIONS ----------------

    def play_cards(self, player_id, indices):
        """FITUR BARU: pemain memilih sendiri berapa banyak kartu kembar yang
        mau dimainkan sekaligus (dulu di versi desktop otomatis semuanya keluar)."""
        if not self.can_act(player_id):
            return False
        player = self.get_player(player_id)
        if player is None:
            return False

        # PENTING: pertahankan urutan seperti dikirim client (elemen pertama =
        # kartu yang PERTAMA diklik user, yang wajib valid dimainkan di atas
        # top_card). Kalau di-sort, kartu pertama bisa berubah jadi kartu
        # kembar lain yang belum tentu valid, dan validasi di bawah jadi salah.
        seen = set()
        ordered_indices = []
        for i in indices:
            if isinstance(i, int) and 0 <= i < len(player.hand) and i not in seen:
                seen.add(i)
                ordered_indices.append(i)
        indices = ordered_indices
        if not indices:
            self.message = "Tidak ada kartu dipilih."
            return False

        # FITUR BARU: pemain WAJIB pencet CLASH dulu sebelum boleh
        # mengeluarkan kartu terakhirnya (sisa 1 di tangan) - dulu bisa
        # langsung main tanpa CLASH selama belum kena timeout, sekarang
        # CLASH jadi syarat wajib, bukan cuma penalti telat.
        if len(player.hand) == 1 and not player.uno_called:
            self.message = "Tekan CLASH dulu sebelum mengeluarkan kartu terakhirmu!"
            return False

        primary_card = player.hand[indices[0]]

        if any(player.hand[i].value != primary_card.value for i in indices):
            self.message = "Kartu yang dipilih harus punya angka/nilai yang sama."
            return False

        if len(indices) > 1 and not self.is_number_card(primary_card):
            self.message = "Cuma kartu angka yang bisa dimainkan bareng-bareng."
            return False

        if not primary_card.can_play_on(self.top_card, self.current_color):
            self.message = "Kartu itu tidak valid untuk dimainkan."
            return False

        played_cards = [player.hand[i] for i in indices]
        for i in sorted(indices, reverse=True):
            player.hand.pop(i)

        distinct_colors = list(dict.fromkeys([c.color for c in played_cards if c.color in COLORS]))

        self._push_top_card_history()
        self.deck.discard(self.top_card)
        for card in played_cards[:-1]:
            self.deck.discard(card)
        last_card = played_cards[-1]

        # Kartu Wild (selalu 1 kartu saja, karena gate is_number_card di atas
        # cegah Wild ikut multi-select).
        if primary_card.color == "Wild":
            self.top_card = last_card
            self.awaiting_wild_color = True
            self.wild_player_id = player_id
            self.message = f"{player.name} main Wild. Pilih warna."
            if len(player.hand) == 0:
                self.finish_winner(player)
            return True

        # Beberapa kartu kembar warna beda -> pemain pilih top color-nya.
        if len(distinct_colors) > 1:
            self.top_card = last_card
            self.current_color = last_card.color
            self.awaiting_number_color = True
            self.number_color_player_id = player_id
            self.pending_number_value = primary_card.value
            self.pending_number_color_options = distinct_colors
            self.message = f"{player.name} main {len(played_cards)}x {primary_card.value}. Pilih warna top card."
            if len(player.hand) == 0:
                self.finish_winner(player)
            return True

        # 1 kartu saja, atau semua warna kembarnya sama -> tidak perlu nanya apa-apa.
        self.top_card = last_card
        self.current_color = last_card.color

        if len(player.hand) == 0:
            self.finish_winner(player)
            return True

        if len(played_cards) == 1:
            self.apply_card_effect(last_card, player)
        else:
            self.advance_turn(1)
            self.message = f"{player.name} main {len(played_cards)}x {primary_card.value}. Giliran {self.current_player().name}."

        self.start_uno_check_if_needed(player)
        self.ensure_current_player_can_act()
        return True

    def draw_card(self, player_id):
        """Draw manual/sukarela - tetap tersedia walau ada auto-draw, buat
        yang sengaja mau narik kartu baru meski masih punya kartu valid."""
        if not self.can_act(player_id):
            return False
        player = self.get_player(player_id)
        player.draw_card(self.deck, 1)
        player.uno_called = False
        if self.pending_uno_player_id == player.player_id:
            self.pending_uno_player_id = None
            self.pending_uno_deadline_ms = 0
        self.advance_turn(1)
        self.message = f"{player.name} narik kartu. Giliran {self.current_player().name}."
        self.ensure_current_player_can_act()
        return True

    # ---------------- MODE EXTREME: STASH ACTIONS ----------------
    # FITUR BARU: kartu spesial dipakai lewat aksi ini di giliran sendiri,
    # menggantikan aksi main-kartu/tarik-kartu biasa untuk giliran itu.

    def use_stash_item(self, player_id, stash_id, target_player_id=None):
        if not self.can_act(player_id):
            return False
        player = self.get_player(player_id)
        item = next((it for it in player.stash if it["id"] == stash_id), None)
        if item is None:
            self.message = "Kartu extreme tidak ditemukan di stash-mu."
            return False

        kind = item["kind"]

        needs_target = kind in ("Curi", "Bom Waktu", "Recall")
        target = self.get_player(target_player_id) if target_player_id is not None else None
        if needs_target:
            if kind == "Recall":
                if (
                    target is None
                    or target.player_id == player_id
                    or not target.finished
                    or target.recall_used
                    or (not target.connected and not target.is_bot)
                ):
                    self.message = "Pemain itu tidak bisa di-Recall lagi (sudah pernah dipakai / belum selesai)."
                    return False
            elif target is None or target.player_id == player_id or not self.is_player_active(target):
                self.message = "Pilih target lawan yang valid dulu."
                return False

        player.stash.remove(item)

        if kind == "Swap Rotasi":
            self._apply_swap_rotation()
            self.message = f"{player.name} pakai Swap Rotasi! Semua kartu tangan berputar."
            self._emit_event("Swap Rotasi", actor=player)
            self.advance_turn(1)
        elif kind == "+2 Skip":
            self._apply_plus2_skip(player)
        elif kind == "+2 Reverse":
            self._apply_plus2_reverse(player)
        elif kind == "Curi":
            if not target.hand:
                # Tidak ada kartu buat dicuri - batalkan, jangan hanguskan item.
                player.stash.append(item)
                self.message = f"{target.name} tidak punya kartu untuk dicuri."
                return False
            # FITUR BARU: bukan langsung dieksekusi - tunggu pemain pilih
            # sendiri 1 dari kartu tertutup milik target lewat pick_steal_card().
            self.awaiting_steal_pick = True
            self.steal_player_id = player_id
            self.steal_target_id = target.player_id
            self.steal_pick_deadline_ms = now_ms() + 8000
            self.message = f"{player.name} sedang memilih kartu curian dari {target.name}..."
            return True
        elif kind == "Bom Waktu":
            self._apply_bomb_pass(player, target, item.get("turns_left"))
            self._emit_event("Bom Waktu Pass", actor=player, target=target)
            self.advance_turn(1)
        elif kind == "Recall":
            recalled_name = target.name
            self._apply_recall(player, target)
            self._emit_event("Recall", actor=player, target=target)
            self.advance_turn(1)
            self.message = f"{player.name} pakai RECALL! {recalled_name} ikut main lagi dengan {RECALL_START_CARDS} kartu. Giliran {self.current_player().name}."
        else:
            # Harusnya tidak pernah kesampaian - kembalikan biar tidak hilang.
            player.stash.append(item)
            self.message = "Kartu extreme tidak dikenal."
            return False

        self.ensure_current_player_can_act()
        return True
    
    def pick_steal_card(self, player_id, card_index):
            """FITUR BARU: langkah ke-2 dari Curi. Dipanggil saat pemain klik
            salah satu kartu tertutup milik target."""
            if self.game_over:
                return False
            if not self.awaiting_steal_pick or self.steal_player_id != player_id:
                self.message = "Tidak ada proses Curi yang sedang berlangsung."
                return False
            target = self.get_player(self.steal_target_id)
            player = self.get_player(player_id)
            if target is None or player is None:
                self.awaiting_steal_pick = False
                self.steal_player_id = None
                self.steal_target_id = None
                return False
            if not isinstance(card_index, int) or not (0 <= card_index < len(target.hand)):
                self.message = "Pilihan kartu tidak valid."
                return False

            self._finish_steal(player, target, card_index)
            return True
    
    def _emit_event(self, kind, actor=None, target=None):
        """FITUR BARU: catat kejadian pemakaian kartu Extreme (atau ledakan
        Bom Waktu) supaya frontend semua pemain bisa nampilin animasi efek
        yang khas per jenis kartu. seq naik terus supaya klien gampang tahu
        ini kejadian baru atau bukan (bukan cuma broadcast state biasa)."""
        self.event_seq += 1
        self.last_event = {
            "seq": self.event_seq,
            "kind": kind,
            "actor_id": actor.player_id if actor else None,
            "actor_name": actor.name if actor else None,
            "target_id": target.player_id if target else None,
            "target_name": target.name if target else None,
        }

    def _apply_swap_rotation(self):
        active = [p for p in self.players if not p.finished]
        if len(active) < 2:
            return
        hands = [p.hand for p in active]
        if self.direction == 1:
            rotated = [hands[-1]] + hands[:-1]
        else:
            rotated = hands[1:] + [hands[0]]
        for p, new_hand in zip(active, rotated):
            p.hand = new_hand
        # Ukuran tangan tiap orang bisa berubah drastis gara-gara tukar-menukar
        # ini (bukan cuma naik/turun 1 seperti main/tarik kartu biasa) - cek
        # ulang status CLASH semua orang supaya yang tiba-tiba nyangkut di 1
        # kartu tetap kena wajib CLASH, dan yang tidak lagi di 1 kartu bebas.
        for p in active:
            self.start_uno_check_if_needed(p)

    def _next_active_target(self):
        """Cari pemain aktif berikutnya dari current_player_index, dipakai
        untuk efek +2 Skip / +2 Reverse (target = pemain berikutnya)."""
        idx = self.next_index(1)
        safety = 0
        while safety < len(self.players):
            target = self.players[idx]
            if not target.finished:
                return idx, target
            idx = (idx + self.direction) % len(self.players)
            safety += 1
        return None, None

    def _apply_plus2_skip(self, player):
        target_idx, target = self._next_active_target()
        if target is None:
            self.advance_turn(1)
            return
        target.draw_card(self.deck, 2)
        self.current_player_index = target_idx
        self._emit_event("+2 Skip", actor=player, target=target)
        # Skip target (yang kena +2) DAN 1 pemain setelahnya - beda dari
        # Draw Two biasa yang cuma skip 1 orang.
        self.advance_turn(2)
        self.message = f"{player.name} pakai +2 Skip! {target.name} kena +2 & 2 pemain dilewati. Giliran {self.current_player().name}."

    def _apply_plus2_reverse(self, player):
        if self.active_player_count() == 2:
            # Cuma 2 pemain aktif - balik arah tidak berpengaruh, jadi
            # perlakukan sebagai "lawan kena +2 tapi tetap dapat giliran".
            target_idx, target = self._next_active_target()
            if target is None:
                self.advance_turn(1)
                return
            target.draw_card(self.deck, 2)
            self.current_player_index = target_idx
            self.message = f"{player.name} pakai +2 Reverse! {target.name} kena +2. Giliran {target.name}."
            self._emit_event("+2 Reverse", actor=player, target=target)
            self._tick_all_bombs()
            return

        self.direction *= -1
        target_idx, target = self._next_active_target()
        if target is None:
            self.advance_turn(1)
            return
        target.draw_card(self.deck, 2)
        # Target TIDAK di-skip (beda dari +2 Skip) - dia kena +2 tapi tetap
        # kebagian giliran sesudah ini, cuma arahnya sudah kebalik.
        self.current_player_index = target_idx
        self.message = f"{player.name} pakai +2 Reverse! Arah berbalik, {target.name} kena +2. Giliran {target.name}."
        self._emit_event("+2 Reverse", actor=player, target=target)
        self._tick_all_bombs()

    def _finish_steal(self, player, target, card_index):
        """Eksekusi Curi setelah pemain benar2 pilih posisi kartu (buta -
        dia cuma lihat sisi belakang) dari tangan target."""
        stolen = target.hand.pop(card_index)
        player.hand.append(stolen)
        self.message = f"{player.name} mencuri 1 kartu dari {target.name}!"
        self._emit_event("Curi", actor=player, target=target)

        self.awaiting_steal_pick = False
        self.steal_player_id = None
        self.steal_target_id = None

        if len(target.hand) == 0:
            self.finish_winner(target)
        else:
            self.start_uno_check_if_needed(target)

        self.advance_turn(1)
        self.ensure_current_player_can_act()

    def _apply_bomb_pass(self, player, target, turns_left=None):
        if turns_left is None:
            turns_left = BOM_WAKTU_TURNS
        target.stash.append({
            "id": uuid.uuid4().hex[:8],
            "kind": "Bom Waktu",
            "turns_left": turns_left,
        })
        self.message = f"{player.name} mengoper Bom Waktu ke {target.name}! Sisa {turns_left} giliran sebelum meledak."

    def _apply_recall(self, player, target):
        if target.player_id in self.finished_order:
            self.finished_order.remove(target.player_id)
        target.finished = False
        target.uno_called = False
        target.hand = []
        target.recall_used = True  
        target.draw_card(self.deck, RECALL_START_CARDS)
        self.message = f"{player.name} pakai RECALL! {target.name} ikut main lagi dengan {RECALL_START_CARDS} kartu."
    
    def choose_number_color(self, player_id, color):
        if self.game_over:
            return False
        if not self.awaiting_number_color or self.number_color_player_id != player_id:
            self.message = "Tidak boleh pilih warna sekarang."
            return False
        if color not in self.pending_number_color_options:
            self.message = "Hanya boleh pilih warna dari kartu yang dimainkan."
            return False
        player = self.get_player(player_id)
        self.top_card = Card(color, self.pending_number_value)
        self.current_color = color
        self.awaiting_number_color = False
        self.number_color_player_id = None
        self.pending_number_value = None
        self.pending_number_color_options = []
        self.advance_turn(1)
        self.message = f"{player.name} pilih {color}. Giliran {self.current_player().name}."
        self.start_uno_check_if_needed(player)
        self.ensure_current_player_can_act()
        return True

    def choose_wild_color(self, player_id, color):
        if self.game_over:
            return False
        if not self.awaiting_wild_color or self.wild_player_id != player_id:
            self.message = "Tidak boleh pilih warna Wild sekarang."
            return False
        if color not in COLORS:
            self.message = "Warna tidak valid."
            return False
        player = self.get_player(player_id)
        self.current_color = color
        self.awaiting_wild_color = False
        self.wild_player_id = None

        if self.top_card is not None and self.top_card.value == "Wild Draw Four":
            # FITUR BARU: efek +4 - cari target aktif berikutnya, dia kena 4
            # kartu sekaligus gilirannya dilewati (persis pola Draw Two, cuma
            # jumlah kartunya 4 dan baru dieksekusi setelah warna dipilih).
            target_idx = self.next_index(1)
            safety = 0
            while safety < len(self.players):
                target = self.players[target_idx]
                if not target.finished:
                    break
                target_idx = (target_idx + self.direction) % len(self.players)
                safety += 1
            target = self.players[target_idx]
            target.draw_card(self.deck, 4)
            self.current_player_index = target_idx
            self.advance_turn(1)
            self.message = f"{player.name} main +4 warna {color}. {target.name} kena +4. Giliran {self.current_player().name}."
        else:
            self.advance_turn(1)
            self.message = f"{player.name} pilih {color}. Giliran {self.current_player().name}."

        self.start_uno_check_if_needed(player)
        self.ensure_current_player_can_act()
        return True

    def apply_card_effect(self, card, player):
        if card.value == "Skip":
            self.advance_turn(2)
            self.message = f"{player.name} main Skip. Giliran {self.current_player().name}."
        elif card.value == "Reverse":
            if self.active_player_count() == 2:
                self.advance_turn(2)
                self.message = f"{player.name} main Reverse. Giliran {self.current_player().name}."
            else:
                self.direction *= -1
                self.advance_turn(1)
                self.message = f"{player.name} main Reverse. Arah berubah. Giliran {self.current_player().name}."
        elif card.value == "Draw Two":
            target_idx = self.next_index(1)
            safety = 0
            while safety < len(self.players):
                target = self.players[target_idx]
                if not target.finished:
                    break
                target_idx = (target_idx + self.direction) % len(self.players)
                safety += 1
            target = self.players[target_idx]
            target.draw_card(self.deck, 2)
            self.current_player_index = target_idx
            self.advance_turn(1)
            self.message = f"{player.name} main Draw Two. {target.name} kena +2. Giliran {self.current_player().name}."
        else:
            self.advance_turn(1)

    def finish_winner(self, player):
        player.finished = True
        if player.player_id not in self.finished_order:
            self.finished_order.append(player.player_id)

        self.awaiting_wild_color = False
        self.wild_player_id = None
        self.awaiting_number_color = False
        self.number_color_player_id = None
        self.pending_number_value = None
        self.pending_number_color_options = []

        if self.pending_uno_player_id == player.player_id:
            self.pending_uno_deadlines.pop(player.player_id, None)
        player.uno_called = False

        if self.finish_game_if_needed():
            return

        self.game_over = False
        self.winner_id = self.finished_order[0]

        if self.current_player().finished:
            self.advance_turn(1)

        self.message = f"{player.name} selesai! Giliran {self.current_player().name}."
        self.ensure_current_player_can_act()

    def mark_player_disconnected(self, player_id):
        player = self.get_player(player_id)
        if player is None or self.game_over or player.finished:
            return
        player.connected = False
        self.message = f"{player.name} terputus koneksi."

        if self.pending_uno_player_id == player_id:
            self.pending_uno_deadlines.pop(player_id, None)

        if self.awaiting_wild_color and self.wild_player_id == player_id:
            self.awaiting_wild_color = False
            self.wild_player_id = None
            self.current_color = random.choice(COLORS)

        if self.awaiting_number_color and self.number_color_player_id == player_id:
            self.awaiting_number_color = False
            self.number_color_player_id = None
            self.pending_number_value = None
            self.pending_number_color_options = []

        if self.awaiting_steal_pick and self.steal_player_id == player_id:
            self.awaiting_steal_pick = False
            self.steal_player_id = None
            self.steal_target_id = None
            self.message = "Proses Curi dibatalkan (pemain terputus)."

        if self.awaiting_steal_pick and self.steal_target_id == player_id:
            self.awaiting_steal_pick = False
            self.steal_player_id = None
            self.steal_target_id = None
            self.message = "Curi dibatalkan, target terputus koneksi."

        if self.finish_game_if_needed():
            return

        if self.current_player().player_id == player_id or not self.is_player_active(self.current_player()):
            self.advance_turn(1)
            self.ensure_current_player_can_act()

    def _push_top_card_history(self):
        if self.top_card is not None:
            self.top_card_history.append(self.top_card)
            if len(self.top_card_history) > 6:
                self.top_card_history.pop(0)

    def _tick_all_bombs(self):
        for player in self.players:
            if not player.stash:
                continue
            if player.finished:
                continue
            remaining = []
            exploded = False
            for item in player.stash:
                if item["kind"] == "Bom Waktu" and item.get("turns_left") is not None:
                    item["turns_left"] -= 1
                    if item["turns_left"] <= 0:
                        player.draw_card(self.deck, 5)
                        exploded = True
                        continue
                remaining.append(item)
            player.stash = remaining
            if exploded:
                self.message = f"\U0001F4A3 Bom Waktu milik {player.name} meledak! Kena +5 kartu."
                self._emit_event("Bom Waktu Explode", actor=player)

    # ---------------- BOT AI ----------------
    # FITUR BARU: main vs bot. Bot dipanggil dari server secara berkala
    # (lihat bot_watcher di server.py) - method ini return True kalau bot
    # betulan melakukan sesuatu (supaya server tahu harus broadcast state).

    def _bot_pick_color(self, player):
        # Heuristik simpel: pilih warna yang paling banyak dipegang bot di
        # tangannya sendiri (biar kartu berikutnya lebih gampang match).
        counts = {c: 0 for c in COLORS}
        for card in player.hand:
            if card.color in counts:
                counts[card.color] += 1
        best = max(counts, key=lambda c: counts[c])
        if counts[best] == 0:
            return random.choice(COLORS)
        return best

    def maybe_run_bot_turn(self):
        if self.game_over or not self.players:
            return False
        
        if self.awaiting_steal_pick:
            player = self.get_player(self.steal_player_id)
            if player is not None and player.is_bot:
                target = self.get_player(self.steal_target_id)
                if target is not None and target.hand:
                    idx = random.randrange(len(target.hand))
                    return self.pick_steal_card(player.player_id, idx)
            return False
        
        if self.awaiting_wild_color:
            player = self.get_player(self.wild_player_id)
            if player is not None and player.is_bot:
                return self.choose_wild_color(player.player_id, self._bot_pick_color(player))
            return False

        if self.awaiting_number_color:
            player = self.get_player(self.number_color_player_id)
            if player is not None and player.is_bot:
                options = self.pending_number_color_options
                color = options[0] if options else random.choice(COLORS)
                return self.choose_number_color(player.player_id, color)
            return False

        current = self.current_player()
        if not current.is_bot or not self.is_player_active(current):
            return False

        # ensure_current_player_can_act() sudah dipanggil di setiap titik
        # giliran berpindah, jadi kalau giliran benar-benar sampai ke bot,
        # bot dijamin punya minimal 1 kartu valid untuk dimainkan.
        for i, card in enumerate(current.hand):
            if card.can_play_on(self.top_card, self.current_color):
                return self.play_cards(current.player_id, [i])

        # FITUR BARU: MODE EXTREME. Kalau bot kebetulan tidak (atau memilih
        # tidak) main kartu normal tapi punya kartu di stash, pakai salah
        # satu daripada cuma narik kartu terus.
        if current.stash:
            opponents_active = [
                p for p in self.players
                if p.player_id != current.player_id and self.is_player_active(p)
            ]
            finished_recallable = [
                p for p in self.players
                if p.player_id != current.player_id and p.finished and not p.recall_used and (p.connected or p.is_bot)
            ]

            usable_items = []
            for it in current.stash:
                if it["kind"] in ("Curi", "Bom Waktu"):
                    if opponents_active:
                        usable_items.append(it)
                elif it["kind"] == "Recall":
                    if finished_recallable:
                        usable_items.append(it)
                else:
                    usable_items.append(it)  # Swap Rotasi - tidak butuh target

            if usable_items:
                item = random.choice(usable_items)
                if item["kind"] in ("Curi", "Bom Waktu"):
                    target = random.choice(opponents_active)
                    return self.use_stash_item(current.player_id, item["id"], target.player_id)
                elif item["kind"] == "Recall":
                    target = random.choice(finished_recallable)
                    return self.use_stash_item(current.player_id, item["id"], target.player_id)
                else:
                    return self.use_stash_item(current.player_id, item["id"])

        # Jaga-jaga (harusnya nyaris tidak pernah kesampaian):
        return self.draw_card(current.player_id)

    # ---------------- STATE ----------------

    def build_state_for(self, viewer_player_id):
        viewer = self.get_player(viewer_player_id)
        if viewer is None or not self.players:
            return None

        # Sekelompok kartu kembar di tangan (angka sama) - dikirim ke frontend
        # supaya UI bisa menyarankan grup mana yang bisa dipilih sekaligus.
        hand_dicts = [c.to_dict() for c in viewer.hand]

        return {
            "type": "mp_game_state",
            "viewer_player_id": viewer_player_id,
            "your_hand": hand_dicts,
            "your_stash": list(viewer.stash),
            "extreme_mode": self.extreme_mode,
            "extreme_pack_count": self.extreme_pack_count,
            "game_session_id": self.game_session_id,
            "last_event": self.last_event,
            "players": [
                {
                    "id": p.player_id,
                    "name": p.name,
                    "card_count": len(p.hand),
                    "connected": p.connected,
                    "is_you": p.player_id == viewer_player_id,
                    "is_current_turn": (
                        not self.game_over
                        and p.player_id == self.current_player().player_id
                        and not p.finished
                    ),
                    "is_finished": p.finished,
                    "rank": (self.finished_order.index(p.player_id) + 1) if p.player_id in self.finished_order else None,
                    "uno_called": p.uno_called,
                    "is_bot": p.is_bot,
                    "bomb_turns_left": next(
                        (it["turns_left"] for it in p.stash if it["kind"] == "Bom Waktu"),
                        None,
                    ),
                    "recall_used": p.recall_used,
                }
                for p in self.players
            ],
            "top_card": self.top_card.to_dict() if self.top_card else None,
            "top_card_history": [c.to_dict() for c in reversed(self.top_card_history[-4:])],
            "current_color": self.current_color,
            "current_player_id": self.current_player().player_id,
            "current_player_name": self.current_player().name,
            "direction": self.direction,
            "game_over": self.game_over,
            "winner_id": self.winner_id,
            "message": self.message,
            "awaiting_wild_color": self.awaiting_wild_color,
            "wild_player_id": self.wild_player_id,
            "awaiting_number_color": self.awaiting_number_color,
            "number_color_player_id": self.number_color_player_id,
            "number_color_options": self.pending_number_color_options,
            "pending_number_value": self.pending_number_value,
            "deck_count": len(self.deck.cards),
            "pack_count": self.pack_count,
            "cards_each": self.cards_each,
            # "pending_uno_player_id": self.pending_uno_player_id,
            # "uno_timeout_remaining": max(0, self.pending_uno_deadline_ms - now_ms()) if self.pending_uno_player_id is not None else 0,
            "awaiting_steal_pick": self.awaiting_steal_pick,
            "steal_pick_remaining_ms": max(0, self.steal_pick_deadline_ms - now_ms()) if self.awaiting_steal_pick else 0,
            "steal_player_id": self.steal_player_id,
            "steal_target_id": self.steal_target_id,
        }

    def build_all_states(self):
        """STABILITY FIX: snapshot state untuk SEMUA pemain sekaligus, dalam
        satu pemanggilan. Method ini WAJIB dipanggil dari dalam `room_lock`
        (lihat broadcast_game() di server.py) - tujuannya supaya seluruh
        pembacaan self.players/self.top_card/dkk untuk semua viewer terjadi
        atomically terhadap 1 versi state yang sama, sebelum lock dilepas
        dan hasilnya baru dikirim ke socket (I/O) di luar lock. Tanpa ini,
        broadcast_game() sebelumnya membaca room langsung tanpa lock di
        tengah kemungkinan mutasi dari thread lain (bot_watcher,
        uno_timeout_watcher, koneksi client lain) sehingga snapshot yang
        dikirim ke client bisa "robek"/tidak konsisten."""
        return {p.player_id: self.build_state_for(p.player_id) for p in self.players}
