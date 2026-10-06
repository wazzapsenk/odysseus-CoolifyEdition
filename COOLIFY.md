# Odysseus — Coolify Edition

Bu fork, [Odysseus](https://github.com/odysseus-dev/odysseus)'u Coolify
üzerinde çalıştırmak için gereken dosyaları içerir. Upstream dosyalarına
dokunulmaz; GitHub'daki **Sync fork** bu yüzden çakışmasız çalışır. Fork'a özel
dosyalar:

| Dosya | Görevi |
|---|---|
| `docker-compose.coolify.yml` | Coolify için compose (Traefik arkasında, host portu yok) |
| `integrations/telegram/` | Telegram botu (sohbet, hatırlatma, takvim, hafıza) |
| `COOLIFY.md` | Bu rehber |

Uygulama kodu repodan değil, `ODYSSEUS_IMAGE` ile sabitlenen GHCR imajından
gelir. Repodan sadece compose dosyası ile bind mount edilen dosyalar kullanılır:
SearXNG ayar şablonu, migration betiği ve Telegram botu.

## Kurulum

1. **Kaynak:** Coolify'da New Resource → Public Repository
   - URL: fork'unun GitHub adresi (`https://github.com/<kullanıcı>/<fork>`)
   - **Check Repository**'ye bas; branch `dev` olmalı.
2. **Build:**
   - Build Pack: **Docker Compose**
   - Base directory: `/`
   - Docker Compose Location: `/docker-compose.coolify.yml`
3. **⚠️ Preserve repository during deployment: AÇIK.** Ayar **Configuration →
   General** sayfasında, compose konumu alanının hemen altında.
   - Neden gerekli: Coolify repoyu sadece geçici bir build konteynerine
     klonlar.
   - Bu ayar kapalıysa `./config/...`, `./scripts/...` ve
     `./integrations/telegram/...` bind mount'ları sunucuda boş klasör olarak
     oluşur ve searxng hiçbir zaman healthy olmaz.
4. **Domain:** `odysseus` servisine ekle.
   - Protocol: `https`
   - Domain: alan adın (örn. `odysseus.example.com`)
   - Port: `7000`. Bu konteyner portudur; dışarıdan erişim 443 üzerinden olur.
5. **DNS:** Bu alan adı için sunucu IP'sine bir A kaydı ekle.
   Cloudflare kullanıyorsan sertifika alınana kadar kaydı "DNS only" bırak.
6. **Değişkenler:**

   | Değişken | Not |
   |---|---|
   | `ODYSSEUS_IMAGE` | Örn. `ghcr.io/odysseus-dev/odysseus:1.0.3`. Mümkünse digest ile sabitle (`...:1.0.3@sha256:...`) |
   | `ODYSSEUS_ADMIN_USER` | Küçük harfe çevrilir. `api`, `demo`, `system`, `internal-tool` kullanılamaz |
   | `ODYSSEUS_ADMIN_PASSWORD` | En az 8 karakter. Sadece ilk açılışta, `auth.json` yokken kullanılır |
   | `SEARXNG_SECRET` | İsteğe bağlı. `openssl rand -hex 32` ile üretilebilir |
   | `ALLOWED_ORIGINS` | Önerilir. Public adresin, örn. `https://odysseus.example.com` (CORS) |
   | `OAUTH_REDIRECT_BASE_URL` | Uzak MCP OAuth kullanacaksan public adresin. Google MCP için boş bırak |
   | `TELEGRAM_*` | İsteğe bağlı; bkz. [Telegram botu](integrations/telegram/README.md) |

   Compose dosyasında hiçbir alan adı yazılı değil; adres sadece Coolify'daki
   değişkenlerde durur. `SECURE_COOKIES` varsayılan olarak `true`.
7. **Deploy.**
8. **Model bağla:** Admin olarak giriş yap. **Add API Models** bölümünden
   Provider olarak `ChatGPT Subscription`'ı seç (device code ile girilir) ya da
   bir API anahtarı ekle (Anthropic, OpenAI vb.).
   ChatGPT Subscription'da şimdilik `gpt-5.6-*` modellerini kullan; nedeni için
   bkz. [Bilinen sorunlar](#bilinen-sorunlar).

## Güncelleme

- **Odysseus sürümü:** Coolify'da `ODYSSEUS_IMAGE` değerini yeni tag'e çevir ve
  Redeploy et. `./data` korunur. Tag'ler GHCR'de listelenir:
  - main kanalı: `latest`, `X.Y.Z`
  - dev kanalı: `dev`, `X.Y.Z-dev.<sha>`
- **Fork güncellemesi:** GitHub'da **Sync fork** yap, ardından Coolify'da
  Redeploy et. Fork'a özel dosyalar upstream'de olmadığı için çakışma çıkmaz.

## Sorun giderme

| Belirti | Neden / çözüm |
|---|---|
| Deploy `dependency failed to start: container searxng... is unhealthy` ile duruyor. searxng logunda `KeyError: 'default_doi_resolver'` var | Preserve repository kapalı (Kurulum, 3. adım). Açtıktan sonra ilk denemeden kalan boş klasörleri sil (aşağıdaki komut) ve Redeploy et |
| Sohbette `Unsupported parameter: temperature` | ChatGPT Subscription'da `gpt-6*` modeli seçili. `gpt-5.6-*` seç |
| `ChatGPT Subscription quota or rate limit was reached` | Plus kotası doldu (Codex ile paylaşılıyor). Sıfırlanmasını bekle |
| Admin şifresi değişkenden değişmiyor | Beklenen davranış. Değişken sadece ilk açılışta okunur; şifreyi uygulama içinden değiştir |
| Telegram botu cevap vermiyor | Coolify'da `telegram-bot` loglarına bak. `Idle:` satırları token'ın eksik ya da yanlış olduğunu söyler. `PAIRING MODE` satırı eşleştirme bekleniyor demektir |
| Bot logunda `409 Conflict: terminated by other getUpdates request`, ardından "Stopped polling" | Aynı bot token'ını başka bir program dinliyor. BotFather'dan Odysseus için ayrı bir bot aç |
| Integrations ekranında `JSON.parse: unexpected character` ve "No integrations configured" | Uygulama o sırada yeniden başlıyordu (Redeploy). Bir dakika bekleyip sayfayı yenile; entegrasyonlar silinmez |
| Traefik logunda `Unable to obtain ACME certificate ... www.<alan-adın>` | DNS kaydı olmayan ek bir domain tanımlı. Ya Domains'ten kaldır ya da DNS kaydını ekle |

Boş klasörleri silmek için (sunucuda; `<uuid>`, Coolify'daki uygulama
UUID'si). `rmdir` yalnızca boş klasörleri siler, `data/`'ya dokunmaz:

```bash
rmdir /data/coolify/applications/<uuid>/config/searxng/settings.yml /data/coolify/applications/<uuid>/scripts/migrate_searxng_settings.py
```

Veri sunucuda `/data/coolify/applications/<uuid>/data` altında durur:
SQLite veritabanı (`app.db`), `auth.json`, skill'ler, MCP OAuth dosyaları ve npm
önbelleği. Bu klasörü yedeklemek, tüm Odysseus verisini yedeklemek demektir.

## MCP sunucuları

Odysseus bir **MCP istemcisidir**; dışarıdaki MCP sunucularına bağlanabilir.
Kendisi MCP sunucusu olarak dışarıya açılmaz. Harici ajanlar `/api/codex/*`
HTTP API'sini kullanır.

**Ekleme:** **Settings → Integrations → Add Integration → MCP Tool Server**.
Açılan "Add MCP Server" formu aşağıdaki alanları içerir. Menüdeki diğer
türler: API Service, CalDAV Calendar, Claude Agent, Codex Agent,
Contacts (CardDAV), Contacts Import, Email (IMAP/SMTP).

| Alan | Açıklama |
|---|---|
| Transport | `stdio`, `SSE` veya `Streamable HTTP` |
| Command / Args | Sadece stdio için. Örn. Command `npx`, Args `["-y", "@modelcontextprotocol/server-filesystem", "/app/data/files"]` |
| Env | JSON, örn. `{"API_KEY": "..."}` |
| URL | SSE/HTTP için. Örn. `https://mcp.example.com/mcp` |

Kaydedilen sunucuda **Reconnect**, **Disable/Enable** ve araç bazında aç/kapat
seçenekleri var. Ayarları düzenleme seçeneği yok; değiştirmek için silip
yeniden ekle.

**Konteyner içindeki stdio sunucuları:**

- **Nerede çalışır:** `odysseus` konteynerinde, `odysseus` kullanıcısıyla
  (home: `/app`).
- **Mevcut araçlar:** `node`, `npm`, `npx`, `python`, `pip`.
- **`uv`/`uvx` yok.** Gerekirse `pip install --user uv` ile kurulabilir;
  `/app/.local/bin`'e kurulur ve kalıcıdır. Bu yol denenmedi.
- **npx önbelleği:** Bu fork `npm_config_cache=/app/data/npm-cache` ayarlıyor.
  Böylece yerleşik Browser MCP gibi npx paketleri redeploy'da yeniden inmiyor.
  Kendi eklediğin stdio sunucusunun Env'ini **boş bırakırsan** MCP SDK sadece
  dar bir ortam değişkeni kümesi geçiriyor ve önbellek yine `/app/.npm`'e
  (kalıcı değil) gidiyor. Bunu önlemek için Env'e en az
  `{"npm_config_cache": "/app/data/npm-cache"}` yaz.

**Uzak sunucular (SSE/HTTP):**

- **Kimlik doğrulama:** Sadece URL alınıyor, özel header veya sabit bearer token
  girilemiyor. OAuth gerektiren sunucularda (Notion, Linear vb.) OAuth akışı
  destekleniyor:
  - Coolify'da `OAUTH_REDIRECT_BASE_URL`'i public adresine ayarla. Callback
    adresi `https://<alan-adın>/api/mcp/oauth/callback` olur.
  - Akışı tarayıcıda admin oturumu açıkken tamamla.
- **Google MCP (Gmail/Calendar, Desktop App OAuth):** Google yalnızca loopback
  yönlendirme adresini kabul ediyor.
  - `OAUTH_REDIRECT_BASE_URL`'i boş bırak; uygulama loopback adresini
    (`http://localhost:7000`) kullanır.
  - Akışı paste-back kutusuyla bitir.
  - Bu ayar, public callback isteyen diğer OAuth MCP'lerini bozar. İkisi aynı
    anda çalışmaz.

**Güvenlik:**

- MCP yönetimi sadece admin'e açık.
- stdio sunucusu eklemek, konteynerde komut çalıştırmak demektir. Yalnızca
  güvendiğin paketleri ekle.
- Ajan (`manage_mcp`) kendi başına `npx`/`uvx` sunucusu ekleyemez; bunun için
  `ODYSSEUS_MCP_ALLOWED_COMMANDS` izin listesi var.
- Araç bazında kapatma prompt'tan gizler, ama çalıştırma anında kesin bir
  engel değildir.

**Yerleşik MCP sunucuları:** Image Generation, Memory, RAG, Email ve Browser
(Playwright, npx). Hepsini kapatmak için `ODYSSEUS_DISABLE_MCP`.

## Skill'ler

Birbirinden bağımsız iki skill türü var.

**1. Odysseus skill'leri** (Odysseus'un kendi ajanı için)

- **Ekleme:** **Memory → Skills → Add Skill**
  - **Import URL:** bir GitHub klasör linki ya da skills.sh. Sadece admin.
  - **Elle:** Title / When to use / How / Tags
- **Saklandığı yer:** `/app/data/skills/<kategori>/<ad>/SKILL.md`. Bu klasör
  kalıcıdır.
- Odysseus sohbetlerden otomatik skill de çıkarabilir.

**2. Claude Code skill'i** (Claude Code'dan Odysseus'a erişim)

- **Nereden alınır:** **Settings → Integrations → Add Integration → Claude Agent**
  ekranındaki kurulum komutları.
- **Kurulum yeri:**
  - Skill: `~/.claude/skills/odysseus/` (Windows: `%USERPROFILE%\.claude\skills\odysseus\`)
  - `ODYSSEUS_URL` ve `ODYSSEUS_API_TOKEN`: `~/.claude/settings.json` →
    `env` bölümü
- **Yetkiler:** Token'ın yetkileri aynı ekrandaki anahtarlarla yönetilir.
- **Token'ı yenilersen** `settings.json`'daki değeri de güncelle.

## Bilinen sorunlar

Upstream Odysseus'ta, 1.0.3 imajında doğrulanmış sorunlar:

1. **ChatGPT Subscription + `gpt-6*` → HTTP 400 `Unsupported parameter: temperature`.**
   - **Nerede:** `src/llm_core.py`.
     - `_build_chatgpt_responses_payload` (≈1294) `temperature`'ı sadece
       `_FIXED_TEMPERATURE_MODELS` (≈1368) dışındaki modellere ekliyor.
     - Liste: `("o1", "o3", "o4", "gpt-5", "kimi-for-coding")`. `gpt-6`
       listede yok.
     - Codex backend bu parametreyi reddediyor.
   - **Kimi etkiliyor:** API'de doğrulandı. Web arayüzündeki sohbet de aynı
     payload kodunu kullanıyor, yani büyük ihtimalle o da etkileniyor (koddan
     çıkarım, test edilmedi).
   - **Geçici çözüm:** `gpt-5.6-*` modellerini kullan.
   - **Kalıcı düzeltme önerisi:** chatgpt-subscription isteklerinde
     `temperature`'ı hiç gönderme ya da listeye `gpt-6` ekle.
2. **`POST /api/v1/chat` var olan oturuma devam ederken 401 veriyor**
   (ChatGPT Subscription).
   - **Nerede:** `routes/webhook/webhook_routes.py`. Oturum devam ettirme
     dalı, `/api/chat_stream`'in yaptığı gibi
     `resolve_session_auth(...)`'ı (`routes/chat_helpers.py:455`) çağırmıyor.
     Saklanan header'larla gidiyor.
   - **Geçici çözüm:** Telegram botu her turda yeni oturum açıp sonra siliyor.
3. **`/api/v1/chat` model verilmezse listedeki ilk modeli seçiyor.** İlk model
   bir `gpt-6*` modeliyse 1. soruna düşüyor. Telegram botu bu yüzden
   `TELEGRAM_ODYSSEUS_MODEL` ile modeli sabitliyor.

Bu sorunlar upstream'e henüz bildirilmedi.
