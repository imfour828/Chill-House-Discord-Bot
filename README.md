# 🤖 Discord Music Bot — CHILL HOUSE

Discord Bot viết bằng **Python + discord.py**, tập trung vào phát nhạc YouTube trong Voice Channel, Music Player bằng Embed + Button, TTS tiếng Việt, tự động kết nối lại Voice và một số tiện ích quản lý bot.

---

## ✨ Tính năng

- 🎵 Phát nhạc từ YouTube bằng tên bài hoặc URL.
- 📋 Hàng chờ nhạc.
- ⏯️ Pause / Resume.
- ⏭️ Skip.
- 🔁 Lặp bài hiện tại.
- ⏱️ Tua đến vị trí bất kỳ.
- ⏹️ Dừng Music Player.
- 🎛️ Điều khiển trực tiếp bằng Button trên Music Player.
- 🎤 `!say` đọc tiếng Việt bằng Google TTS.
- 🔊 `!join` / `!leave` quản lý Voice Channel.
- 🔄 Tự động kết nối lại Voice Channel khi bot bị mất kết nối.
- 💾 Lưu trạng thái Voice/Music để hỗ trợ khôi phục.
- 💬 Tự động trả lời DM gửi trực tiếp cho bot.
- ❤️ Health endpoint `/health`.
- 📝 Logging để theo dõi lỗi và hoạt động của bot.

---

# 📦 Cài đặt

## 1. Cài Python

Khuyến nghị dùng **Python 3.10+**.

Kiểm tra:

```bash
python --version
```

---

## 2. Cài thư viện

Tạo file `requirements.txt`:

```txt
discord.py[voice]>=2.5,<3.0
gTTS>=2.5,<3.0
yt-dlp>=2025.1,<2027.0
PyNaCl>=1.5,<2.0
```

Sau đó chạy:

```bash
pip install -r requirements.txt
```

---

# 🎧 FFmpeg

Bot cần **FFmpeg** để phát audio trong Discord.

Kiểm tra:

```bash
ffmpeg -version
```

Nếu máy chủ/hosting chưa có FFmpeg, hãy cài FFmpeg trước khi chạy bot.

---

# 🤖 Tạo Discord Bot

Tạo Bot tại Discord Developer Portal.

Bot cần bật:

- **Message Content Intent**

Các quyền Discord tối thiểu nên có:

- View Channel
- Send Messages
- Embed Links
- Add Reactions
- Read Message History
- Connect
- Speak
- Use Voice Activity

Bot cũng cần quyền phù hợp để xóa tin nhắn nếu muốn sử dụng cơ chế tự dọn command của server.

---

# 🔐 Biến môi trường

Bot yêu cầu:

```env
DISCORD_TOKEN=TOKEN_CUA_BOT
BOT_OWNER_ID=ID_DISCORD_CUA_BAN
```

### `DISCORD_TOKEN`

Token của Discord Bot.

### `BOT_OWNER_ID`

Discord User ID của chủ bot.

Có thể thêm:

```env
PORT=5000
LOG_LEVEL=INFO
```

Nếu môi trường có Deno hoặc Node.js, bot có thể sử dụng runtime đó cho yt-dlp:

```env
DENO_PATH=/path/to/deno
```

hoặc:

```env
NODE_PATH=/path/to/node
```

---

# ▶️ Chạy Bot

Nếu file chính là `bot_progress.py`:

```bash
python bot_progress.py
```

Nếu đổi tên file:

```bash
python bot.py
```

---

# 🎮 Danh sách lệnh

Prefix hiện tại:

```text
!
```

---

## 🎵 Music

### `!play`

Phát bài hát hoặc thêm bài vào hàng chờ.

```text
!play <tên bài hát>
```

Ví dụ:

```text
!play Có Không Giữ Mất Đừng Tìm
```

Có thể dùng URL YouTube:

```text
!play https://youtube.com/...
```

Alias:

```text
!p
```

---

### `!pause`

Tạm dừng bài hát:

```text
!pause
```

---

### `!resume`

Tiếp tục bài hát:

```text
!resume
```

Alias:

```text
!unpause
```

---

### `!skip`

Bỏ qua bài hiện tại:

```text
!skip
```

Alias:

```text
!s
```

---

### `!stop`

Dừng Music Player và xóa hàng chờ:

```text
!stop
```

Quyền dừng:

- Administrator / Manage Server: dừng trực tiếp.
- Người dùng thông thường: sử dụng cơ chế vote Stop của Music Player.

---

### `!queue`

Xem danh sách bài đang chờ:

```text
!queue
```

Alias:

```text
!q
```

---

# 🎛️ Music Player Buttons

Music Player có các nút:

| Nút | Chức năng |
|---|---|
| ⏯️ | Pause / Resume |
| ⏭️ | Skip bài hiện tại |
| 🔁 | Bật / tắt Loop |
| ⏱️ Tua đến | Tua đến thời gian mong muốn |
| ⏹️ | Dừng Music Player |

### Tua nhạc

Có thể nhập:

```text
30
```

Tua đến giây thứ 30.

Hoặc:

```text
5:30
```

Tua đến 5 phút 30 giây.

Hoặc:

```text
1:23:45
```

Tua đến 1 giờ 23 phút 45 giây.

---

# 🎤 TTS — `!say`

Dùng:

```text
!say <nội dung>
```

Ví dụ:

```text
!say Xin chào mọi người
```

Bot sẽ đọc theo dạng:

```text
<Tên hiển thị> vừa nói <nội dung>
```

Ví dụ người dùng tên **Bé Tứ** nhập:

```text
!say hello mọi người
```

Bot sẽ đọc:

```text
Bé Tứ vừa nói hello mọi người
```

### Khi `!say` thành công

