(() => {
  "use strict";

  // ---------------- STATE ----------------
  let ws = null;
  let myPlayerId = null;
  let myName = "";
  let pendingJoinName = "";
  let latestLobbyState = null;
  let latestGameState = null;
  let prevTopCardKey = null;
  let prevCurrentPlayerId = null;
  let prevHandSignature = "";
  let prevFinishedIds = new Set();
  // ANIMASI KARTU DILETAKKAN: dipakai untuk membedakan kartu top yang baru
  // berubah karena KITA sendiri yang main (sudah dianimasikan langsung dari
  // klik) vs karena pemain lain/bot yang main (dianimasikan terbang dari
  // posisi chip mereka).
  let lastLocalPlayAt = 0;

  // Multi-select kartu kembar (fitur baru: pemain pilih sendiri berapa
  // banyak kartu kembar yang mau dimainkan sekaligus).
  let selection = { value: null, indices: [] };

  // FITUR BARU: MODE EXTREME. stash_id yang sedang menunggu pemilihan target
  // (dipakai buat Curi & Bom Waktu, yang butuh pilih lawan dulu).
  let pendingStashId = null;

  // ANIMASI KARTU DILETAKKAN: menyimpan kartu Wild / grup kembar beda-warna
  // yang masih menunggu pemain memilih warna - animasi terbangnya ditunda
  // sampai warna benar-benar dipilih (lihat renderColorPicker).
  let pendingColorFly = null;

  // FITUR BARU: popup penjelasan Extreme mode di awal game + animasi efek
  // kartu spesial. lastSeenGameSessionId dipakai buat tahu "ini game session
  // baru" (munculin popup sekali), lastSeenEventSeq buat tahu "ada kejadian
  // kartu extreme baru yang belum dianimasikan" (bukan cuma broadcast biasa).
  let lastSeenGameSessionId = null;
  let lastSeenEventSeq = null;
  let extremeIntroTimer = null;
  let extremeIntroInterval = null;
  let deckExtremeParticlesBuilt = false;

  let firstTimeHintShown = false;

  let pingIntervalId = null;
  let lastLatencyMs = null;
  
  function startPingLoop() {
    clearInterval(pingIntervalId);
    pingIntervalId = setInterval(() => {
      send({ type: "ping_check", t: Date.now() });
    }, 4000);
  }

  const EXTREME_CARD_INFO = [
    {
      kind: "Swap Rotasi", slug: "swaprotasi", icon: "🔀",
      desc: "Semua kartu tangan (termasuk punyamu) geser 1 arah rotasi giliran.",
    },
    {
      kind: "+2 Skip", slug: "2skip", icon: "⛔",
      desc: "Target kena +2 kartu, dia dan 1 pemain setelahnya dilewati.",
    },
    {
      kind: "+2 Reverse", slug: "2reverse", icon: "🔁",
      desc: "Arah berbalik, pemain berikutnya di arah baru kena +2 (tapi tetap dapat giliran).",
    },
    {
      kind: "Curi", slug: "curi", icon: "🤏",
      desc: "Ambil 1 kartu random dari tangan lawan pilihanmu.",
    },
    {
      kind: "Bom Waktu", slug: "bomwaktu", icon: "💣",
      desc: "Hitung mundur 3 giliranmu - oper ke lawan sebelum meledak (+5 kartu kalau telat).",
    },
    {
      kind: "Recall", slug: "recall", icon: "↩️",
      desc: "Ajak pemain yang sudah selesai untuk ikut main lagi, mulai dengan 4 kartu.",
    },
  ];

  const $ = (id) => document.getElementById(id);

  function showDisconnectBanner() {
    let banner = $("disconnectBanner");
    if (!banner) return;
    banner.style.display = "flex";
  }

  document.addEventListener("DOMContentLoaded", () => {
    const retryBtn = $("reconnectBtn");
    if (retryBtn) retryBtn.addEventListener("click", () => location.reload());

    const hintBtn = $("hintBtn");
    const hintOverlay = $("hintOverlay");
    const closeHintBtn = $("closeHintBtn");
    if (hintBtn && hintOverlay && closeHintBtn) {
      hintBtn.addEventListener("click", () => { hintOverlay.style.display = "flex"; });
      closeHintBtn.addEventListener("click", () => { hintOverlay.style.display = "none"; });
      hintOverlay.addEventListener("click", (e) => {
        if (e.target === hintOverlay) hintOverlay.style.display = "none";
      });
    }

    renderExtremeCardGrid($("hintExtremeCards"));
    renderExtremeCardGrid($("extremeIntroCards"));

    const introCloseBtn = $("extremeIntroCloseBtn");
    if (introCloseBtn) introCloseBtn.addEventListener("click", closeExtremeIntro);

    const shareRow = $("shareIpRow");
    const shareText = $("shareIpText");
    const copyBtn = $("copyIpBtn");
    if (shareRow && shareText && copyBtn) {
      shareText.textContent = location.origin;
      shareRow.style.display = "flex";
      copyBtn.addEventListener("click", async () => {
        try {
          await navigator.clipboard.writeText(location.origin);
          copyBtn.textContent = "TERSALIN!";
        } catch (e) {
          // Fallback kalau clipboard API diblokir (mis. bukan HTTPS)
          shareText.style.userSelect = "text";
          copyBtn.textContent = "SELECT MANUAL";
        }
        setTimeout(() => { copyBtn.textContent = "SALIN"; }, 1600);
      });
    }
  });

  // ---------------- MODE EXTREME: KARTU, POPUP INTRO, ANIMASI EFEK ----------------

  function renderExtremeCardGrid(container) {
    if (!container) return;
    container.innerHTML = "";
    EXTREME_CARD_INFO.forEach((info) => {
      const el = document.createElement("div");
      el.className = "ext-card ext-" + info.slug;
      el.innerHTML = `
        <div class="ext-card-icon">${info.icon}</div>
        <div class="ext-card-body">
          <div class="ext-card-title">${escapeHtml(info.kind)}</div>
          <div class="ext-card-desc">${escapeHtml(info.desc)}</div>
        </div>`;
      container.appendChild(el);
    });
  }

  function maybeShowExtremeIntro(state) {
    // Munculin popup penjelasan Extreme mode SEKALI setiap sesi game baru
    // (dideteksi lewat game_session_id yang naik tiap start_game/rematch) -
    // baik untuk pemain yang baru pertama kali lihat state (join di tengah
    // game) maupun yang sudah ada sejak lobby.
    const isFirstStateEver = lastSeenGameSessionId === null;
    const isNewSession = !isFirstStateEver && state.game_session_id !== lastSeenGameSessionId;
    lastSeenGameSessionId = state.game_session_id;

    if (!state.extreme_mode) return;
    if (!isFirstStateEver && !isNewSession) return;

    showExtremeIntro();
  }

  function showExtremeIntro() {
    const overlay = $("extremeIntroOverlay");
    const countdownEl = $("extremeIntroCountdown");
    if (!overlay || !countdownEl) return;

    clearTimeout(extremeIntroTimer);
    clearInterval(extremeIntroInterval);

    let secondsLeft = 5;
    countdownEl.textContent = secondsLeft;
    overlay.style.display = "flex";

    extremeIntroInterval = setInterval(() => {
      secondsLeft -= 1;
      countdownEl.textContent = Math.max(0, secondsLeft);
      if (secondsLeft <= 0) clearInterval(extremeIntroInterval);
    }, 1000);

    extremeIntroTimer = setTimeout(closeExtremeIntro, 5000);
  }

  function closeExtremeIntro() {
    const overlay = $("extremeIntroOverlay");
    if (overlay) overlay.style.display = "none";
    clearTimeout(extremeIntroTimer);
    clearInterval(extremeIntroInterval);
  }

  const EXTREME_EFFECT_LABEL = {
    "Swap Rotasi": (ev) => `${ev.actor_name} pakai Swap Rotasi!`,
    "+2 Skip": (ev) => `${ev.actor_name} pakai +2 Skip ke ${ev.target_name}!`,
    "+2 Reverse": (ev) => `${ev.actor_name} pakai +2 Reverse ke ${ev.target_name}!`,
    "Curi": (ev) => `${ev.actor_name} mencuri kartu dari ${ev.target_name}!`,
    "Bom Waktu Pass": (ev) => `${ev.actor_name} mengoper Bom Waktu ke ${ev.target_name}!`,
    "Bom Waktu Explode": (ev) => `💥 Bom Waktu milik ${ev.actor_name} meledak!`,
    "Recall": (ev) => `${ev.actor_name} pakai RECALL ke ${ev.target_name}!`,
  };
  const EXTREME_EFFECT_ICON = {
    "Swap Rotasi": "🔀", "+2 Skip": "⛔", "+2 Reverse": "🔁", "Curi": "🤏",
    "Bom Waktu Pass": "💣", "Bom Waktu Explode": "💥", "Recall": "↩️",
  };
  const EXTREME_EFFECT_SLUG = {
    "Swap Rotasi": "swaprotasi", "+2 Skip": "2skip", "+2 Reverse": "2reverse",
    "Curi": "curi", "Bom Waktu Pass": "bombpass", "Bom Waktu Explode": "bombexplode",
    "Recall": "recall",
  };

  function checkExtremeEffect(state) {
    const ev = state.last_event;
    if (!ev) return;
    if (lastSeenEventSeq === null) {
      // Sinkronisasi pertama kali (baru join / baru buka halaman) - jangan
      // animasikan kejadian lama yang sudah kejadian sebelum kita nyambung.
      lastSeenEventSeq = ev.seq;
      return;
    }
    if (ev.seq <= lastSeenEventSeq) return;
    lastSeenEventSeq = ev.seq;
    triggerExtremeEffect(ev);
  }

  const EXTREME_EFFECT_BIGTEXT = {
    "Swap Rotasi": "SWAP!",
    "+2 Skip": "+2 SKIP!",
    "+2 Reverse": "+2 REVERSE!",
    "Curi": "KARTU DICURI!",
    "Bom Waktu Pass": "BOM DIOPER!",
    "Bom Waktu Explode": "BOOM!",
    "Recall": "RECALL!",
  };

  // FITUR BARU: ANIMASI DRAMATIS. Partikel kecil yang nyebar dari titik
  // tengah badge efek, ngasih kesan "ledakan visual" - dipakai ulang di
  // semua jenis efek Extreme biar konsisten tapi tetap terasa beda-beda
  // (lewat parameter emoji/warna/jarak sebar yang beda per kind).
  function spawnParticles(opts) {
    const layer = $("extremeEffectLayer");
    if (!layer) return;
    const {
      count = 14,
      emoji = "\u2728",
      colors = null,
      spread = 220,
      duration = 1.1,
      originX = "50%",
      originY = "40%",
    } = opts;

    for (let i = 0; i < count; i++) {
      const p = document.createElement("div");
      p.className = "ext-fx-particle";
      const angle = (Math.PI * 2 * i) / count + (Math.random() * 0.6 - 0.3);
      const dist = spread * (0.5 + Math.random() * 0.6);
      const dx = Math.cos(angle) * dist;
      const dy = Math.sin(angle) * dist;
      const rot = (Math.random() * 720 - 360).toFixed(0);
      const dur = duration * (0.75 + Math.random() * 0.5);

      p.style.left = originX;
      p.style.top = originY;
      p.style.setProperty("--dx", dx.toFixed(0) + "px");
      p.style.setProperty("--dy", dy.toFixed(0) + "px");
      p.style.setProperty("--rot", rot + "deg");
      p.style.animationDuration = dur + "s";

      if (colors) {
        p.style.background = colors[Math.floor(Math.random() * colors.length)];
        p.classList.add("ext-fx-particle-dot");
      } else {
        p.textContent = emoji;
        p.classList.add("ext-fx-particle-emoji");
      }

      layer.appendChild(p);
      setTimeout(() => p.remove(), dur * 1000 + 100);
    }
  }

  function flashScreen(colorClass, duration = 350) {
    const layer = $("extremeEffectLayer");
    if (!layer) return;
    const flash = document.createElement("div");
    flash.className = "ext-fx-flash " + colorClass;
    layer.appendChild(flash);
    setTimeout(() => flash.remove(), duration);
  }

  function triggerExtremeEffect(ev) {
    const layer = $("extremeEffectLayer");
    if (!layer) return;
    const slug = EXTREME_EFFECT_SLUG[ev.kind] || "generic";
    const icon = EXTREME_EFFECT_ICON[ev.kind] || "\u2728";
    const labelFn = EXTREME_EFFECT_LABEL[ev.kind];
    const label = labelFn ? labelFn(ev) : ev.kind;

    const badge = document.createElement("div");
    badge.className = "ext-fx-badge ext-fx-" + slug;
    badge.innerHTML = `<span class="ext-fx-icon">${icon}</span>`;
    layer.appendChild(badge);
    setTimeout(() => badge.remove(), 1900);

    const toast = document.createElement("div");
    toast.className = "ext-fx-toast ext-fx-toast-" + slug;
    toast.textContent = label;
    layer.appendChild(toast);
    setTimeout(() => toast.remove(), 2400);

    // FITUR BARU: ANIMASI DRAMATIS - kepribadian visual beda per kartu.
    switch (ev.kind) {
      case "Swap Rotasi":
        spawnParticles({ count: 10, emoji: "\uD83C\uDCCF", spread: 180, duration: 1.3 });
        break;
      case "+2 Skip":
        spawnParticles({ count: 8, colors: ["#f08a8a", "#a02b2b"], spread: 160, duration: 0.9 });
        flashScreen("ext-fx-flash-red", 250);
        break;
      case "+2 Reverse":
        spawnParticles({ count: 12, emoji: "\u27A1\uFE0F", spread: 200, duration: 1.2 });
        break;
      case "Curi":
        spawnParticles({ count: 16, colors: ["#f4de6b", "#ffd98a"], spread: 240, duration: 1.0, originX: "50%", originY: "40%" });
        break;
      case "Bom Waktu Pass":
        spawnParticles({ count: 10, emoji: "\uD83D\uDD25", spread: 260, duration: 1.1 });
        break;
      case "Bom Waktu Explode":
        spawnParticles({ count: 26, colors: ["#ffdc5a", "#ff7a3a", "#ff3b3b", "#4a0000"], spread: 340, duration: 1.0 });
        flashScreen("ext-fx-flash-explode", 400);
        break;
      case "Recall":
        spawnParticles({ count: 14, emoji: "\u2726", spread: 200, duration: 1.5, originY: "60%" });
        break;
      default:
        spawnParticles({ count: 10, spread: 180 });
    }

    if (ev.kind === "Bom Waktu Explode") {
      const stage = $("screen-game");
      if (stage) {
        stage.classList.add("screen-shake");
        setTimeout(() => stage.classList.remove("screen-shake"), 450);
      }
    }
  }

  function triggerSwapBurst(layer) {
    const centerX = window.innerWidth / 2;
    const centerY = window.innerHeight * 0.42;
    const count = 14;
    for (let i = 0; i < count; i++) {
      const angle = (Math.PI * 2 * i) / count + Math.random() * 0.3;
      const dist = 140 + Math.random() * 90;
      const dx = Math.cos(angle) * dist;
      const dy = Math.sin(angle) * dist;
      const particle = document.createElement("div");
      particle.className = "ext-fx-swap-particle";
      particle.textContent = "🔀";
      particle.style.left = centerX + "px";
      particle.style.top = centerY + "px";
      layer.appendChild(particle);
      particle.animate(
        [
          { transform: "translate(-50%, -50%) scale(0.3) rotate(0deg)", opacity: 0 },
          { transform: `translate(calc(-50% + ${dx * 0.4}px), calc(-50% + ${dy * 0.4}px)) scale(1.1) rotate(180deg)`, opacity: 1, offset: 0.35 },
          { transform: `translate(calc(-50% + ${dx}px), calc(-50% + ${dy}px)) scale(0.6) rotate(360deg)`, opacity: 0 },
        ],
        { duration: 900 + Math.random() * 300, easing: "cubic-bezier(.2,.8,.3,1)" }
      );
      setTimeout(() => particle.remove(), 1300);
    }
  }

  function showScreen(name) {
    document.querySelectorAll(".screen").forEach((el) => el.classList.remove("active"));
    $("screen-" + name).classList.add("active");
  }

  // ---------------- WEBSOCKET ----------------

  function connect() {
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    // Otomatis connect ke host yang sama dengan yang dipakai untuk buka
    // halaman ini - TIDAK perlu input IP manual sama sekali.
    const url = `${proto}//${location.host}/ws`;
    ws = new WebSocket(url);

    ws.onopen = () => {
      const savedId = localStorage.getItem("clash_player_id");
      const savedSession = localStorage.getItem("clash_session_id");
      const savedName = localStorage.getItem("clash_name");
      const effectiveName = myName || savedName || "Player";
      pendingJoinName = effectiveName;
      // STABILITY FIX: selalu kirim "name" walau ini percobaan reconnect -
      // supaya kalau reconnect ternyata tidak valid lagi (misal server sempat
      // direstart), server tetap bisa daftarkan sebagai pemain baru dengan
      // nama yang benar, bukan fallback ke nama generik "Player".
      // session_id dikirim juga supaya id lama TIDAK PERNAH dianggap valid
      // kalau ternyata itu peninggalan sesi server yang berbeda/sudah restart.
      ws.send(JSON.stringify({
        type: "join",
        name: effectiveName,
        player_id: savedId ? parseInt(savedId, 10) : null,
        session_id: savedSession || null
      },
      
      startPingLoop()
    ));
    };

    ws.onmessage = (evt) => {
      let data;
      try { data = JSON.parse(evt.data); } catch (e) { return; }
      handleServerMessage(data);
    };

    ws.onclose = () => {
      const hadJoined = myPlayerId !== null;
      ws = null;
      if (!hadJoined) {
        const joinBtn = $("joinBtn");
        const joinError = $("joinError");
        if (joinBtn) { joinBtn.disabled = false; joinBtn.classList.remove("btn-loading"); }
        if (joinError) joinError.textContent = "Gagal terhubung ke host. Pastikan server masih berjalan.";
        return;
      }
      showDisconnectBanner();
      clearInterval(pingIntervalId);
    };

    ws.onerror = () => {};
  }

  function send(payload) {
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify(payload));
    }
  }


  function showGlobalError(message) {
    const msg = message || "Terjadi kesalahan.";
    const activeGame = $("screen-game") && $("screen-game").classList.contains("active");
    const activeLobby = $("screen-lobby") && $("screen-lobby").classList.contains("active");
    const msgBar = $("msgBar");
    const joinError = $("joinError");

    if ((activeGame || activeLobby) && msgBar) {
      msgBar.textContent = msg;
      msgBar.style.opacity = "1";
    } else if (joinError) {
      joinError.textContent = msg;
    } else {
      alert(msg);
    }
  }

  function handleServerMessage(data) {
    switch (data.type) {
      case "joined":
        myPlayerId = data.player_id;
        myName = data.name || pendingJoinName || myName || localStorage.getItem("clash_name") || "Player";
        localStorage.setItem("clash_player_id", String(myPlayerId));
        localStorage.setItem("clash_session_id", data.session_id || "");
        localStorage.setItem("clash_name", myName);
        const jb = $("joinBtn");
        if (jb) jb.classList.remove("btn-loading");
        break;
      case "lobby_state":
        latestLobbyState = data;
        if (!latestGameState || !data.started) {
          renderLobby(data);
          showScreen("lobby");
        }
        break;
      case "mp_game_state":
        latestGameState = data;
        renderGame(data);
        showScreen("game");
        break;
      case "error":
        showGlobalError(data.message);
        break;
      case "pong_check":
        lastLatencyMs = Date.now() - data.t;
        updatePingBadge();
        break;
    }
  }

  // ---------------- JOIN SCREEN ----------------

  $("joinBtn").addEventListener("click", doJoin);
  $("nameInput").addEventListener("keydown", (e) => { if (e.key === "Enter") doJoin(); });

  function doJoin() {
    if (ws) return;
    const name = $("nameInput").value.trim();
    if (!name) {
      $("joinError").textContent = "Isi nama dulu ya.";
      return;
    }
    myName = name;
    $("joinError").textContent = "";
    $("joinBtn").disabled = true;
    $("joinBtn").classList.add("btn-loading"); // NEW: kasih spinner selagi connect
    connect();
  }

  // ---------------- LOBBY ----------------

  let localPackCount = 1;
  let localCardsEach = 7;
  let localBotCount = 0;
  let localExtremeMode = false;
  let localExtremePackCount = 1;
  let nameInputTouched = false;

  document.querySelectorAll(".stepper").forEach((stepperEl) => {
    stepperEl.querySelectorAll(".step-btn").forEach((btn) => {
      btn.addEventListener("click", () => {
        const target = stepperEl.dataset.target;
        const dir = parseInt(btn.dataset.dir, 10);
        if (target === "pack_count") {
          localPackCount = Math.max(1, Math.min(4, localPackCount + dir));
          $("packVal").textContent = localPackCount;
        } else if (target === "cards_each") {
          localCardsEach = Math.max(3, Math.min(20, localCardsEach + dir));
          $("cardsVal").textContent = localCardsEach;
        } else if (target === "bot_count") {
          localBotCount = Math.max(0, Math.min(9, localBotCount + dir));
          $("botVal").textContent = localBotCount;
        } else if (target === "extreme_pack_count") {
          localExtremePackCount = Math.max(1, Math.min(8, localExtremePackCount + dir));
          $("extremePackVal").textContent = localExtremePackCount;
        }
        send({
          type: "set_settings",
          pack_count: localPackCount,
          cards_each: localCardsEach,
          bot_count: localBotCount,
          extreme_mode: localExtremeMode,
          extreme_pack_count: localExtremePackCount
        });
      });
    });
  });

  $("startBtn").addEventListener("click", () => send({ type: "start_game" }));
  $("rematchBtn").addEventListener("click", () => send({ type: "rematch" }));
  $("backToLobbyBtn").addEventListener("click", () => send({ type: "back_to_lobby" }));

  // FITUR BARU: MODE EXTREME. Toggle Normal/Extreme di lobby, cuma leader
  // yang bisa ubah (settings lain juga begitu).
  document.querySelectorAll("#modeToggle .mode-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      localExtremeMode = btn.dataset.mode === "extreme";
      document.querySelectorAll("#modeToggle .mode-btn").forEach((b) => {
        b.classList.toggle("active", b.dataset.mode === btn.dataset.mode);
      });
      send({
        type: "set_settings",
        pack_count: localPackCount,
        cards_each: localCardsEach,
        bot_count: localBotCount,
        extreme_mode: localExtremeMode,
        extreme_pack_count: localExtremePackCount
      });
    });
  });

  $("saveNameBtn").addEventListener("click", saveMyName);
  $("myNameInput").addEventListener("keydown", (e) => { if (e.key === "Enter") saveMyName(); });
  $("myNameInput").addEventListener("input", () => { nameInputTouched = true; });

  function saveMyName() {
    const newName = $("myNameInput").value.trim();
    if (!newName) return;
    myName = newName;
    localStorage.setItem("clash_name", newName);
    send({ type: "set_name", name: newName });
    nameInputTouched = false;
  }

  function doQuit() {
    localStorage.removeItem("clash_player_id");
    localStorage.removeItem("clash_session_id");

    if (ws && ws.readyState === WebSocket.OPEN) {
      try { ws.send(JSON.stringify({ type: "leave_lobby" })); } catch (e) { /* ignore */ }
    }
    if (ws) {
      try { ws.onclose = null; ws.close(1000, "user_quit"); } catch (e) { /* ignore */ }
    }
    setTimeout(() => { location.reload(); }, 150);
  }

  $("quitLobbyBtn").addEventListener("click", doQuit);

  function renderLobby(state) {
    const isLeader = state.leader_id === myPlayerId;
    localPackCount = state.pack_count;
    localCardsEach = state.cards_each;
    localBotCount = state.bot_count || 0;
    localExtremeMode = !!state.extreme_mode;
    localExtremePackCount = state.extreme_pack_count || 1;
    $("packVal").textContent = localPackCount;
    $("cardsVal").textContent = localCardsEach;
    $("botVal").textContent = localBotCount;
    if ($("extremePackVal")) $("extremePackVal").textContent = localExtremePackCount;
    if ($("extremePackSetting")) $("extremePackSetting").style.display = (isLeader && localExtremeMode) ? "flex" : "none";
    document.querySelectorAll("#modeToggle .mode-btn").forEach((b) => {
      b.classList.toggle("active", (b.dataset.mode === "extreme") === localExtremeMode);
    });
    const modeText = $("modeIndicatorText");
    if (modeText) {
      if (!isLeader) {
        modeText.style.display = "block";
        modeText.textContent = localExtremeMode ? `Mode: EXTREME (${localExtremePackCount} paket kartu spesial)` : "Mode: Normal";
      } else {
        modeText.style.display = "none";
      }
    }

    const container = $("lobbyPlayers");
    container.innerHTML = "";
 
    state.players.forEach((p) => {
      const row = document.createElement("div");
      row.className = "lobby-player-row" + (p.connected ? "" : " offline");
      row.innerHTML = `
        <span class="dot"></span>
        ${p.is_leader ? '<span class="crown">&#9733;</span>' : ""}
        <span class="pname">${escapeHtml(p.name)}${p.id === myPlayerId ? " (kamu)" : ""}</span>
      `;
      // FITUR BARU: leader bisa kick slot Offline langsung dari lobby.
      if (isLeader && !p.connected && p.id !== myPlayerId) {
        const kickBtn = document.createElement("button");
        kickBtn.className = "btn btn-small lobby-kick-btn";
        kickBtn.textContent = "KICK";
        kickBtn.addEventListener("click", () => {
          if (confirm(`Kick ${p.name} dari lobby?`)) {
            send({ type: "kick_player", target_player_id: p.id });
          }
        });
        row.appendChild(kickBtn);
      }
      container.appendChild(row);
    });

    if (!nameInputTouched && document.activeElement !== $("myNameInput")) {
      const me = state.players.find((p) => p.id === myPlayerId);
      $("myNameInput").value = me ? me.name : myName;
    }

    $("leaderSettings").style.display = isLeader ? "flex" : "none";
    $("waitingText").style.display = isLeader ? "none" : "block";
    const connectedHumans = state.players.filter((p) => p.connected).length;
    $("startBtn").disabled = (connectedHumans + localBotCount) < 2;
  }

  // ---------------- GAME RENDER ----------------

  function cardKey(c) { return c ? `${c.color}-${c.value}` : "none"; }

  function valueLabel(value) {
    if (value === "Draw Two") return "+2";
    if (value === "Wild Draw Four") return "+4";
    if (value === "Reverse") return "REV";
    if (value === "Skip") return "SKIP";
    if (value === "Wild") return "W";
    return value;
  }

  function buildCardEl(card, opts = {}) {
    const el = document.createElement("div");
    // FITUR BARU: kalau ini kartu Wild/+4 DAN ada colorOverride (dipakai untuk
    // top_card setelah pemain pilih warnanya), tampilkan warna solid pilihan
    // itu (bukan rainbow lagi), tapi label "W"/"+4" tetap sama.
    let colorClass;
    let isWildColored = false;
    if (card.color === "Wild") {
      if (opts.colorOverride) {
        colorClass = opts.colorOverride.toLowerCase();
        isWildColored = true;
      } else {
        colorClass = "wild";
      }
    } else {
      colorClass = card.color.toLowerCase();
    }
    el.className = "card " + colorClass + (opts.small ? " small" : "") + (isWildColored ? " wild-colored" : "");
    el.innerHTML = `<div class="pip"></div><div class="val">${valueLabel(card.value)}</div>`;
    return el;
  }

  function buildBackCardEl(small) {
    const el = document.createElement("div");
    el.className = "card back" + (small ? " small" : "");
    return el;
  }

  // ---------------- ANIMASI KARTU DILETAKKAN ----------------

  // Membuat kartu "clone" yang terbang dari posisi asal (fromRect) menuju
  // tumpukan tengah (toRect / topCardWrap), lalu menghilang begitu sampai.
  // Kartu asli di tumpukan tengah tetap dirender seperti biasa oleh
  // renderTopCardAndHistory - clone ini cuma efek visual di atasnya.
  function animateCardFly(fromRect, card, colorOverride, toRect) {
    if (!fromRect || fromRect.width === 0) return;
    const target = toRect || $("topCardWrap").getBoundingClientRect();

    const clone = buildCardEl(card, { colorOverride });
    clone.classList.add("flying-card");
    clone.style.position = "fixed";
    clone.style.left = fromRect.left + "px";
    clone.style.top = fromRect.top + "px";
    clone.style.width = fromRect.width + "px";
    clone.style.height = fromRect.height + "px";
    clone.style.margin = "0";
    clone.style.transition = "none";
    document.body.appendChild(clone);

    const dx = (target.left + (target.width - fromRect.width) / 2) - fromRect.left;
    const dy = (target.top + (target.height - fromRect.height) / 2) - fromRect.top;
    const rot = (Math.random() * 26 - 13).toFixed(1);

    requestAnimationFrame(() => {
      requestAnimationFrame(() => {
        clone.style.transition = "transform 0.42s cubic-bezier(.25,.7,.3,1.15), opacity 0.42s ease 0.24s";
        clone.style.transform = `translate(${dx}px, ${dy}px) rotate(${rot}deg) scale(0.94)`;
      });
    });

    setTimeout(() => { clone.style.opacity = "0"; }, 380);
    setTimeout(() => { clone.remove(); }, 460);
  }

  function getHandSignature(hand) {
    return (hand || []).map((c) => `${c.color}:${c.value}`).join("|");
  }

  function resetSelectionIfStateChanged(state) {
    const currentHandSignature = getHandSignature(state.your_hand || []);
    const turnChanged = prevCurrentPlayerId !== null && prevCurrentPlayerId !== state.current_player_id;
    const handChanged = prevHandSignature !== "" && prevHandSignature !== currentHandSignature;

    if (selection.value !== null && (turnChanged || handChanged)) {
      selection = { value: null, indices: [] };
      pendingColorFly = null;
      const bar = $("selectionBar");
      if (bar) bar.style.display = "none";
    }

    prevHandSignature = currentHandSignature;
  }

  function renderGame(state) {
    resetSelectionIfStateChanged(state);
    $("screen-game").classList.toggle("extreme-arena", !!state.extreme_mode);
    renderMessage(state);
    renderPlayers(state);
    renderDirectionArrow(state);
    renderTopCardAndHistory(state);
    renderDeckPile(state);
    $("deckCount").textContent = state.deck_count + " kartu";
    renderHand(state);
    // renderStash(state);
    renderClashButton(state);
    renderColorPicker(state);
    renderStealPicker(state);
    renderGameOver(state);
    checkCelebrations(state);
    checkExtremeEffect(state);
    maybeShowExtremeIntro(state);
    maybeShowFirstTimeHint(state);

    const hintExtreme = $("hintExtremeSection");
    if (hintExtreme) hintExtreme.style.display = state.extreme_mode ? "block" : "none";

    prevTopCardKey = cardKey(state.top_card);
    prevCurrentPlayerId = state.current_player_id;
  }

  function renderDeckPile(state) {
    const pile = $("deckPile");
    if (!pile) return;
    pile.classList.toggle("deck-extreme", !!state.extreme_mode);

    if (state.extreme_mode && !deckExtremeParticlesBuilt) {
      buildDeckExtremeParticles(pile);
      deckExtremeParticlesBuilt = true;
    } else if (!state.extreme_mode && deckExtremeParticlesBuilt) {
      const layer = pile.querySelector(".deck-extreme-particles");
      if (layer) layer.remove();
      deckExtremeParticlesBuilt = false;
    }
  }

  function buildDeckExtremeParticles(pile) {
    const layer = document.createElement("div");
    layer.className = "deck-extreme-particles";
    const count = 6;
    for (let i = 0; i < count; i++) {
      const p = document.createElement("span");
      p.className = "deck-spark";
      p.style.left = (10 + Math.random() * 80) + "%";
      p.style.animationDelay = (Math.random() * 2.2).toFixed(2) + "s";
      p.style.animationDuration = (1.8 + Math.random() * 1.4).toFixed(2) + "s";
      layer.appendChild(p);
    }
    pile.appendChild(layer);
  }

  function checkCelebrations(state) {
    // FITUR BARU: setiap kali ada pemain yang BARU SAJA selesai (kartunya
    // habis), tampilkan confetti + toast singkat merayakan itu - bukan cuma
    // pemenang pertama, semua yang finis dapat celebrasi kecil.
    const newFinishedIds = new Set(state.players.filter((p) => p.is_finished).map((p) => p.id));
    if (newFinishedIds.size < prevFinishedIds.size) {
      // Game baru dimulai lagi (rematch) - reset diam-diam, tanpa celebrasi.
      prevFinishedIds = newFinishedIds;
      return;
    }
    state.players.forEach((p) => {
      if (p.is_finished && !prevFinishedIds.has(p.id)) {
        triggerCelebration(p.name, p.rank);
      }
    });
    prevFinishedIds = newFinishedIds;
  }

  function triggerCelebration(name, rank) {
    const layer = $("celebrationLayer");
    if (!layer) return;
    const colors = ["#dc4141", "#4173dc", "#41b45f", "#ebcd46", "#ffdc5a"];
    const count = 46;
    for (let i = 0; i < count; i++) {
      const piece = document.createElement("div");
      piece.className = "confetti-piece";
      piece.style.left = Math.random() * 100 + "%";
      piece.style.background = colors[Math.floor(Math.random() * colors.length)];
      piece.style.borderRadius = Math.random() > 0.5 ? "50%" : "2px";
      const duration = 1.6 + Math.random() * 1.3;
      piece.style.animationDuration = duration + "s";
      piece.style.animationDelay = (Math.random() * 0.25) + "s";
      layer.appendChild(piece);
      setTimeout(() => piece.remove(), (duration + 0.5) * 1000);
    }

    const toast = document.createElement("div");
    toast.className = "celebration-toast";
    toast.textContent = rank === 1 ? `\ud83c\udfc6 ${name} FINIS PERTAMA!` : `\ud83c\udf89 ${name} selesai! Peringkat #${rank}`;
    layer.appendChild(toast);
    setTimeout(() => toast.remove(), 2700);
  }

  function renderMessage(state) {
    $("msgBar").textContent = state.message || "";
  }

  function renderPlayers(state) {
    const row = $("opponentsRow");
    row.classList.toggle("extreme-theme", !!state.extreme_mode);
    row.innerHTML = "";
    state.players.forEach((p) => {
      const chip = document.createElement("div");
      let cls = "player-chip";
      if (p.is_current_turn) cls += " turn";
      if (p.is_finished) cls += " finished";
      if (!p.connected) cls += " offline";

      const hasBomb = p.bomb_turns_left !== null && p.bomb_turns_left !== undefined;
      if (hasBomb) {
        cls += " has-bomb";
        if (p.bomb_turns_left <= 1) cls += " bomb-critical";
      }
      chip.className = cls;
      chip.dataset.playerId = String(p.id);

      let badge = "";
      if (p.is_finished && p.rank) badge = `#${p.rank}`;
      else if (!p.connected) badge = "Offline";
      else if (p.uno_called && p.card_count === 1) badge = "CLASH!";

      const bombBadge = hasBomb
        ? `<div class="pc-bomb-badge">\u{1F4A3} ${p.bomb_turns_left}</div>`
        : "";

      chip.innerHTML = `
        <div class="pc-name">${p.is_you ? "&#9679; " : ""}${escapeHtml(p.name)}</div>
        <div class="pc-count">${p.card_count} kartu</div>
        ${badge ? `<div class="pc-badge">${badge}</div>` : ""}
        ${bombBadge}
      `;
      row.appendChild(chip);
    });
  }

  function renderDirectionArrow(state) {
    const arrowWrap = $("directionArrow");
    arrowWrap.className = "direction-arrow" + (state.direction === -1 ? " reverse" : "");
  }

  function renderTopCardAndHistory(state) {
    const wrap = $("topCardWrap");
    const override = state.top_card && state.top_card.color === "Wild" ? state.current_color : null;
    const newKey = cardKey(state.top_card);
    const topChanged = prevTopCardKey !== null && newKey !== prevTopCardKey;

    // ANIMASI KARTU DILETAKKAN: kalau kartu top berubah dan itu BUKAN hasil
    // main kita sendiri (yang sudah dianimasikan langsung dari klik tangan),
    // berarti pemain lain/bot yang barusan main - terbangkan clone kartu dari
    // posisi chip mereka menuju tumpukan tengah supaya kelihatan "diletakkan".
    if (topChanged && state.top_card) {
      const recentLocal = Date.now() - lastLocalPlayAt < 700;
      if (!recentLocal && prevCurrentPlayerId !== null && prevCurrentPlayerId !== myPlayerId) {
        const chip = document.querySelector(`.player-chip[data-player-id="${prevCurrentPlayerId}"]`);
        if (chip) {
          animateCardFly(chip.getBoundingClientRect(), state.top_card, override, wrap.getBoundingClientRect());
        }
      }
    }

    wrap.innerHTML = "";
    if (state.top_card) {
      // FIX: top_card Wild sekarang ditampilkan pakai current_color (warna
      // yang barusan dipilih pemain), bukan rainbow terus-terusan.
      const el = buildCardEl(state.top_card, { colorOverride: override });
      wrap.appendChild(el);
    }
    if (topChanged) {
      wrap.classList.remove("pop");
      void wrap.offsetWidth; // restart animasi
      wrap.classList.add("pop");
    }

    const historyWrap = $("historyStack");
    historyWrap.innerHTML = "";
    (state.top_card_history || []).forEach((card, idx) => {
      const el = buildCardEl(card, { small: true });
      el.style.left = (idx * 10) + "px";
      el.style.zIndex = String(10 - idx);
      historyWrap.appendChild(el);
    });
  }

  function isPlayable(card, state) {
    if (!card || !state || !state.top_card) return false;
    if (card.color === "Wild") return true;
    return card.color === state.current_color || card.value === state.top_card.value;
  }

  function meUnoCalled(state) {
    const me = (state.players || []).find((p) => p.id === myPlayerId);
    return !!(me && me.uno_called);
  }

  function isMyTurnFree(state) {
    return (
      !state.game_over &&
      state.current_player_id === myPlayerId &&
      !state.awaiting_wild_color &&
      !state.awaiting_number_color &&
      !state.awaiting_steal_pick
    );
  }

  function renderHand(state) {
    const handRow = $("handRow");
    handRow.innerHTML = "";
    const hand = state.your_hand || [];
    const myTurn = isMyTurnFree(state);

    hand.forEach((card, index) => {
      // FITUR BARU: kartu terakhir (sisa 1) tidak boleh dimainkan sebelum
      // CLASH dipencet dulu - tampilkan sebagai belum "playable" biar jelas
      // kelihatan kenapa gak bisa diklik, bukan cuma ditolak diam-diam.
      const mustClashFirst = hand.length === 1 && !meUnoCalled(state);
      const playable = myTurn && isPlayable(card, state) && !mustClashFirst;
      const inSelection = selection.value !== null && card.value === selection.value;
      const el = buildCardEl(card);
      if (playable || inSelection) el.classList.add("playable");
      if (selection.indices.includes(index)) el.classList.add("selected");

      el.addEventListener("click", () => onHandCardClick(index, card, state, playable, inSelection, el));
      handRow.appendChild(el);
    });

    const stash = state.your_stash || [];
    stash.forEach((item) => {
      const stashEl = buildStashHandCardEl(item, myTurn);
      stashEl.addEventListener("click", () => {
        if (!isMyTurnFree(state)) return;
        onUseStashClick(item, state);
      });
      handRow.appendChild(stashEl);
    });

    $("drawBtn").style.display = myTurn ? "inline-block" : "none";
    $("drawBtn").onclick = () => { clearSelection(); send({ type: "draw_card" }); };

    updateSelectionBar();
  }

  function onHandCardClick(index, card, state, playable, inSelection, el) {
    if (selection.value === null) {
      if (!playable) return;
      const isNumber = /^[0-9]$/.test(card.value);
      const duplicates = (state.your_hand || []).filter((c, i) => i !== index && c.value === card.value);
      if (isNumber && duplicates.length > 0) {
        selection = { value: card.value, indices: [index] };
        renderHand(state);
        return;
      }
      // Tidak ada kembarannya -> langsung main, tanpa perlu konfirmasi tambahan.
      // ANIMASI KARTU DILETAKKAN: kartu Wild/+4 masih akan nanya pilih warna
      // dulu - jangan terbangkan kartunya sekarang, tunda sampai warnanya
      // benar-benar dipilih (lihat renderColorPicker), supaya urutannya
      // "pilih dulu, baru kartu keliatan diletakkan", bukan kebalik.
      if (card.color === "Wild") {
        if (el) pendingColorFly = { needWild: true, items: [{ rect: el.getBoundingClientRect(), card }] };
      } else {
        if (el) animateCardFly(el.getBoundingClientRect(), card, null);
        lastLocalPlayAt = Date.now();
      }
      send({ type: "play_cards", indices: [index] });
      return;
    }

    if (card.value !== selection.value) return; // sedang milih grup lain, abaikan klik luar grup

    const pos = selection.indices.indexOf(index);
    if (pos >= 0) {
      selection.indices.splice(pos, 1);
      if (selection.indices.length === 0) selection = { value: null, indices: [] };
    } else {
      selection.indices.push(index);
    }
    renderHand(state);
  }

  function clearSelection() {
    selection = { value: null, indices: [] };
    updateSelectionBar();
  }

  function updateSelectionBar() {
    const bar = $("selectionBar");
    if (selection.value === null || selection.indices.length === 0) {
      bar.style.display = "none";
      return;
    }
    bar.style.display = "flex";
    $("selectionText").textContent = `${selection.indices.length} kartu "${valueLabel(selection.value)}" dipilih`;
  }

  $("confirmPlayBtn").addEventListener("click", () => {
    if (selection.indices.length === 0) return;
    // ANIMASI KARTU DILETAKKAN: terbangkan tiap kartu kembar yang dipilih,
    // dengan sedikit jeda antar kartu supaya terlihat "dijatuhkan" satu-satu.
    // TAPI kalau kartu kembarnya beda-beda warna, server bakal nanya "pilih
    // warna top card" dulu - tunda animasinya sampai warna itu benar-benar
    // dipilih (sama seperti Wild), jangan animasi duluan sebelum keputusan.
    const handRow = $("handRow");
    const hand = (latestGameState && latestGameState.your_hand) || [];
    const items = selection.indices
      .map((idx) => ({ rect: handRow.children[idx] ? handRow.children[idx].getBoundingClientRect() : null, card: hand[idx] }))
      .filter((it) => it.rect && it.card);
    const distinctColors = new Set(items.map((it) => it.card.color));

    if (distinctColors.size > 1) {
      pendingColorFly = { needWild: false, items };
    } else {
      items.forEach(({ rect, card }, i) => {
        setTimeout(() => animateCardFly(rect, card, null), i * 90);
      });
      lastLocalPlayAt = Date.now();
    }
    send({ type: "play_cards", indices: selection.indices.slice() });
    clearSelection();
  });
  $("cancelPlayBtn").addEventListener("click", () => {
    clearSelection();
    if (latestGameState) renderHand(latestGameState);
  });

  // ---------------- MODE EXTREME: STASH ----------------

  const STASH_LABELS = {
    "Swap Rotasi": "SWAP",
    "+2 Skip": "+2 SKIP",
    "+2 Reverse": "+2 REV",
    "Curi": "CURI",
    "Bom Waktu": "BOM",
    "Recall": "RECALL",
  };

  const STASH_ICONS = {
    "Swap Rotasi": "🔀",
    "+2 Skip": "⛔",
    "+2 Reverse": "🔁",
    "Curi": "🤏",
    "Bom Waktu": "💣",
    "Recall": "↩️",
  };

  const STASH_NEEDS_TARGET = ["Curi", "Bom Waktu", "Recall"];

  function buildStashHandCardEl(item, clickable) {
    const el = document.createElement("div");
    const slug = item.kind.replace(/[^a-zA-Z0-9]/g, "").toLowerCase();
    el.className = "card stash-inline stash-" + slug + (clickable ? " playable" : " disabled-stash");
    const label = STASH_LABELS[item.kind] || item.kind;
    const icon = STASH_ICONS[item.kind] || "✨";
    el.innerHTML = `
      <div class="stash-inline-icon">${icon}</div>
      <div class="val stash-inline-label">${label}</div>
    `;
    if (item.kind === "Bom Waktu" && item.turns_left !== null && item.turns_left !== undefined) {
      const badge = document.createElement("div");
      badge.className = "stash-timer";
      badge.textContent = item.turns_left;
      el.appendChild(badge);
      if (item.turns_left <= 1) el.classList.add("critical");
    }
    return el;
  }

  // function buildStashCardEl(item) {
  //   const el = document.createElement("div");
  //   el.className = "stash-card stash-" + item.kind.replace(/[^a-zA-Z0-9]/g, "").toLowerCase();
  //   const label = STASH_LABELS[item.kind] || item.kind;
  //   el.innerHTML = `<div class="stash-label">${label}</div>`;
  //   if (item.kind === "Bom Waktu" && item.turns_left !== null && item.turns_left !== undefined) {
  //     const badge = document.createElement("div");
  //     badge.className = "stash-timer";
  //     badge.textContent = item.turns_left;
  //     el.appendChild(badge);
  //     if (item.turns_left <= 1) {
  //       el.classList.add("critical");
  //     }
  //   }
  //   return el;
  // }

  // function renderStash(state) {
  //   const row = $("stashRow");
  //   row.innerHTML = "";
  //   const stash = state.your_stash || [];
  //   if (!state.extreme_mode || stash.length === 0) {
  //     row.style.display = "none";
  //     return;
  //   }
  //   row.style.display = "flex";

  //   const label = document.createElement("div");
  //   label.className = "stash-row-label";
  //   label.textContent = "STASH";
  //   row.appendChild(label);

  //   const myTurn = isMyTurnFree(state);

  //   stash.forEach((item) => {
  //     const wrap = document.createElement("div");
  //     wrap.className = "stash-item";
  //     const cardEl = buildStashCardEl(item);
  //     wrap.appendChild(cardEl);

  //     const btn = document.createElement("button");
  //     btn.className = "btn btn-small stash-use-btn";
  //     btn.textContent = "PAKAI";
  //     btn.disabled = !myTurn;
  //     btn.addEventListener("click", () => onUseStashClick(item, state));
  //     wrap.appendChild(btn);

  //     row.appendChild(wrap);
  //   });
  // }

  function onUseStashClick(item, state) {
    if (STASH_NEEDS_TARGET.includes(item.kind)) {
      openTargetPicker(item, state);
    } else {
      send({ type: "use_stash_item", stash_id: item.id });
    }
  }

  function openTargetPicker(item, state) {
    pendingStashId = item.id;
    const overlay = $("targetPickerOverlay");
    const optionsWrap = $("targetPickerOptions");

    let candidates;
    if (item.kind === "Recall") {
      $("targetPickerTitle").textContent = "Recall siapa? (cuma bisa 1x per pemain)";
      candidates = (state.players || []).filter(
        (p) => p.id !== myPlayerId && p.is_finished && !p.recall_used
      );
    } else {
      $("targetPickerTitle").textContent = item.kind === "Curi"
        ? "Curi kartu dari siapa?"
        : "Oper Bom Waktu ke siapa?";
      candidates = (state.players || []).filter(
        (p) => p.id !== myPlayerId && !p.is_finished
      );
    }

    optionsWrap.innerHTML = "";
    if (candidates.length === 0) {
      optionsWrap.innerHTML = '<p class="waiting-text">Tidak ada target yang valid.</p>';
    }
    candidates.forEach((p) => {
      const btn = document.createElement("button");
      btn.className = "btn target-option-btn";
      btn.textContent = p.name + (p.connected ? "" : " (offline)");
      btn.addEventListener("click", () => {
        send({ type: "use_stash_item", stash_id: pendingStashId, target_player_id: p.id });
        closeTargetPicker();
      });
      optionsWrap.appendChild(btn);
    });

    overlay.style.display = "flex";
  }

  function closeTargetPicker() {
    pendingStashId = null;
    $("targetPickerOverlay").style.display = "none";
  }

  $("cancelTargetBtn").addEventListener("click", closeTargetPicker);

  function renderClashButton(state) {
    const wrap = $("clashBtnWrap");
    wrap.innerHTML = "";
    const me = (state.players || []).find((p) => p.id === myPlayerId);
    const needClash = me && !me.is_finished && me.card_count === 1 && !me.uno_called && !state.game_over;
    if (needClash) {
      const btn = document.createElement("button");
      btn.className = "clash-btn";
      btn.textContent = "CLASH!";
      const x = 10 + Math.random() * 70;
      const y = 20 + Math.random() * 45;
      btn.style.left = x + "%";
      btn.style.top = y + "%";
      btn.addEventListener("click", () => send({ type: "call_clash" }));
      wrap.appendChild(btn);
    }
  }

  function renderColorPicker(state) {
    const overlay = $("colorPickerOverlay");
    const optionsWrap = $("colorPickerOptions");

    const needWild = state.awaiting_wild_color && state.wild_player_id === myPlayerId;
    const needNumber = state.awaiting_number_color && state.number_color_player_id === myPlayerId;

    if (!needWild && !needNumber) {
      overlay.style.display = (state.awaiting_wild_color || state.awaiting_number_color) ? "flex" : "none";
      if (overlay.style.display === "flex") {
        $("colorPickerTitle").textContent = "Menunggu pemain lain memilih warna...";
        optionsWrap.innerHTML = "";
      }
      return;
    }

    overlay.style.display = "flex";
    const colors = needWild ? ["Red", "Blue", "Green", "Yellow"] : state.number_color_options;
    const isDrawFour = state.top_card && state.top_card.value === "Wild Draw Four";
    $("colorPickerTitle").textContent = needWild
      ? (isDrawFour ? "Pilih Warna +4 (lawan kena 4 kartu!)" : "Pilih Warna Wild")
      : `Pilih Warna Top Card untuk ${valueLabel(state.pending_number_value)}`;

    optionsWrap.innerHTML = "";
    colors.forEach((color) => {
      const btn = document.createElement("button");
      btn.className = "color-swatch " + color;
      btn.textContent = color;
      btn.addEventListener("click", () => {
        // ANIMASI KARTU DILETAKKAN: baru sekarang, setelah warna dipilih,
        // kartunya "diletakkan" secara visual - bukan sebelum ini.
        if (pendingColorFly) {
          const chosen = color;
          pendingColorFly.items.forEach(({ rect, card }, i) => {
            const override = pendingColorFly.needWild ? chosen : null;
            setTimeout(() => animateCardFly(rect, card, override), i * 80);
          });
          pendingColorFly = null;
          lastLocalPlayAt = Date.now();
        }
        send({ type: needWild ? "choose_wild_color" : "choose_number_color", color });
      });
      optionsWrap.appendChild(btn);
    });
  }

  // FITUR BARU: onboarding otomatis. Tampilkan panel "Cara Main" sekali saja
  // seumur browser (pakai localStorage), begitu pemain pertama kali betul2
  // masuk ke layar game - ditunda supaya tidak tabrakan dgn popup Extreme
  // Mode (yang juga overlay di screen yang sama).
  function maybeShowFirstTimeHint(state) {
    if (firstTimeHintShown) return;
    if (localStorage.getItem("clash_hint_v1_seen")) return;
    firstTimeHintShown = true;
    localStorage.setItem("clash_hint_v1_seen", "1");

    const delay = state.extreme_mode ? 5600 : 1100; // nunggu intro extreme selesai dulu kalau ada
    setTimeout(() => {
      const hintOverlay = $("hintOverlay");
      // Jangan timpa overlay lain yang lagi aktif (color picker, target picker, dst).
      const anyOtherOverlayOpen = ["colorPickerOverlay", "targetPickerOverlay", "gameOverOverlay", "extremeIntroOverlay"]
        .some((id) => $(id) && $(id).style.display === "flex");
      if (!anyOtherOverlayOpen && hintOverlay) hintOverlay.style.display = "flex";
    }, delay);
  }
  // FITUR BARU: Curi 2 langkah - setelah target dipilih, tampilkan sejumlah
  // kartu tertutup (terbalik) sebanyak kartu di tangan target, biar pemain
  // sendiri yang klik salah satunya (buta, tidak tahu isinya).
  function renderStealPicker(state) {
    const overlay = $("stealPickerOverlay");
    if (!overlay) return;

    if (!state.awaiting_steal_pick || state.steal_player_id !== myPlayerId) {
      overlay.style.display = "none";
      return;
    }

    overlay.style.display = "flex";
    const target = (state.players || []).find((p) => p.id === state.steal_target_id);
    const count = target ? target.card_count : 0;
    $("stealPickerTitle").textContent = target
      ? `Pilih 1 kartu dari tangan ${target.name}`
      : "Pilih kartu";

    const wrap = $("stealPickerCards");
    wrap.innerHTML = "";
    for (let i = 0; i < count; i++) {
      const el = buildBackCardEl(false);
      el.classList.add("steal-pick-card");
      el.addEventListener("click", () => {
        send({ type: "pick_steal_card", card_index: i });
      });
      wrap.appendChild(el);
    }
  }

  function renderGameOver(state) {
    const overlay = $("gameOverOverlay");
    if (!state.game_over) { overlay.style.display = "none"; return; }
    overlay.style.display = "flex";

    const winner = state.players.find((p) => p.id === state.winner_id);
    $("winnerText").textContent = winner ? `\u{1F3C6} ${winner.name} MENANG!` : "GAME SELESAI";

    const medals = ["\u{1F947}", "\u{1F948}", "\u{1F949}"];

    const rankingWrap = $("rankingList");
    rankingWrap.innerHTML = "";
    state.players
      .filter((p) => p.rank)
      .sort((a, b) => a.rank - b.rank)
      .forEach((p, idx) => {
        const row = document.createElement("div");
        row.className = "rank-row" + (p.rank === 1 ? " rank-first" : p.rank === 2 ? " rank-second" : p.rank === 3 ? " rank-third" : "");
        row.style.animationDelay = (idx * 0.08) + "s";
        const icon = medals[p.rank - 1] || `#${p.rank}`;
        row.innerHTML = `
          <span class="rank-icon">${icon}</span>
          <span class="rank-name">${escapeHtml(p.name)}</span>
        `;
        rankingWrap.appendChild(row);
      });

    const isLeader = latestLobbyState && latestLobbyState.leader_id === myPlayerId;
    $("leaderGameOverActions").style.display = isLeader ? "flex" : "none";
    $("rematchWaitText").style.display = isLeader ? "none" : "block";
  }

  $("quitGameBtn").addEventListener("click", doQuit);
  $("quitInGameBtn").addEventListener("click", () => {
    if (confirm("Yakin mau keluar dari game?")) doQuit();
  });

  function escapeHtml(str) {
    const div = document.createElement("div");
    div.textContent = str;
    return div.innerHTML;
  }
})();

