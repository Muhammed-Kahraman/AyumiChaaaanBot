# 🤖 AyumuChanBot — Telegram Media Downloader

Grupta paylaşılan **Instagram** ve **X (Twitter)** linklerindeki video ve resimleri otomatik olarak indirip gruba gönderen Telegram botu.

## ✨ Özellikler

- 📸 Instagram post/reel/video linklerini algılama ve indirme
- 🐦 X (Twitter) tweet/video linklerini algılama ve indirme
- 🎬 **Video**: İndir ve orijinal mesaja reply olarak gönder
- 🖼️ **Resim**: İlk 2 resmi albüm (slide) olarak gönder
- 💬 Tüm medyalar orijinal mesaja **reply** olarak gönderilir
- 🤖 Video, tek resim ve carousel postlarında otomatik anime spoiler analizi
- 🙈 Spoiler bulunan her medya Telegram spoiler perdesiyle gönderilir

## 🚀 Kurulum

### 1. Gereksinimler

- Python 3.10+
- ffmpeg (video işleme için)

```bash
# macOS
brew install ffmpeg

# Ubuntu/Debian
sudo apt install ffmpeg
```

### 2. Bot Token Alma

1. Telegram'da [@BotFather](https://t.me/BotFather)'a gidin
2. `/newbot` komutu gönderin
3. Bot adını ve kullanıcı adını belirleyin
4. Size verilen **token**'ı kopyalayın

### 3. Kurulum

```bash
cd AyumuChanBot

# Bağımlılıkları kur
pip install -r requirements.txt

# Ortam değişkenlerini ayarla
cp .env.example .env
# .env dosyasını düzenleyin ve token'ınızı girin
```

Groq anahtarını ekledikten sonra ücretsiz kota ile spoiler analizini açın:

```env
AI_PROVIDER=groq
GROQ_API_KEY=...
GROQ_TRANSCRIPTION_MODEL=whisper-large-v3-turbo
GROQ_VISION_MODEL=qwen/qwen3.6-27b
AI_SPOILER_ENABLED=true
SPOILER_THRESHOLD=3
AI_FAILURE_POLICY=spoiler
```

Video analizinde FFmpeg ile sınırlı sayıda kare ve sıkıştırılmış ses
çıkarılır. Ses yazıya dökülür; transcript ve kareler birlikte
değerlendirilir. Resim postlarında görsel, karakterler ve gömülü
metin/altyazı analiz edilir. AI kullanılamazsa medya gönderimi durmaz;
`AI_FAILURE_POLICY` uygulanır.

Prod öncesi gerçek indirme + analiz akışını Telegram'a göndermeden
bir Instagram/X linkiyle test etmek için:

```bash
python3 check_spoiler.py 'https://www.instagram.com/reel/.../'
```

### 4. Grup Ayarları

1. Botu grubunuza ekleyin
2. Bota **grup mesajlarını okuma** izni verin:
   - `@BotFather` → `/mybots` → Botunuz → **Bot Settings** → **Group Privacy** → **Turn off**

### 5. Çalıştırma

```bash
python bot.py
```

## 📋 Kullanım

Bot gruba eklendikten sonra:

1. Gruba bir **Instagram** veya **X** linki gönderin
2. Bot otomatik olarak medyayı indirir
3. Video veya resim(ler) orijinal mesajınıza **reply** olarak gönderilir

### Desteklenen Linkler

| Platform | Link Türleri |
|----------|-------------|
| Instagram | `/p/`, `/reel/`, `/reels/`, `/tv/` |
| X (Twitter) | `/status/` (tweet, video, fotoğraf) |

### Limitler

- Max dosya boyutu: **50 MB** (Telegram limiti)
- Max resim sayısı: **2** (birden fazla resimli postlarda)

## 🛠️ Yapılandırma

`config.py` dosyasından ayarları değiştirebilirsiniz:

```python
MAX_IMAGES = 10                   # Gönderilecek max resim sayısı
MAX_FILE_SIZE = 50 * 1024 * 1024  # 50 MB (Telegram limiti)
```

### 🔑 Instagram Oturum Ayarı (Zorunlu)
Instagram, giriş yapılmayan anonim istekleri engellediği için (403 Forbidden), botun Instagram indirmeleri yapabilmesi adına `.env` dosyasına bir `sessionid` eklemeniz gereklidir:

1. Tarayıcınızdan (Chrome/Safari) [instagram.com](https://www.instagram.com)'a gidin ve hesabınıza giriş yapın.
2. Geliştirici Araçlarını açın (F12 veya Sağ Tık -> İncele).
3. **Application** (Chrome) veya **Storage** (Safari) sekmesine geçin.
4. Soldaki menüden **Cookies** altında `https://www.instagram.com` dizinini seçin.
5. `sessionid` adlı çerezi bulun ve değerini (value) kopyalayın.
6. `.env` dosyanızı açıp şu şekilde ekleyin:

```env
IG_SESSIONID=kopyaladiginiz_uzun_cookie_degeri_buraya
```


# Servisi durdur
launchctl unload ~/Library/LaunchAgents/com.ayumuchan.bot.plist
# Servisi başlat
launchctl load ~/Library/LaunchAgents/com.ayumuchan.bot.plist
# Logları izle
tail -f ~/Desktop/AyumuChanBot/bot.log