- Bot phát giọng nói trong Voice Channel hiện tại.
- Không gửi thêm tin nhắn thông báo.
- React `✅` vào tin nhắn `!say`.

### Khi `!say` thất bại

Bot react:

```text
❌
```

### Khi Bot đang phát nhạc

Không sử dụng `!say` trong lúc Music Player đang phát hoặc pause nhạc.

Bot sẽ thông báo:

```text
❌ Bot đang phát nhạc! Vui lòng không dùng `!say` lúc này để tránh làm gián đoạn bài hát.
```

Thông báo này được tự động xóa sau **5 giây**.

> Lý do: Discord Voice Client chỉ có một audio source tại một thời điểm. TTS không nên tự ý ghi đè audio của Music Player.

---

# 🔊 Voice

## `!join`

Cho bot vào Voice Channel mà người dùng đang ở:

```text
!join
```

Người dùng phải ở trong Voice Channel trước.

Bot sẽ lưu Voice Channel mục tiêu để có thể thử kết nối lại khi mất kết nối.

---

## `!leave`

Cho bot rời Voice Channel:

```text
!leave
```

Lệnh này cũng dọn Music Player của server.

---

# 📊 `!status`

Đổi trạng thái Discord của bot:

```text
!status <status> [hoạt động]
```

Các trạng thái:

```text
online
idle
dnd
invisible
```

Ví dụ:

```text
!status online
```

Hoặc:

```text
!status dnd Đang chill 🎵
```

Bot sẽ đổi Discord Presence tương ứng.

---

# 💬 DM Bot

Khi người dùng nhắn tin trực tiếp cho Bot qua DM, Bot sẽ tự động gửi thông báo rằng đây là bot tự động và hướng người dùng liên hệ **Bé Tứ** nếu cần hỗ trợ.

Bot không xử lý các vấn đề cá nhân thông qua DM.

---

# 🔄 Tự động kết nối lại Voice

Bot có cơ chế lưu Voice Channel mục tiêu.

Khi bot bị mất kết nối Voice, bot có thể thử kết nối lại vào Voice Channel đã lưu.

Các trạng thái liên quan được lưu trong:

```text
voice_targets.json
```

---

# 💾 Music State

Music Player có hỗ trợ lưu trạng thái vào:

```text
music_state.json
```

Mục đích là hỗ trợ khôi phục queue/trạng thái Music Player khi tiến trình bot gặp sự cố hoặc khởi động lại.

---

# 🌐 Health Check

Bot có HTTP health endpoint:

```text
/health
```

Dùng để kiểm tra bot/service có đang hoạt động hay không.

Điều này hữu ích khi chạy bot trên hosting có yêu cầu web service hoặc health check.

---

# ▶️ YouTube / yt-dlp

Bot sử dụng:

```text
yt-dlp
```

để tìm và lấy audio từ YouTube.

Một số môi trường hiện tại có thể yêu cầu JavaScript runtime như:

- Deno
- Node.js

Bot tự kiểm tra runtime khả dụng thông qua:

```text
DENO_PATH
NODE_PATH
```

Nếu YouTube trả về lỗi `403`, nguyên nhân có thể liên quan đến:

- IP của máy chủ/hosting.
- YouTube thay đổi cơ chế stream.
- yt-dlp chưa được cập nhật.
- Thiếu JavaScript runtime.
- Stream URL đã hết hạn và cần được refresh.

---

# 🛠️ Cập nhật yt-dlp

Khi YouTube thay đổi hệ thống, nên cập nhật:

```bash
pip install -U yt-dlp
```

Sau đó restart bot.

---

# 🧩 Cấu trúc chính

Các thành phần chính của bot gồm:

```text
bot_progress.py
requirements.txt
README.md
voice_targets.json
music_state.json
```

Một số file JSON có thể được tạo tự động khi bot chạy.

---

# ⚠️ Lưu ý

## Discord Token

Không chia sẻ:

```text
DISCORD_TOKEN
```

Nếu token bị lộ, hãy reset token ngay trong Discord Developer Portal.

---

## Bot không nghe được Voice

Kiểm tra:

1. Bot có quyền `Connect`.
2. Bot có quyền `Speak`.
3. FFmpeg đã được cài.
4. PyNaCl đã được cài.
5. Bot có thực sự kết nối Voice Channel.
6. User đang ở đúng Voice Channel khi sử dụng lệnh Music.

---

## Không phát được YouTube

Thử:

```bash
pip install -U yt-dlp
```

Kiểm tra FFmpeg:

```bash
ffmpeg -version
```

Nếu vẫn lỗi, kiểm tra log của bot để xác định lỗi YouTube/yt-dlp.

---

# ❤️ Credits

**Discord Music Bot — CHILL HOUSE**

Phát triển bởi:

**Bé Tứ**

Bot được xây dựng nhằm hỗ trợ cộng đồng Discord với các tính năng:

- 🎵 Music Player
- 🎤 Vietnamese TTS
- 🔊 Voice
- 🎛️ Music Controls
- 🔄 Auto Reconnect
- 💾 Music State
- 🤖 Automation

---

## 📜 Tóm tắt lệnh

```text
!play <tên/URL>       → Phát nhạc
!p <tên/URL>          → Alias !play

!pause                → Tạm dừng
!resume               → Tiếp tục
!unpause              → Alias !resume

!skip                 → Skip
!s                    → Alias !skip

!stop                 → Dừng Music Player

!queue                → Xem hàng chờ
!q                    → Alias !queue

!say <nội dung>       → Bot đọc tiếng Việt

!join                 → Bot vào Voice
!leave                → Bot rời Voice

!status <trạng thái>  → Đổi trạng thái Bot
```

**CHILL HOUSE 🎧 — Music • Voice • Community**
