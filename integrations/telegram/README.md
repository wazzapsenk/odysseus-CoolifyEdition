# Odysseus Telegram botu

Odysseus'u Telegram'dan kullanmak için küçük bir köprü servisi
(`odysseus_telegram_bot.py`). Coolify compose dosyasında `telegram-bot` servisi
olarak çalışır ve iki iş yapar:

- **Sohbet ve işlem:** Mesajların Odysseus'taki modelinle (varsayılan
  `gpt-5.6-sol`) yanıtlanır. Model, gerektiğinde Odysseus'ta işlem yapar:
  hatırlatma/yapılacak ekleme, listeleme, güncelleme ve silme; takvim etkinliği
  ekleme, listeleme ve silme; hafızaya not ekleme ve listeleme.
- **Hatırlatma bildirimi (isteğe bağlı):** Odysseus'un hatırlatma kanalı
  "Webhook" yapılırsa, zamanı gelen hatırlatmalar Telegram'a mesaj olarak düşer.

Sadece Python standart kütüphanesini kullanır. Ayrı bir imaj build etmeye gerek
yoktur, `python:3.12-slim` üzerinde çalışır.

## Kurulum

### 1. Telegram botu oluştur

1. Telegram'da [@BotFather](https://t.me/BotFather) ile konuşmayı aç.
2. `/newbot` yaz; bota bir ad ve `_bot` ile biten bir kullanıcı adı ver.
3. BotFather'ın verdiği token'ı kopyala. `123456:ABC...` biçimindedir.

### 2. Odysseus'ta bot için token oluştur

**Settings → Integrations → Add Integration → Claude Agent** yolunu izle.

- **Ad:** Örneğin `Telegram Bot`.
- **Açılacak izinler** (bunlar yeterli):
  - Todos READ + WRITE
  - Calendar READ + WRITE
  - Memory READ + WRITE
- **Kapalı bırakılacaklar** (bot bunları kullanmıyor):
  - Email (READ / DRAFT / SEND)
  - Documents
  - Cookbook

Sohbet için gereken `chat` yetkisi bu entegrasyon türünde zaten var.

Token yalnızca bir kez gösterilir; kopyala. Claude Code için oluşturduğun token'ı
burada kullanma, bot için ayrı ve daha dar yetkili bir token aç.

### 3. Coolify'da değişkenleri gir

Uygulamanın **Environment Variables** sekmesinde:

| Değişken | Değer | Zorunlu |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | BotFather'dan aldığın token | ✅ |
| `TELEGRAM_ODYSSEUS_TOKEN` | 2. adımdaki `ody_...` token | ✅ |
| `TELEGRAM_ODYSSEUS_MODEL` | Varsayılan `gpt-5.6-sol`. Değiştirmek istersen başka bir model adı | – |
| `TELEGRAM_ALLOWED_CHAT_IDS` | Kendi chat ID'n. Boş bırakırsan eşleştirme kullanılır (4. adım) | – |
| `TELEGRAM_REMINDER_SECRET` | Hatırlatma webhook'u için rastgele bir gizli değer (5. adım) | – |
| `BOT_TIMEZONE` | Varsayılan `Europe/Istanbul` | – |

`TELEGRAM_REMINDER_SECRET` için şu komutla bir değer üretebilirsin:

```bash
openssl rand -hex 32
```

Kaydet ve **Redeploy** et. Bu iki zorunlu değişken boşken servis sessizce
bekler ve uygulamanın geri kalanını etkilemez.

### 4. Botu kendi hesabınla eşleştir

`TELEGRAM_ALLOWED_CHAT_IDS` boşsa bot **eşleştirme modunda** açılır.

1. Coolify'da `telegram-bot` servisinin loglarını aç. Şöyle bir satır görürsün:
   ```
   PAIRING MODE: send "/eslestir A1B2C3D4" to the bot from your Telegram account
   ```
2. Telegram'da botuna özel mesajla `/eslestir A1B2C3D4` yaz.

