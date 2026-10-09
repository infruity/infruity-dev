# Infruity DevBot

Bot Telegram penjembatan **FE ↔ BE** untuk repo `infruity/infruity-erp`.

| Push ke | Dilaporkan sebagai | Video demo |
|---|---|---|
| `frontend` | 🎨 Frontend (Noorman) | diminta jika ada commit `feat` |
| `backend` | ⚙️ Backend (Maulana) | diminta jika ada commit `feat` |
| `main` | 🚀 Release | tidak diminta |

Setiap push masuk ke:
- Tab **Log Push**, yang dibuat otomatis dan berisi 1 baris per push.
- Kolom catatan di tab **Plan**. Baris yang diawali `🤖` ditulis bot, catatan manual tidak disentuh.

Video demo diupload ke folder Drive `Infruity/<branch>/`.

## Alur di Telegram

1. **Registrasi (sekali):** DM bot, ketik `/start`, pilih Frontend/Backend, lalu kirim GitHub username. Admin akan menyetujui lewat tombol.
2. **Push:** laporan otomatis dikirim ke grup, lengkap dengan tag ke developer lawan.
3. **Video:** tekan **📹 Kirim video**, lalu reply dengan video (atau link Drive/YouTube). Kalau tidak ada perubahan visual, tekan **⏭ Skip**.
4. **Selesai:** `/selesai pengadaan` mencentang kolom Plan dan memberi tahu developer lawan.
5. **Reminder:** dikirim sekali kalau video belum masuk setelah `REMINDER_HOURS` jam.

Perintah: `/start` `/pending` `/selesai` `/help` `/reset` `/id`. Khusus admin: `/users` `/setgroup`.

## Konvensi commit

Bot mengenali scope dari Conventional Commits, misalnya `feat(pengadaan): ...` atau `fix(products): ...`.
Scope dipetakan ke nama fitur di Plan lewat `aliases.yaml`. Scope yang tidak dikenal masuk kelompok "Umum".
Kalau menambah fitur baru, tambahkan barisnya di `aliases.yaml` (nama harus **sama persis** dengan kolom A di Plan).

## Setup

### 1. Bot Telegram
1. Buat bot lewat @BotFather, salin token-nya.
2. Buat grup, lalu tambahkan bot dan kedua developer ke grup.
3. *(Opsional)* Jalankan BotFather → `/setprivacy` → **Disable**. Ini tidak wajib karena bot sudah menerima reply ke pesannya sendiri.

### 2. Google (Sheet + Drive)
Service account **tidak bisa** upload ke My Drive biasa karena tidak punya kuota penyimpanan, jadi pakai OAuth:
1. Di Google Cloud Console, aktifkan **Sheets API** dan **Drive API**.
2. Buat OAuth Client ID tipe **Desktop**, download, lalu simpan sebagai `secrets/credentials.json`.
3. Di laptop jalankan `pip install -r requirements.txt && python scripts/google_auth.py`, lalu login dengan akun yang punya akses **edit** ke sheet *Infruity Plan* dan folder Drive *Infruity*.
4. Copy `secrets/token.json` ke server.

### 3. Server
```bash
cp .env.example .env      # isi token, secret, admin ID
docker compose up -d --build
docker compose logs -f bot
```
Tanpa Docker:
```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
python -m bot
```
Setelah bot jalan:
- DM bot lalu ketik `/id` untuk mendapatkan ID kamu. Isi ke `TELEGRAM_ADMIN_IDS`, lalu restart bot.
- Di grup, ketik `/setgroup`.
- Kamu dan Noorman masing-masing `/start` di DM bot.

### 4. Sambungan ke GitHub
**Mode polling (default, `GITHUB_MODE=poll`).** Bot mengecek sendiri commit baru di `frontend`/`backend`/`main` setiap `POLL_INTERVAL` detik. Server tidak perlu bisa diakses dari internet, jadi tidak perlu webhook, port, atau domain. Laporan telat maksimal 1 interval.
- Disarankan isi `GITHUB_TOKEN`, yaitu fine-grained token dengan akses *Contents: Read-only* ke `infruity/infruity-erp`. Tanpa token, batasnya 60 request/jam dan interval otomatis minimal 90 detik.
- Saat pertama kali jalan, bot hanya mencatat posisi branch. Push setelah itu yang dilaporkan.
- Pelaku push ditentukan dari branch: `backend` → developer Backend, `frontend` → developer Frontend.

**Mode webhook (`GITHUB_MODE=webhook`).** Pakai mode ini kalau server punya URL publik HTTPS (Caddy/Nginx atau Cloudflare Tunnel). Lalu di repo → **Settings → Webhooks → Add webhook**:
- Payload URL: `https://bot.domainmu.com/github/webhook`
- Content type: `application/json`
- Secret: sama dengan `GITHUB_WEBHOOK_SECRET`
- Events: **Just the push event**

## Video > 20 MB (opsional)
Bot API standar Telegram hanya bisa mengunduh file maksimal **20 MB**. Untuk video yang lebih besar (sampai ~2 GB):
1. Ambil `api_id` dan `api_hash` di https://my.telegram.org, lalu isi `TELEGRAM_API_ID` dan `TELEGRAM_API_HASH` di `.env`.
2. Jalankan sekali saja: `curl https://api.telegram.org/bot<TOKEN>/logOut`
3. Isi `TELEGRAM_API_BASE_URL=http://telegram-bot-api:8081` dan `TELEGRAM_LOCAL_MODE=true` di `.env`.
4. Jalankan `docker compose --profile localapi up -d`.

## Test
```bash
pip install pytest && python -m pytest -q
```