Eşleşme kalıcıdır (`telegram-bot-data` volume'unda saklanır) ve ilk başarılı
eşleşmeden sonra eşleştirme kapanır. 10 yanlış denemeden sonra da yeniden
başlatılana kadar kapalı kalır. Botun kim olduğunu bilmeyen biri sana yazarsa
yalnızca "eşleştirilmedi" yanıtını alır.

Kodla uğraşmak istemezsen `/kimim` yazıp chat ID'ni öğren,
`TELEGRAM_ALLOWED_CHAT_IDS` değişkenine gir ve redeploy et.

### 5. (İsteğe bağlı) Hatırlatmaları Telegram'a gönder

Odysseus'ta iki ayar yapılacak.

**Entegrasyonu ekle:** **Settings → Integrations → Add Integration → API**

| Alan | Değer |
|---|---|
| Preset | `Custom (no preset)` |
| Name | `Telegram Bot` |
| Base URL | `http://telegram-bot:8088/reminder` |
| Auth | Gizli değer belirlediysen `Bearer` ve **API Key** alanına `TELEGRAM_REMINDER_SECRET`; belirlemediysen `None` |

**Test** ile kontrol edip **Save** ile kaydet.

**Hatırlatma kanalını ayarla:** **Settings → Reminders** ("How you're reminded")

| Alan | Değer |
|---|---|
| Channel | `Webhook` |
| Integration | `Telegram Bot` |
| Payload | `{"title": "{{title}}", "message": "{{message}}"}` |

Üstteki **Test** butonuna basınca Telegram'a bir deneme bildirimi gelmeli.

`http://telegram-bot:8088` sadece compose ağı içinden erişilebilir. İnternete
açık bir port veya domain yoktur.

## Kullanım

Bota normal yazman yeterli:

- "Yarın 09:30'da faturayı ödemeyi hatırlat"
- "Perşembe 14:00-15:00 arası takvime Ahmet ile toplantı ekle"
- "Bu hafta takvimimde ne var?"
- "Az önce eklediğin hatırlatmayı sil"
- "Kahveyi şekersiz içtiğimi hatırla"

Komutlar:

| Komut | Ne yapar |
|---|---|
| `/yeni` | Sohbet bağlamını sıfırlar |
| `/yapilacaklar` | Hatırlatmaları ve yapılacakları listeler |
| `/takvim [gün]` | Önümüzdeki günlerin etkinliklerini gösterir (varsayılan 7) |
| `/hafiza` | Hafıza notlarını gösterir |
| `/durum` | Bağlantıyı, modeli ve token yetkilerini gösterir |
| `/kimim` | Telegram chat ID'ni gösterir |

## Nasıl çalışır?

- Bot Telegram'ı long polling ile dinler, bu yüzden webhook, domain veya açık
  port gerekmez.
- Her mesaj için `POST /api/v1/chat` ile **tek seferlik** bir Odysseus oturumu
  açılır ve yanıt alınınca oturum silinir. Bu yüzden Odysseus arayüzünde
  Telegram oturumları birikmez.
- Son 12 tur (en fazla ~14.000 karakter) bot tarafında, `telegram-bot-data`
  volume'unda tutulur. Bunu `BOT_HISTORY_TURNS` ve `BOT_HISTORY_CHARS` ile
  değiştirebilirsin.
- Model işlem isterse yanıtına bir ` ```odysseus ` JSON bloğu ekler. Bot bu
  işlemleri `/api/codex/*` üzerinden token'ın yetkileriyle çalıştırır, sonucu
  sana gösterir ve bir sonraki turda modele bildirir.

Neden oturumlar devam ettirilmiyor? `/api/v1/chat` var olan bir oturuma devam
ederken ChatGPT Subscription gibi OAuth tabanlı sağlayıcıların kimlik bilgisini
yeniden çözmüyor; ikinci mesaj 401 alıyor. Ayrıntı için `COOLIFY.md` →
"Bilinen sorunlar".

## Sınırlar

- Sadece metin mesajları işlenir; ses, fotoğraf ve dosya desteklenmez.
- Web araması, e-posta ve doküman işlemleri bot üzerinden yapılamaz.
- Model adı sabitlenmelidir. ChatGPT Subscription'daki `gpt-6*` modelleri şu an
  `/api/v1/chat` üzerinde `Unsupported parameter: temperature` hatası veriyor.

## Geliştirme

```bash
python -m pytest integrations/telegram -q
```

Testler ağ kullanmaz; Telegram ve Odysseus sahte istemcilerle simüle edilir.
