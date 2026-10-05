# -*- coding: utf-8 -*-
"""Vietnamese Discord bot with Prefix/Slash Music Player, DM Interactive Logs, Voice TTS, and Health Server."""

from __future__ import annotations

import asyncio
import ctypes.util
import json
import logging
import os
import shutil
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

import discord
from discord.ext import commands
from gtts import gTTS
import yt_dlp


logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("discord_bot")

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
BOT_OWNER_ID_VALUE = os.getenv("BOT_OWNER_ID", "").strip()

if not DISCORD_TOKEN:
    raise RuntimeError("Thiếu DISCORD_TOKEN. Hãy thêm token trong Replit Secrets.")
if not BOT_OWNER_ID_VALUE.isdigit():
    raise RuntimeError("BOT_OWNER_ID chưa hợp lệ. Hãy đặt biến này bằng ID Discord dạng số của bạn.")

BOT_OWNER_ID = int(BOT_OWNER_ID_VALUE)
MAX_CHANNEL_MESSAGE_LENGTH = 1900
MAX_TTS_LENGTH = 500
VOICE_RECONNECT_INTERVAL = 15
VOICE_TARGETS_FILE = Path(__file__).with_name("voice_targets.json")
MUSIC_STATE_FILE = Path(__file__).with_name("music_state.json")
MUSIC_STATE_INTERVAL = 5

# YouTube/yt-dlp 2026+: enable the JS challenge solver when a runtime exists.
_DENO_PATH = os.getenv("DENO_PATH") or shutil.which("deno")
_NODE_PATH = os.getenv("NODE_PATH") or shutil.which("node")
_YTDL_JS_RUNTIMES: dict[str, dict[str, str | None]] = {}
if _DENO_PATH:
    _YTDL_JS_RUNTIMES["deno"] = {"path": _DENO_PATH}
elif _NODE_PATH:
    _YTDL_JS_RUNTIMES["node"] = {"path": _NODE_PATH}

YTDL_OPTIONS = {
    "format": "bestaudio[ext=m4a]/bestaudio[acodec!=none]/best",
    "noplaylist": True,
    "nocheckcertificate": True,
    "ignoreerrors": False,
    "logtostderr": False,
    "quiet": True,
    "no_warnings": True,
    "default_search": "ytsearch",
    "source_address": "0.0.0.0",
    "retries": 3,
    "fragment_retries": 3,
    "extractor_retries": 3,
    "socket_timeout": 20,
    "js_runtimes": _YTDL_JS_RUNTIMES,
    "remote_components": {"ejs:github"},
    # Tránh client android_vr đang gây 403 trên nhiều môi trường hiện tại.
    # yt-dlp đã ghi nhận android_vr bị 403; client android là fallback thực tế.
    "extractor_args": {
        "youtube": {
            "player_client": ["android"],
        },
    },
    "http_headers": {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36",
        "Accept-Language": "vi-VN,vi;q=0.9,en-US;q=0.8,en;q=0.7",
    },
}

FFMPEG_OPTIONS = {
    "before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
    "options": "-vn",
}

ytdl = yt_dlp.YoutubeDL(YTDL_OPTIONS)


class YTDLSource(discord.PCMVolumeTransformer):
    def __init__(self, source: discord.FFmpegPCMAudio, *, data: dict[str, Any], requester: discord.Member | discord.User, volume: float = 0.5):
        super().__init__(source, volume)
        self.data = data
        self.title = data.get("title", "Không rõ tiêu đề")
        self.url = data.get("url", "")
        self.webpage_url = data.get("webpage_url", "")
        self.duration = data.get("duration", 0)
        self.thumbnail = data.get("thumbnail")
        self.requester = requester
        self.started_at: float | None = None
        self.paused_at: int = 0
        self.resume_position: int = 0
        self.resume_paused: bool = False

        if self.duration:
            mins, secs = divmod(self.duration, 60)
            self.duration_str = f"{mins}:{secs:02d}"
        else:
            self.duration_str = "N/A"

    @classmethod
    async def from_url(
        cls,
        url: str,
        requester: discord.Member | discord.User,
        *,
        loop: asyncio.AbstractEventLoop | None = None,
        start_time: int = 0,
    ) -> YTDLSource:
        loop = loop or asyncio.get_event_loop()
        data = await loop.run_in_executor(None, lambda: ytdl.extract_info(url, download=False))
        if not data:
            raise RuntimeError("Không lấy được dữ liệu âm thanh.")
        if "entries" in data:
            entries = data.get("entries") or []
            if not entries:
                raise RuntimeError("Không tìm thấy bài hát.")
            data = entries[0]
        filename = data.get("url")
        if not filename:
            raise RuntimeError("Nguồn âm thanh không hợp lệ.")
        ffmpeg_options = dict(FFMPEG_OPTIONS)
        if start_time > 0:
            ffmpeg_options["before_options"] = f"-ss {int(start_time)} " + ffmpeg_options["before_options"]
        return cls(discord.FFmpegPCMAudio(filename, **ffmpeg_options), data=data, requester=requester)


async def refresh_source(track: YTDLSource, *, loop: asyncio.AbstractEventLoop) -> YTDLSource:
    """Tạo stream URL mới ngay trước khi phát, có thể tiếp tục từ vị trí đã lưu."""
    source_url = track.webpage_url or track.url or track.title
    start_time = max(0, int(getattr(track, "resume_position", 0)))
    new_track = await YTDLSource.from_url(
        source_url, track.requester, loop=loop, start_time=start_time
    )
    new_track.resume_position = start_time
    new_track.resume_paused = bool(getattr(track, "resume_paused", False))
    return new_track


def build_intents() -> discord.Intents:
    intents = discord.Intents.default()
    intents.message_content = True
    return intents


async def is_bot_owner(interaction: discord.Interaction) -> bool:
    return interaction.user.id == BOT_OWNER_ID


async def respond_privately(interaction: discord.Interaction, content: str) -> None:
    if interaction.response.is_done():
        await interaction.followup.send(content, ephemeral=True)
    else:
        await interaction.response.send_message(content, ephemeral=True)


def remove_temp_audio(path: str) -> None:
    try:
        Path(path).unlink(missing_ok=True)
    except OSError:
        logger.warning("Không thể xóa tệp âm thanh tạm: %s", path)


def load_opus_library() -> None:
    if discord.opus.is_loaded():
        return

    candidates: list[str] = []
    detected_library = ctypes.util.find_library("opus")
    if detected_library:
        candidates.append(detected_library)

    for linker_flag in os.getenv("NIX_LDFLAGS", "").split():
        if not linker_flag.startswith("-L") or len(linker_flag) <= 2:
            continue
        library_directory = Path(linker_flag[2:])
        for library_name in ("libopus.so.0", "libopus.so"):
            library_path = library_directory / library_name
            if library_path.is_file() and str(library_path) not in candidates:
                candidates.append(str(library_path))

    load_errors: list[str] = []
    for candidate in candidates:
        try:
            discord.opus.load_opus(candidate)
            logger.info("Đã nạp thư viện Opus: %s", candidate)
            return
        except OSError as error:
            load_errors.append(f"{candidate}: {error}")

    details = "; ".join(load_errors) or "không tìm thấy libopus trong đường dẫn hệ thống"
    raise RuntimeError(f"Không thể nạp thư viện Opus cần cho voice: {details}")


def load_voice_targets() -> dict[int, int]:
    try:
        saved_targets = json.loads(VOICE_TARGETS_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError):
        logger.exception("Không thể đọc danh sách voice channel đã lưu.")
        return {}

    if not isinstance(saved_targets, dict):
        logger.warning("Tệp voice channel đã lưu không đúng định dạng.")
        return {}

    targets: dict[int, int] = {}
    for guild_id, channel_id in saved_targets.items():
        if (
            isinstance(guild_id, str)
            and guild_id.isdigit()
            and isinstance(channel_id, int)
            and not isinstance(channel_id, bool)
            and channel_id > 0
        ):
            targets[int(guild_id)] = channel_id
    return targets


def save_voice_targets(targets: dict[int, int]) -> None:
    temporary_file = VOICE_TARGETS_FILE.with_suffix(".tmp")
    serialized = {str(guild_id): channel_id for guild_id, channel_id in targets.items()}
    try:
        temporary_file.write_text(
            json.dumps(serialized, indent=2),
            encoding="utf-8",
        )
        temporary_file.replace(VOICE_TARGETS_FILE)
    except OSError:
        logger.exception("Không thể lưu voice channel đã chọn.")


def _track_state(track: YTDLSource, *, position: int = 0, paused: bool = False) -> dict[str, Any]:
    return {
        "title": track.title,
        "webpage_url": track.webpage_url or track.url,
        "duration": int(track.duration or 0),
        "thumbnail": track.thumbnail,
        "requester_id": int(getattr(track.requester, "id", 0) or 0),
        "position": max(0, int(position)),
        "paused": bool(paused),
    }


def load_music_states() -> dict[int, dict[str, Any]]:
    try:
        raw = json.loads(MUSIC_STATE_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError):
        logger.exception("Không thể đọc trạng thái Music Player đã lưu.")
        return {}
    if not isinstance(raw, dict):
        logger.warning("Tệp music state không đúng định dạng.")
        return {}
    states: dict[int, dict[str, Any]] = {}
    for guild_id, state in raw.items():
        if isinstance(guild_id, str) and guild_id.isdigit() and isinstance(state, dict):
            states[int(guild_id)] = state
    return states


def save_music_states(states: dict[int, dict[str, Any]]) -> None:
    temporary_file = MUSIC_STATE_FILE.with_suffix(".tmp")
    try:
        temporary_file.write_text(
            json.dumps(states, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary_file.replace(MUSIC_STATE_FILE)
    except OSError:
        logger.exception("Không thể lưu trạng thái Music Player.")


def delete_music_state(guild_id: int) -> None:
    states = load_music_states()
    if guild_id in states:
        states.pop(guild_id, None)
        save_music_states(states)


async def create_vietnamese_speech_file(text: str) -> str:
    audio_file = tempfile.NamedTemporaryFile(suffix=".mp3", delete=False)
    audio_path = audio_file.name
    audio_file.close()

    try:
        speech = gTTS(text=text[:MAX_TTS_LENGTH], lang="vi")
        await asyncio.to_thread(speech.save, audio_path)
    except Exception:
        remove_temp_audio(audio_path)
        raise

    return audio_path


async def text_to_speech(text: str, voice_client: discord.VoiceClient) -> bool:
    if not voice_client.is_connected():
        return False

    audio_path: str | None = None
    audio_source: discord.AudioSource | None = None
    try:
        audio_path = await create_vietnamese_speech_file(text)

        def after_playback(error: Exception | None) -> None:
            if error:
                logger.warning("Lỗi phát TTS trong voice: %s", error)
            remove_temp_audio(audio_path)

        audio_source = discord.FFmpegPCMAudio(audio_path)
        voice_client.play(audio_source, after=after_playback)
        return True
    except Exception:
        if audio_source is not None:
            audio_source.cleanup()
        if audio_path is not None:
            remove_temp_audio(audio_path)
        logger.exception("Không thể tạo hoặc phát giọng nói TTS.")
        return False


# --- NÂNG CẤP HỆ THỐNG MUSIC PLAYER KHÔNG LAG ---

class MusicPlayerView(discord.ui.View):
    """Bảng điều khiển Music Player.

    Mọi người đang ở cùng voice channel với bot đều có thể dùng các nút.
    Stop là ngoại lệ: 2 người vote hoặc Admin/Mod dừng ngay.
    """
    def __init__(self, player: "GuildMusicPlayer") -> None:
        super().__init__(timeout=None)
        self.player = player
        vc = player.guild.voice_client
        if vc and vc.is_paused():
            self.pause_resume.emoji = "▶️"
        else:
            self.pause_resume.emoji = "⏯️"

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        vc = interaction.guild.voice_client if interaction.guild else None
        if not interaction.user.voice or not interaction.user.voice.channel:
            await interaction.response.send_message(
                "❌ Bạn phải ở trong phòng voice để dùng nút!", ephemeral=True
            )
            return False
        if vc and interaction.user.voice.channel != vc.channel:
            await interaction.response.send_message(
                f"❌ Bạn phải ở cùng phòng voice với bot ({vc.channel.mention})!",
                ephemeral=True,
            )
            return False
        return True

    @discord.ui.button(emoji="⏯️", style=discord.ButtonStyle.secondary, row=0)
    async def pause_resume(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.defer(ephemeral=True)
        vc = interaction.guild.voice_client if interaction.guild else None
        if not vc:
            return await interaction.followup.send("❌ Bot không ở trong kênh voice!", ephemeral=True)
        if not self.player.current or not (vc.is_playing() or vc.is_paused()):
            return await interaction.followup.send("❌ Không có bài đang phát!", ephemeral=True)

        if vc.is_paused():
            text = f"{interaction.user.display_name} đã tiếp tục phát."
            ok = await self.player.announce_music_action(text, action="resume", final_paused=False)
        else:
            text = f"{interaction.user.display_name} đã tạm dừng phát."
            ok = await self.player.announce_music_action(text, action="pause", final_paused=True)

        if not ok:
            # Nếu Google TTS lỗi, vẫn thực hiện thao tác nhạc bình thường.
            if vc.is_paused():
                vc.resume()
                if self.player.current:
                    paused_at = getattr(self.player.current, "paused_at", 0)
                    self.player.current.started_at = asyncio.get_running_loop().time() - paused_at
                    self.player.current.paused_at = 0
            elif vc.is_playing():
                if self.player.current:
                    self.player.current.paused_at = max(
                        0,
                        int(asyncio.get_running_loop().time() - getattr(
                            self.player.current, "started_at", asyncio.get_running_loop().time()
                        )),
                    )
                vc.pause()
            self.player.save_state()
            await self.player.recreate_embed()

    @discord.ui.button(emoji="⏭️", style=discord.ButtonStyle.primary, row=0)
    async def skip(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.defer(ephemeral=True)
        vc = interaction.guild.voice_client if interaction.guild else None
        if not vc or not (vc.is_playing() or vc.is_paused()):
            return await interaction.followup.send("❌ Không có bài hát nào đang phát!", ephemeral=True)

        current = self.player.current
        title = current.title if current else "bài hát hiện tại"
        if interaction.channel:
            try:
                await interaction.channel.send(
                    f"⏭️ **{interaction.user.display_name}** đã skip **{title}**.",
                    delete_after=4,
                )
            except discord.HTTPException:
                pass

        text = f"{interaction.user.display_name} đã skip bài hát."
        ok = await self.player.announce_music_action(text, action="skip")
        if not ok and (vc.is_playing() or vc.is_paused()):
            vc.stop()

    @discord.ui.button(emoji="🔁", style=discord.ButtonStyle.success, row=0)
    async def toggle_loop(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.defer(ephemeral=True)
        modes = [False, "song", "queue"]
        self.player.loop = modes[(modes.index(self.player.loop) + 1) % len(modes)]
        self.player.save_state()
        await self.player.recreate_embed()

    @discord.ui.button(emoji="⏱️", style=discord.ButtonStyle.secondary, row=0)
    async def seek_to_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        """Mở ô nhập thời gian để tua đến vị trí mong muốn."""
        if not self.player.current:
            return await interaction.response.send_message(
                "❌ Không có bài hát nào đang phát!", ephemeral=True
            )
        await interaction.response.send_modal(SeekModal(self.player))

    @discord.ui.button(emoji="⏹️", style=discord.ButtonStyle.danger, row=0)
    async def stop(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.defer(ephemeral=True)
        if not self.player.current:
            return await interaction.followup.send("❌ Không có nhạc đang phát!", ephemeral=True)

        is_mod = isinstance(interaction.user, discord.Member) and (
            interaction.user.guild_permissions.administrator
            or interaction.user.guild_permissions.manage_guild
        )
        if is_mod:
            await self.player.stop_now()
            return

        await self.player.start_stop_vote(interaction.user, interaction.channel)

class SeekModal(discord.ui.Modal, title="⏱️ Tua đến thời gian"):
    timestamp = discord.ui.TextInput(
        label="Nhập thời gian",
        placeholder="30 • 5:30 • 1:23:45",
        required=True,
        max_length=8,
    )

    def __init__(self, player: "GuildMusicPlayer") -> None:
        super().__init__()
        self.player = player

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            value = self.timestamp.value.strip()
            parts = [int(x) for x in value.split(":")]
            if len(parts) == 1:
                seconds = parts[0]
            elif len(parts) == 2 and 0 <= parts[1] < 60:
                seconds = parts[0] * 60 + parts[1]
            elif len(parts) == 3 and 0 <= parts[1] < 60 and 0 <= parts[2] < 60:
                seconds = parts[0] * 3600 + parts[1] * 60 + parts[2]
            else:
                raise ValueError
            if seconds < 0:
                raise ValueError
        except (ValueError, TypeError):
            return await interaction.response.send_message(
                "❌ Thời gian không hợp lệ. Dùng `30`, `5:30` hoặc `1:23:45`.",
                ephemeral=True,
            )
        await interaction.response.defer(ephemeral=True)
        position_text = self.player.format_voice_time(seconds)
        text = f"{interaction.user.display_name} đã tua bài hát đến {position_text}."
        ok = await self.player.announce_music_action(
            text,
            action="seek",
            target_seconds=seconds,
            final_paused=bool(self.player.guild.voice_client and self.player.guild.voice_client.is_paused()),
        )
        if not ok:
            await self.player.seek_to_seconds(seconds)


class GuildMusicPlayer:
    def __init__(self, bot: "VietnameseDiscordBot", guild: discord.Guild, chat_channel: discord.abc.Messageable) -> None:
        self.bot = bot
        self.guild = guild
        # Music Player luôn được gửi vào chat của Voice Channel nơi bot đang ở.
        self.channel = chat_channel
        self.queue: list[YTDLSource] = []
        self.queue_lock = asyncio.Lock()
        self.render_lock = asyncio.Lock()
        self.next_event = asyncio.Event()
        self.queue_event = asyncio.Event()
        self.current: YTDLSource | None = None
        self.last_msg: discord.Message | None = None
        self.loop: bool | str = False
        self.stop_votes: set[int] = set()
        self.stop_vote_message: discord.Message | None = None
        self.seek_lock = asyncio.Lock()
        # Khóa riêng cho thông báo TTS của các nút Music Player, tránh 2 người
        # bấm Skip/Pause/Resume/Seek cùng lúc làm chồng tiếng hoặc tranh VoiceClient.
        self.announcement_lock = asyncio.Lock()
        self.playback_id = 0
        self.playback_event: asyncio.Event | None = None
        self._restoring = False
        self.player_task = self.bot.loop.create_task(self.player_loop())

    def _current_position(self) -> int:
        track = self.current
        if not track:
            return 0
        vc = self.guild.voice_client
        if vc and vc.is_paused():
            return max(0, int(getattr(track, "paused_at", 0)))
        started = getattr(track, "started_at", None)
        if started is None:
            return max(0, int(getattr(track, "resume_position", 0)))
        return max(0, int(asyncio.get_running_loop().time() - started))

    async def update_progress_embed(self) -> None:
        """Cập nhật thời gian phát trên Music Player mà không gửi message mới.

        Chỉ edit message hiện tại mỗi MUSIC_STATE_INTERVAL giây, nên không
        tạo message/delete message liên tục và giảm tối đa API spam.
        """
        if not self.current or not self.last_msg:
            return

        async with self.render_lock:
            message = self.last_msg
            track = self.current
            if not message or not track:
                return
            try:
                embed = self.build_embed(track)
                await message.edit(embed=embed)
            except (discord.NotFound, discord.Forbidden):
                # Message bị xóa hoặc bot mất quyền sửa message.
                if self.last_msg is message:
                    self.last_msg = None
            except discord.HTTPException:
                # Lỗi API tạm thời: lần cập nhật 5 giây sau sẽ thử lại.
                logger.warning(
                    "Không thể cập nhật tiến trình Music Player guild %s.",
                    self.guild.id,
                )

    def save_state(self) -> None:
        if not self.current and not self.queue:
            delete_music_state(self.guild.id)
            return

        states = load_music_states()
        current_state = None
        if self.current:
            current_state = _track_state(
                self.current,
                position=self._current_position(),
                paused=bool(self.guild.voice_client and self.guild.voice_client.is_paused()),
            )

        queue_state = [
            _track_state(item, position=0, paused=False)
            for item in self.queue
        ]
        voice_client = self.guild.voice_client
        states[self.guild.id] = {
            "version": 1,
            "guild_id": self.guild.id,
            "text_channel_id": self.channel.id,
            "chat_channel_id": self.channel.id,
            "voice_channel_id": voice_client.channel.id if voice_client and voice_client.channel else None,
            "message_id": self.last_msg.id if self.last_msg else None,
            "loop": self.loop,
            "current": current_state,
            "queue": queue_state,
        }
        save_music_states(states)

    async def restore_state(self, state: dict[str, Any]) -> None:
        """Khôi phục bài hiện tại + hàng chờ sau khi bot khởi động lại.

        Không xóa embed cũ. Khi bài được phát thành công, player_loop sẽ
        tự gửi một embed Music Player mới.
        """
        self._restoring = True
        try:
            self.loop = state.get("loop", False)
            if self.loop not in (False, "song", "queue"):
                self.loop = False

            async def restore_track(item: Any) -> YTDLSource | None:
                if not isinstance(item, dict):
                    return None
                source_url = str(item.get("webpage_url") or "").strip()
                if not source_url:
                    return None

                requester: discord.Member | discord.User | None = None
                requester_id = item.get("requester_id")
                if isinstance(requester_id, int) and requester_id > 0:
                    requester = self.guild.get_member(requester_id)
                    if requester is None:
                        try:
                            requester = await self.bot.fetch_user(requester_id)
                        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                            pass
                if requester is None:
                    requester = self.guild.me or self.bot.user
                if requester is None:
                    return None

                position = max(0, int(item.get("position", 0) or 0))
                paused = bool(item.get("paused", False))
                try:
                    track = await YTDLSource.from_url(
                        source_url, requester, loop=self.bot.loop, start_time=position
                    )
                    track.resume_position = position
                    track.resume_paused = paused
                    return track
                except Exception:
                    logger.exception(
                        "Không thể khôi phục bài '%s' trong guild %s.",
                        item.get("title", source_url), self.guild.id,
                    )
                    return None

            current_item = state.get("current")
            restored_current = await restore_track(current_item) if current_item else None

            restored_queue: list[YTDLSource] = []
            saved_queue = state.get("queue", [])
            if isinstance(saved_queue, list):
                for item in saved_queue:
                    track = await restore_track(item)
                    if track is not None:
                        restored_queue.append(track)

            async with self.queue_lock:
                self.queue.clear()
                if restored_current is not None:
                    self.queue.append(restored_current)
                self.queue.extend(restored_queue)
                if self.queue:
                    self.queue_event.set()

            if self.queue:
                logger.info(
                    "Đã khôi phục Music Player guild %s: current=%s, queue=%s. "
                    "Embed cũ giữ nguyên; sẽ tạo embed mới.",
                    self.guild.id, bool(restored_current), len(restored_queue),
                )
            elif current_item or saved_queue:
                logger.warning(
                    "Chưa khôi phục được Music Player guild %s; giữ music_state.json để thử lại.",
                    self.guild.id,
                )
            else:
                delete_music_state(self.guild.id)
        finally:
            self._restoring = False

    async def _wait_for_queue(self) -> None:
        """Chờ queue bằng Event, không polling liên tục."""
        while not self.bot.is_closed():
            async with self.queue_lock:
                if self.queue:
                    return
                self.queue_event.clear()
            await self.queue_event.wait()

    async def _delete_embed(self, message: discord.Message | None = None) -> None:
        old = message if message is not None else self.last_msg
        if old is self.last_msg:
            self.last_msg = None
        if old:
            try:
                await old.delete()
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                pass

    def build_idle_embed(self, *, stopped: bool = False) -> discord.Embed:
        """Embed trạng thái khi không còn bài đang phát.

        Không xóa Music Player khi bài cuối kết thúc; thay vào đó cập nhật
        embed để người dùng nhìn thấy ngay trạng thái mới.
        """
        if stopped:
            title = "⏹️ CHILL HOUSE MUSIC"
            description = "📭 **Đã dừng và làm sạch hàng chờ.**\nDùng `!play <tên bài>` để bắt đầu lại."
            color = discord.Color.red()
            footer = "🎧 ĐÃ DỪNG"
        elif self.queue:
            title = "⏳ CHILL HOUSE MUSIC"
            description = "⏳ **Đang chuyển sang bài tiếp theo...**"
            color = discord.Color.gold()
            footer = f"📋 HÀNG CHỜ · {len(self.queue)}"
        else:
            title = "🎶 CHILL HOUSE MUSIC"
            description = "📭 **Không còn bài nào đang phát.**\nDùng `!play <tên bài>` để thêm nhạc."
            color = discord.Color.from_rgb(88, 101, 242)
            footer = "🔶 ĐÃ HẾT BÀI"

        embed = discord.Embed(title=title, description=description, color=color)

        if self.queue:
            lines: list[str] = []
            for i, item in enumerate(self.queue[:8], 1):
                item_title = item.title.replace("\n", " ").strip()[:72]
                item_url = item.webpage_url or item.url
                linked_title = f"[{item_title}]({item_url})" if item_url else item_title
                requester = getattr(item.requester, "mention", "")
                requester_line = f"└ Yêu cầu bởi: {requester}" if requester else "└ Yêu cầu bởi: Không rõ"
                lines.append(f"**{i}.** {linked_title} · `{item.duration_str}`\n{requester_line}")
            if len(self.queue) > 8:
                lines.append(f"*… và còn **{len(self.queue) - 8}** bài khác*")
            embed.add_field(
                name="📋 Hàng chờ tiếp theo",
                value="\n".join(lines)[:1024],
                inline=False,
            )
        else:
            embed.add_field(
                name="📋 Hàng chờ tiếp theo",
                value="*Chưa có bài tiếp theo*",
                inline=False,
            )

        embed.set_image(url="https://cdn.discordapp.com/attachments/1551279871076859904/1555017933321670666/chill_wcg.gif?backend=b2&ex=6ac0f90e&is=6abfa78e&hm=7e44d200eda2316b284af58494121d2b8dcbc08a5edd8e791097f6b8b5d61230&")
        embed.set_footer(text=footer)
        return embed

    async def recreate_embed(self, *, stopped: bool = False) -> None:
        """Cập nhật đúng 1 Music Player embed, tránh spam và tránh embed cũ bị kẹt."""
        async with self.render_lock:
            old = self.last_msg

            if stopped:
                embed = self.build_idle_embed(stopped=True)
                view = MusicPlayerView(self)
            elif self.current:
                embed = self.build_embed(self.current)
                view = MusicPlayerView(self)
            else:
                embed = self.build_idle_embed()
                view = MusicPlayerView(self)

            # Gửi embed mới trước rồi mới xóa embed cũ. Nếu Discord/Voice Chat
            # lỗi quyền hoặc lỗi API, embed cũ vẫn còn thay vì biến mất.
            try:
                new_msg = await self.channel.send(embed=embed, view=view)
            except (discord.Forbidden, discord.HTTPException, AttributeError):
                logger.exception(
                    "Không thể gửi/cập nhật Music Player trong channel %s của guild %s.",
                    getattr(self.channel, "id", None),
                    self.guild.id,
                )
                return

            self.last_msg = new_msg
            if old and old.id != new_msg.id:
                await self._delete_embed(old)
            self.save_state()

    async def player_loop(self) -> None:
        await self.bot.wait_until_ready()
        try:
            while not self.bot.is_closed():
                await self._wait_for_queue()
                if self.bot.is_closed():
                    return

                async with self.queue_lock:
                    if not self.queue:
                        continue
                    queued_track = self.queue.pop(0)

                try:
                    track = await refresh_source(queued_track, loop=self.bot.loop)
                except Exception as error:
                    error_text = str(error)
                    if "403" in error_text or "Forbidden" in error_text or "access denied" in error_text.lower():
                        logger.error(
                            "YouTube từ chối stream HTTP 403 cho %s. "
                            "Kiểm tra yt-dlp[default], Deno/Node và IP máy chủ.",
                            queued_track.title,
                        )
                    else:
                        logger.exception(
                            "Không thể tải lại stream cho bài trong hàng chờ: %s",
                            queued_track.title,
                        )
                    # Không làm mất bài nếu YouTube tạm thời từ chối stream.
                    # Đưa bài trở lại đầu queue để lần kế tiếp thử lại.
                    async with self.queue_lock:
                        self.queue.insert(0, queued_track)
                        self.queue_event.set()
                    self.current = None
                    await asyncio.sleep(2)
                    continue

                self.current = track
                vc = self.guild.voice_client
                if not vc or not vc.is_connected():
                    await self.destroy()
                    return

                self.playback_id += 1
                playback_id = self.playback_id
                done_event = asyncio.Event()
                self.playback_event = done_event

                def after_playback(error: Exception | None) -> None:
                    if playback_id != self.playback_id:
                        return
                    if error:
                        logger.error(
                            "Music playback error in guild %s: %s",
                            self.guild.id,
                            error,
                        )
                    self.bot.loop.call_soon_threadsafe(done_event.set)

                try:
                    start_position = max(0, int(getattr(track, "resume_position", 0)))
                    track.started_at = asyncio.get_running_loop().time() - start_position
                    track.paused_at = start_position if getattr(track, "resume_paused", False) else 0
                    vc.play(track, after=after_playback)
                    if getattr(track, "resume_paused", False):
                        vc.pause()
                    track.resume_position = 0
                    track.resume_paused = False
                except Exception:
                    logger.exception(
                        "Không thể bắt đầu phát nhạc trong guild %s",
                        self.guild.id,
                    )
                    self.current = None
                    continue

                self.stop_votes.clear()
                self.save_state()
                await self.recreate_embed()
                await done_event.wait()

                # Seek/stop đã thay playback_id. Không lấy bài kế tiếp 2 lần.
                if self.playback_id != playback_id:
                    new_event = self.playback_event
                    if new_event is not None and new_event is not done_event:
                        await new_event.wait()
                    continue

                if self.loop in {"song", "queue"}:
                    async with self.queue_lock:
                        if self.loop == "song":
                            self.queue.insert(0, track)
                        else:
                            self.queue.append(track)
                        self.queue_event.set()

                self.current = None
                if self.queue:
                    # Có bài kế tiếp: giữ Music Player và báo trạng thái chuyển bài.
                    self.save_state()
                    await self.recreate_embed()
                else:
                    # Hết hẳn queue: KHÔNG xóa embed. Cập nhật nó thành
                    # "ĐÃ HẾT BÀI" để người dùng thấy sự kiện đã xảy ra.
                    delete_music_state(self.guild.id)
                    await self.recreate_embed()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Music player loop bị lỗi trong guild %s", self.guild.id)
            self.current = None

    async def start_stop_vote(self, user: discord.abc.User, channel: discord.abc.Messageable | None) -> None:
        self.stop_votes.add(user.id)
        count = len(self.stop_votes)
        if count >= 2:
            if self.stop_vote_message:
                try:
                    await self.stop_vote_message.delete()
                except discord.HTTPException:
                    pass
                self.stop_vote_message = None
            await self.stop_now()
            return

        if channel:
            text = "🗳️ **Vote Stop:** `1/2` — cần thêm **1 phiếu** để dừng."
            if self.stop_vote_message:
                try:
                    await self.stop_vote_message.edit(content=text)
                except discord.HTTPException:
                    self.stop_vote_message = None
            if not self.stop_vote_message:
                self.stop_vote_message = await channel.send(text, delete_after=60)

    async def stop_now(self) -> None:
        async with self.queue_lock:
            self.queue.clear()
            self.queue_event.set()

        self.stop_votes.clear()
        if self.stop_vote_message:
            try:
                await self.stop_vote_message.delete()
            except discord.HTTPException:
                pass
            self.stop_vote_message = None

        self.playback_id += 1
        if self.playback_event is not None:
            self.playback_event.set()

        vc = self.guild.voice_client
        if vc and (vc.is_playing() or vc.is_paused()):
            vc.stop()

        self.current = None
        delete_music_state(self.guild.id)
        self.next_event.set()
        await self.recreate_embed(stopped=True)

        task = self.player_task
        if not task.done() and task is not asyncio.current_task():
            task.cancel()
        self.bot.music_players.pop(self.guild.id, None)

    @staticmethod
    def format_voice_time(seconds: int) -> str:
        seconds = max(0, int(seconds))
        hours, remainder = divmod(seconds, 3600)
        minutes, secs = divmod(remainder, 60)
        if hours:
            return f"{hours} giờ {minutes} phút {secs} giây"
        if minutes:
            return f"{minutes} phút {secs} giây"
        return f"{secs} giây"

    async def announce_music_action(
        self,
        text: str,
        *,
        action: str,
        target_seconds: int | None = None,
        final_paused: bool = False,
    ) -> bool:
        """Phát TTS rồi khôi phục Music Player theo đúng trạng thái mong muốn.

        Quan trọng: TTS được tạo trước khi ngắt stream nhạc. Sau khi TTS xong,
        track hiện tại được đưa lại đầu queue với vị trí chính xác; player_loop
        sẽ tự refresh stream và tiếp tục phát. Vì vậy không gọi vc.play() TTS
        chồng trực tiếp lên source nhạc đang chạy.
        """
        async with self.announcement_lock:
            vc = self.guild.voice_client
            track = self.current
            if not vc or not vc.is_connected() or not track:
                return False

            was_paused = vc.is_paused()
            position = self._current_position()
            if target_seconds is not None:
                target_seconds = max(0, int(target_seconds))
                if track.duration:
                    target_seconds = min(target_seconds, max(0, int(track.duration) - 1))
                position = target_seconds

            # Tạo file trước để Google TTS chậm không làm nhạc bị ngắt giữa chừng.
            try:
                audio_path = await create_vietnamese_speech_file(text)
            except Exception:
                logger.exception("Không thể tạo TTS cho thao tác Music Player.")
                return False

            old_event = self.playback_event
            self.playback_id += 1
            new_playback_id = self.playback_id
            hold_event = asyncio.Event()
            self.playback_event = hold_event
            if old_event is not None:
                old_event.set()

            # Lưu lại trạng thái để player_loop có thể dựng lại stream sau TTS.
            track.resume_position = position
            track.resume_paused = bool(final_paused)
            self.current = None

            if vc.is_playing() or vc.is_paused():
                vc.stop()

            # Chờ một nhịp để AudioPlayer cũ thoát hoàn toàn trước khi vc.play(TTS).
            await asyncio.sleep(0.05)

            tts_done = asyncio.Event()
            audio_source: discord.AudioSource | None = None

            def after_tts(error: Exception | None) -> None:
                if error:
                    logger.warning("Lỗi phát TTS Music Player guild %s: %s", self.guild.id, error)
                self.bot.loop.call_soon_threadsafe(tts_done.set)

            try:
                audio_source = discord.FFmpegPCMAudio(audio_path)
                vc.play(audio_source, after=after_tts)
                await tts_done.wait()
            except Exception:
                logger.exception("Không thể phát TTS Music Player guild %s.", self.guild.id)
                if audio_source is not None:
                    try:
                        audio_source.cleanup()
                    except Exception:
                        pass
                remove_temp_audio(audio_path)
                # Không bỏ mất bài: khôi phục queue ngay cả khi TTS lỗi.
            finally:
                remove_temp_audio(audio_path)

            # Nếu trong lúc TTS có thao tác khác chen vào, không ghi đè trạng thái mới.
            if self.playback_id != new_playback_id:
                hold_event.set()
                return True

            if action == "skip":
                # Bỏ bài hiện tại; player_loop sẽ lấy bài kế tiếp trong queue.
                self.current = None
            else:
                # Giữ bài hiện tại và để player_loop dựng lại stream từ vị trí mới.
                async with self.queue_lock:
                    self.queue.insert(0, track)
                    self.queue_event.set()
                self.current = None

            self.save_state()
            hold_event.set()
            return True

    async def seek_relative(self, seconds: int, interaction: discord.Interaction) -> None:
        vc = self.guild.voice_client
        track = self.current
        if not vc or not track:
            return await interaction.response.send_message("❌ Không có bài đang phát!", ephemeral=True)

        if vc.is_paused():
            current_pos = getattr(track, "paused_at", 0)
        else:
            current_pos = max(
                0,
                int(asyncio.get_running_loop().time() - getattr(
                    track, "started_at", asyncio.get_running_loop().time()
                )),
            )
        await interaction.response.defer(ephemeral=True)
        await self.seek_to_seconds(max(0, current_pos + seconds))

    async def seek_to(self, seconds: int, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        await self.seek_to_seconds(seconds)

    async def seek_to_seconds(self, seconds: int) -> None:
        async with self.seek_lock:
            track = self.current
            vc = self.guild.voice_client
            if not track or not vc or not vc.is_connected():
                return
            if track.duration:
                seconds = min(seconds, max(0, int(track.duration) - 1))
            source_url = track.webpage_url or track.title
            requester = track.requester
            was_paused = vc.is_paused()

            # Tải stream mới trước; nếu tải lỗi thì bài cũ vẫn tiếp tục.
            try:
                new_track = await YTDLSource.from_url(
                    source_url, requester, loop=self.bot.loop, start_time=seconds
                )
            except Exception:
                logger.exception("Không thể tải stream để tua bài hát")
                return

            if self.current is not track or vc is not self.guild.voice_client:
                try:
                    new_track.cleanup()
                except Exception:
                    pass
                return

            old_event = self.playback_event
            self.playback_id += 1
            new_playback_id = self.playback_id
            new_event = asyncio.Event()
            self.playback_event = new_event
            if old_event is not None:
                old_event.set()

            try:
                if vc.is_playing() or vc.is_paused():
                    vc.stop()

                new_track.started_at = asyncio.get_running_loop().time() - seconds
                new_track.paused_at = seconds if was_paused else 0

                def after_seek_playback(error: Exception | None) -> None:
                    if new_playback_id != self.playback_id or self.current is not new_track:
                        return
                    if error:
                        logger.error("Music playback error after seek: %s", error)
                    self.bot.loop.call_soon_threadsafe(new_event.set)

                self.current = new_track
                vc.play(new_track, after=after_seek_playback)
                if was_paused:
                    vc.pause()
                self.save_state()
                await self.recreate_embed()
            except Exception:
                self.current = track
                self.playback_id += 1
                self.playback_event = old_event
                try:
                    new_track.cleanup()
                except Exception:
                    pass
                logger.exception("Không thể tua bài hát")

    def build_embed(self, track: YTDLSource) -> discord.Embed:
        vc = self.guild.voice_client
        is_paused = vc.is_paused() if vc else False
        is_playing = vc.is_playing() if vc else False
        status = (
            "⏸️ TẠM DỪNG"
            if is_paused
            else ("▶️ ĐANG PHÁT" if is_playing else "⏳ ĐANG CHUẨN BỊ")
        )
        color = discord.Color.gold() if is_paused else discord.Color.from_rgb(88, 101, 242)

        # Hiển thị tiến trình dạng MM:SS / MM:SS.
        # _current_position() chỉ tính thời gian cục bộ, không gọi YouTube/FFmpeg.
        if track.duration:
            total = max(0, int(track.duration))
            position = self._current_position() if self.current is track else 0
            position = min(max(0, int(position)), total)
            pos_min, pos_sec = divmod(position, 60)
            total_min, total_sec = divmod(total, 60)
            pos_text = f"{pos_min:02d}:{pos_sec:02d} / {total_min:02d}:{total_sec:02d}"
        else:
            position = self._current_position() if self.current is track else 0
            pos_min, pos_sec = divmod(max(0, int(position)), 60)
            pos_text = f"{pos_min:02d}:{pos_sec:02d} / N/A"

        url = track.webpage_url or track.url
        title = track.title.replace("\n", " ").strip()[:100]
        title_line = f"[🎵 {title}]({url})" if url else f"🎵 {title}"
        requester = getattr(track.requester, "mention", "") or "Không rõ"

        # Dòng trạng thái dạng ký tự theo đúng mẫu:

        loop_symbol = {
            False: "🔁 TẮT",
            "queue": "🔁 HÀNG CHỜ",
            "song": "🔂 LẶP LẠI",
        }.get(self.loop, "↻")


        description = (
            f"{title_line}\n"
            f"└ Yêu cầu bởi: {requester}\n\n⏱️`{pos_text}`\n\n"
        )

        # Separator nằm ngay trước khu hàng chờ để hai phần nhìn tách biệt.
        description += "✧────────────────────✧\n### 𝄞𝄞𝄞 **HÀNG CHỜ**"

        if self.queue:
            lines: list[str] = []
            for i, item in enumerate(self.queue[:8], 1):
                item_title = item.title.replace("\n", " ").strip()[:72]
                item_url = item.webpage_url or item.url
                linked_title = f"[{item_title}]({item_url})" if item_url else item_title
                item_requester = getattr(item.requester, "mention", "") or "Không rõ"
                lines.append(
                    f"**{i}.** {linked_title} · `{item.duration_str}`\n"
                    f"   └ Yêu cầu bởi: {item_requester}"
                )
            if len(self.queue) > 8:
                lines.append(f"*… và còn **{len(self.queue) - 8}** bài khác*")
            description += "\n\n" + "\n\n".join(lines)
        else:
            description += "\n\n*( 𝄞 Hàng chờ trống! )*\n-#  *♯ Tips: `!play <tên bài hát>`*"

        description += "\n\n✧────────────────────✧"

        embed = discord.Embed(description=description, color=color)
        if track.thumbnail:
            # Thumbnail vẫn là ảnh YouTube của bài đang phát.
            embed.set_thumbnail(url=track.thumbnail)

        # GIF lớn cố định ở cuối embed.
        embed.set_image(
            url="https://cdn.discordapp.com/attachments/1551279871076859904/1555017933321670666/chill_wcg.gif?backend=b2&ex=6ac0f90e&is=6abfa78e&hm=7e44d200eda2316b284af58494121d2b8dcbc08a5edd8e791097f6b8b5d61230&"
        )
        embed.set_footer(text=f"{status} | {loop_symbol}")
        return embed

    async def destroy(self) -> None:
        self.queue_event.set()
        self.playback_id += 1
        if self.playback_event is not None:
            self.playback_event.set()
        await self._delete_embed()
        vc = self.guild.voice_client
        if vc and (vc.is_playing() or vc.is_paused()):
            vc.stop()
        if not self.player_task.done() and self.player_task is not asyncio.current_task():
            self.player_task.cancel()
        self.bot.music_players.pop(self.guild.id, None)


class VietnameseDiscordBot(commands.Bot):
    def __init__(self) -> None:
        super().__init__(
            command_prefix="!",
            intents=build_intents(),
            status=discord.Status.online,
        )
        self.target_voice_channels = load_voice_targets()
        self.voice_keepalive_task: asyncio.Task[None] | None = None
        self.health_server: asyncio.Server | None = None
        self.music_players: dict[int, GuildMusicPlayer] = {}
        self.music_state_task: asyncio.Task[None] | None = None
        self._music_states_restored = False

    def get_music_player(self, ctx: commands.Context) -> GuildMusicPlayer:
        # Dù !play được gõ ở text channel nào, Music Player phải nằm trong
        # chat tích hợp của Voice Channel mà bot đang ở.
        voice_channel = None
        if ctx.guild.voice_client and ctx.guild.voice_client.channel:
            voice_channel = ctx.guild.voice_client.channel
        elif ctx.author.voice and ctx.author.voice.channel:
            voice_channel = ctx.author.voice.channel

        chat_channel = voice_channel or ctx.channel

        if ctx.guild.id in self.music_players:
            player = self.music_players[ctx.guild.id]
            player.channel = chat_channel
            return player

        player = GuildMusicPlayer(self, ctx.guild, chat_channel)
        self.music_players[ctx.guild.id] = player
        return player

    def persist_voice_targets(self) -> None:
        save_voice_targets(self.target_voice_channels)

    async def start_health_server(self) -> None:
        port = int(os.getenv("PORT", "5000"))
        if not 0 < port < 65536:
            raise RuntimeError(f"PORT không hợp lệ: {port}")

        self.health_server = await asyncio.start_server(
            self.handle_health_request,
            host="0.0.0.0",
            port=port,
        )
        logger.info("Health endpoint đang lắng nghe tại 0.0.0.0:%s/health", port)

    async def handle_health_request(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        status_code = 200
        reason = "OK"
        payload: dict[str, Any] = {
            "status": "ok",
            "discord_ready": self.is_ready(),
            "voice_connected": any(
                voice_client.is_connected() for voice_client in self.voice_clients
            ),
        }
        method = "GET"

        try:
            request_line = await asyncio.wait_for(reader.readline(), timeout=5)
            request_parts = request_line.decode("ascii", errors="replace").split()
            if len(request_parts) < 2:
                status_code, reason = 400, "Bad Request"
                payload = {"status": "error"}
            else:
                method = request_parts[0].upper()
                path = request_parts[1].split("?", 1)[0]
                if method not in {"GET", "HEAD"}:
                    status_code, reason = 405, "Method Not Allowed"
                    payload = {"status": "error"}
                elif path not in {"/", "/health"}:
                    status_code, reason = 404, "Not Found"
                    payload = {"status": "not_found"}
        except asyncio.TimeoutError:
            status_code, reason = 408, "Request Timeout"
            payload = {"status": "error"}

        response_body = json.dumps(payload).encode("utf-8")
        response_headers = (
            f"HTTP/1.1 {status_code} {reason}\r\n"
            "Content-Type: application/json; charset=utf-8\r\n"
            f"Content-Length: {len(response_body)}\r\n"
            "Cache-Control: no-store\r\n"
            "Connection: close\r\n"
            "\r\n"
        ).encode("ascii")
        writer.write(response_headers)
        if method != "HEAD":
            writer.write(response_body)
        try:
            await writer.drain()
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except ConnectionError:
                pass

    async def setup_hook(self) -> None:
        await self.start_health_server()

        self.voice_keepalive_task = asyncio.create_task(
            self.keep_voice_alive(),
            name="voice-keepalive",
        )
        self.music_state_task = asyncio.create_task(
            self.persist_music_states_loop(),
            name="music-state-persistence",
        )

    async def close(self) -> None:
        for player in list(self.music_players.values()):
            try:
                player.save_state()
            except Exception:
                logger.exception("Không thể lưu Music Player trước khi đóng bot.")

        if self.music_state_task is not None:
            self.music_state_task.cancel()
            try:
                await self.music_state_task
            except asyncio.CancelledError:
                pass
            self.music_state_task = None

        if self.voice_keepalive_task is not None:
            self.voice_keepalive_task.cancel()
            try:
                await self.voice_keepalive_task
            except asyncio.CancelledError:
                pass
            self.voice_keepalive_task = None

        for player in list(self.music_players.values()):
            try:
                await player.destroy()
            except Exception:
                logger.exception("Lỗi khi dọn Music Player lúc bot đóng.")

        for vc in list(self.voice_clients):
            try:
                if vc.is_connected():
                    await vc.disconnect(force=True)
            except Exception:
                logger.exception("Lỗi khi ngắt voice lúc bot đóng.")

        if self.health_server is not None:
            self.health_server.close()
            await self.health_server.wait_closed()
            self.health_server = None
        await super().close()

    async def on_ready(self) -> None:
        await self.change_presence(status=discord.Status.online)
        logger.info("Bot đã đăng nhập thành công: %s", self.user)
        if not self._music_states_restored:
            self._music_states_restored = True
            asyncio.create_task(self.restore_music_states(), name="restore-music-states")

    async def restore_music_states(self) -> None:
        await asyncio.sleep(2)
        states = load_music_states()
        for guild_id, state in states.items():
            guild = self.get_guild(guild_id)
            if guild is None:
                continue
            voice_channel_id = state.get("voice_channel_id") or self.target_voice_channels.get(guild_id)
            if isinstance(voice_channel_id, int):
                self.target_voice_channels[guild_id] = voice_channel_id
            if not isinstance(voice_channel_id, int):
                logger.warning("Không có voice channel để khôi phục Music Player guild %s.", guild_id)
                continue
            channel = guild.get_channel(voice_channel_id)
            if not isinstance(channel, discord.VoiceChannel):
                try:
                    channel = await self.fetch_channel(voice_channel_id)
                except Exception:
                    logger.exception("Không thể lấy voice channel %s để khôi phục.", voice_channel_id)
                    continue
            if not isinstance(channel, discord.VoiceChannel):
                continue
            try:
                vc = discord.utils.get(self.voice_clients, guild=guild)
                if vc is None or not vc.is_connected():
                    vc = await channel.connect(reconnect=True, timeout=30.0)
                elif vc.channel.id != channel.id:
                    await vc.move_to(channel)
            except Exception:
                logger.exception("Không thể kết nối voice để khôi phục guild %s.", guild_id)
                continue

            # Chat tích hợp của Voice Channel chính là nơi Discord hiển thị
            # tin nhắn/Embed của cuộc gọi. discord.py hiện hỗ trợ VoiceChannel
            # như một Messageable nên có thể .send(embed=...) trực tiếp.
            chat_channel = channel

            player = self.music_players.get(guild_id)
            if player is None:
                player = GuildMusicPlayer(self, guild, chat_channel)
                self.music_players[guild_id] = player
            else:
                player.channel = chat_channel

            # Không xóa embed cũ. Khi khôi phục, player sẽ tạo embed mới.
            player.last_msg = None
            await player.restore_state(state)
        self.persist_voice_targets()

    async def persist_music_states_loop(self) -> None:
        await self.wait_until_ready()
        while not self.is_closed():
            await asyncio.sleep(MUSIC_STATE_INTERVAL)
            for player in list(self.music_players.values()):
                try:
                    player.save_state()
                    await player.update_progress_embed()
                except Exception:
                    logger.exception("Không thể autosave/cập nhật tiến trình Music Player guild %s.", player.guild.id)

    async def keep_voice_alive(self) -> None:
        await self.wait_until_ready()

        while not self.is_closed():
            for guild_id, channel_id in list(self.target_voice_channels.items()):
                guild = self.get_guild(guild_id)
                if guild is None:
                    continue

                channel = guild.get_channel(channel_id)
                if channel is None:
                    try:
                        channel = await self.fetch_channel(channel_id)
                    except discord.NotFound:
                        logger.warning(
                            "Không tìm thấy voice channel %s; ngừng tự kết nối.",
                            channel_id,
                        )
                        self.target_voice_channels.pop(guild_id, None)
                        self.persist_voice_targets()
                        continue
                    except (discord.Forbidden, discord.HTTPException) as error:
                        logger.warning(
                            "Không thể tải voice channel %s: %s", channel_id, error
                        )
                        continue

                if not isinstance(channel, discord.VoiceChannel):
                    logger.warning(
                        "Kênh %s không phải voice channel; ngừng tự kết nối.",
                        channel_id,
                    )
                    self.target_voice_channels.pop(guild_id, None)
                    self.persist_voice_targets()
                    continue

                voice_client = discord.utils.get(self.voice_clients, guild=guild)
                try:
                    if voice_client is None or not voice_client.is_connected():
                        logger.info("Đang kết nối lại vào voice channel: %s", channel.name)
                        await channel.connect(reconnect=True, timeout=30.0)
                    elif voice_client.channel.id != channel.id:
                        await voice_client.move_to(channel)
                except Exception:
                    logger.warning(
                        "Không thể duy trì kết nối voice ở %s; sẽ thử lại.",
                        channel.name,
                        exc_info=True,
                    )

            await asyncio.sleep(VOICE_RECONNECT_INTERVAL)


bot = VietnameseDiscordBot()


# --- LỆNH MUSIC PLAYER (PREFIX "!") ---

async def require_same_voice(ctx: commands.Context) -> bool:
    if not ctx.guild:
        return False
    vc = discord.utils.get(bot.voice_clients, guild=ctx.guild)
    if not ctx.author.voice or not ctx.author.voice.channel:
        await ctx.send("❌ Bạn phải ở cùng Voice Channel với bot để điều khiển nhạc.", delete_after=5)
        return False
    if vc and vc.is_connected() and ctx.author.voice.channel != vc.channel:
        await ctx.send(f"❌ Bạn phải ở cùng voice channel với bot ({vc.channel.mention}).", delete_after=5)
        return False
    return True

@bot.command(name="play", aliases=["p"], help="Phát nhạc từ YouTube (!play <tên_bài/URL>)")
async def play_music(ctx: commands.Context, *, query: str = "") -> None:
    if not ctx.guild:
        return

    if not query:
        await ctx.send("❌ Vui lòng nhập tên bài hát hoặc URL. Ví dụ: `!play Sơn Tùng MTP`", delete_after=5)
        return

    if not ctx.author.voice or not ctx.author.voice.channel:
        await ctx.send("❌ Bạn phải vào Voice Channel trước!", delete_after=5)
        return

    voice_client = discord.utils.get(bot.voice_clients, guild=ctx.guild)
    if voice_client and voice_client.is_connected() and voice_client.channel != ctx.author.voice.channel:
        await ctx.send(f"❌ Bot đang ở {voice_client.channel.mention}. Bạn phải vào cùng voice channel để thêm nhạc.", delete_after=5)
        return
    if not voice_client or not voice_client.is_connected():
        try:
            await ctx.author.voice.channel.connect(reconnect=True, timeout=30.0)
        except Exception:
            await ctx.send("❌ Bot không thể vào voice channel này.", delete_after=5)
            return

    async with ctx.typing():
        try:
            player_source = await YTDLSource.from_url(query, ctx.author, loop=bot.loop)
        except Exception as e:
            error_text = str(e)
            if "403" in error_text or "Forbidden" in error_text or "access denied" in error_text.lower():
                await ctx.send(
                    "❌ YouTube đang từ chối stream (HTTP 403). Đã bật cấu hình yt-dlp mới; "
                    "nếu vẫn lỗi trên Replit thì IP máy chủ đang bị YouTube CDN từ chối.",
                    delete_after=8,
                )
            else:
                await ctx.send(f"❌ Lỗi tìm kiếm/tải bài hát: {e}", delete_after=5)
            return

        music_player = bot.get_music_player(ctx)
        async with music_player.queue_lock:
            music_player.queue.append(player_source)
            music_player.queue_event.set()
            music_player.save_state()

        if music_player.current:
            await ctx.send(f"✅ Đã thêm **{player_source.title}** vào hàng chờ!", delete_after=5)
            await music_player.recreate_embed()
        else:
            # Player loop sẽ tự nhận Event và bắt đầu bài; không cần polling.
            await asyncio.sleep(0)


@bot.command(name="pause", help="Tạm dừng bài hát đang phát")
async def pause_music(ctx: commands.Context) -> None:
    if not ctx.guild:
        return
    if not await require_same_voice(ctx):
        return
    voice_client = discord.utils.get(bot.voice_clients, guild=ctx.guild)
    if voice_client and voice_client.is_playing():
        voice_client.pause()
        if ctx.guild.id in bot.music_players:
            await bot.music_players[ctx.guild.id].recreate_embed()
        await ctx.send("⏸️ Đã tạm dừng nhạc.", delete_after=5)
    else:
        await ctx.send("❌ Không có nhạc đang phát.", delete_after=5)


@bot.command(name="resume", aliases=["unpause"], help="Tiếp tục phát nhạc")
async def resume_music(ctx: commands.Context) -> None:
    if not ctx.guild:
        return
    if not await require_same_voice(ctx):
        return
    voice_client = discord.utils.get(bot.voice_clients, guild=ctx.guild)
    if voice_client and voice_client.is_paused():
        voice_client.resume()
        if ctx.guild.id in bot.music_players:
            await bot.music_players[ctx.guild.id].recreate_embed()
        await ctx.send("▶️ Đã tiếp tục phát nhạc.", delete_after=5)
    else:
        await ctx.send("❌ Nhạc không bị tạm dừng.", delete_after=5)


@bot.command(name="skip", aliases=["s"], help="Bỏ qua bài hát hiện tại")
async def skip_music(ctx: commands.Context) -> None:
    if not ctx.guild:
        return
    if not await require_same_voice(ctx):
        return
    voice_client = discord.utils.get(bot.voice_clients, guild=ctx.guild)
    player = bot.music_players.get(ctx.guild.id)
    if voice_client and player and (voice_client.is_playing() or voice_client.is_paused()):
        title = player.current.title if player.current else "bài hát hiện tại"
        await ctx.send(f"⏭️ **{ctx.author.display_name}** đã skip **{title}**.", delete_after=4)
        if voice_client.is_playing() or voice_client.is_paused():
            voice_client.stop()
    else:
        await ctx.send("❌ Không có nhạc đang phát.", delete_after=5)


@bot.command(name="stop", help="Dừng nhạc và xóa danh sách chờ")
async def stop_music(ctx: commands.Context) -> None:
    if not ctx.guild:
        return
    if not await require_same_voice(ctx):
        return
    player = bot.music_players.get(ctx.guild.id)
    if not player:
        await ctx.send("❌ Music Player đang không hoạt động.", delete_after=5)
        return
    if isinstance(ctx.author, discord.Member) and (ctx.author.guild_permissions.administrator or ctx.author.guild_permissions.manage_guild):
        await ctx.send(f"⏹️ **{ctx.author.display_name}** đã dừng Music Player.", delete_after=4)
        await asyncio.sleep(1.0)
        await player.stop_now()
        return
    await player.start_stop_vote(ctx.author, ctx.channel)
    if player.stop_votes:
        await ctx.send(f"🗳️ **{ctx.author.display_name}** đã vote Stop. **{len(player.stop_votes)}/2**", delete_after=5)


@bot.command(name="queue", aliases=["q"], help="Xem danh sách nhạc đang chờ")
async def view_queue(ctx: commands.Context) -> None:
    if not ctx.guild:
        return
    if not await require_same_voice(ctx):
        return
    player = bot.music_players.get(ctx.guild.id)
    if not player:
        await ctx.send("📭 Hàng chờ hiện tại đang trống.", delete_after=5)
        return

    if player.current:
        now = player.current.title[:90]
        now_line = f"🎵 **Đang phát:** [{now}]({player.current.webpage_url or player.current.url})"
    else:
        now_line = "⏳ **Đang chuẩn bị phát...**"

    lines = []
    for i, song in enumerate(player.queue[:15], 1):
        title = song.title.replace("\n", " ").strip()[:75]
        url = song.webpage_url or song.url
        linked = f"[{title}]({url})" if url else title
        lines.append(
            f"**{i}.** {linked}  `⏱ {song.duration_str}`\n"
            f"└ Yêu cầu bởi: {song.requester.mention}"
        )

    text = "\n\n".join(lines) if lines else "📭 *Hàng chờ đang trống.*"
    if len(player.queue) > 15:
        text += f"\n\n*… còn **{len(player.queue) - 15}** bài khác*"

    embed = discord.Embed(
        title="📋 HÀNG CHỜ TIẾP THEO",
        description=f"{now_line}\n\n{text}"[:4000],
        color=discord.Color.from_rgb(88, 101, 242),
    )
    embed.set_footer(text=f"🎧 Tổng cộng {len(player.queue)} bài đang chờ")
    await ctx.send(embed=embed, delete_after=20)


@bot.command(name="status")
async def set_status(ctx: commands.Context, state: Literal["online", "idle", "dnd", "invisible"], *, activity_text: str = "") -> None:
    status_map = {
        "online": discord.Status.online,
        "idle": discord.Status.idle,
        "dnd": discord.Status.dnd,
        "invisible": discord.Status.invisible,
    }

    act = discord.Game(name=activity_text) if activity_text else None
    await bot.change_presence(status=status_map[state], activity=act)
    await ctx.send(f"✅ Đã đổi trạng thái bot sang **{state.upper()}**{f' với hoạt động: *{activity_text}*' if activity_text else ''}")


@bot.event
async def on_voice_state_update(
    member: discord.Member,
    before: discord.VoiceState,
    after: discord.VoiceState,
) -> None:
    if bot.user is None:
        return

    player = bot.music_players.get(member.guild.id)
    if player and player.stop_votes:
        vc = member.guild.voice_client
        if vc and vc.channel and member.id in player.stop_votes and after.channel != vc.channel:
            player.stop_votes.discard(member.id)
            if player.stop_vote_message:
                try:
                    count = len(player.stop_votes)
                    await player.stop_vote_message.edit(
                        content=f"🗳️ **Vote Stop:** `{count}/2` — cần {2 - count} phiếu nữa để dừng."
                    )
                except discord.HTTPException:
                    pass

    if member.id != bot.user.id or before.channel == after.channel:
        return

    if after.channel is None:
        target_channel_id = bot.target_voice_channels.get(member.guild.id)
        if target_channel_id is None:
            logger.info("Bot đã rời voice channel theo yêu cầu.")
        else:
            logger.warning("Bot bị ngắt khỏi voice channel %s; sẽ thử kết nối lại.", target_channel_id)
        return

    logger.info(
        "Bot đang ở voice channel %s (guild %s).",
        after.channel.id,
        member.guild.id,
    )


async def _delete_command_message(ctx: commands.Context) -> None:
    """Xóa tin nhắn lệnh để câu lệnh !say không làm rối kênh chat."""
    try:
        if ctx.message:
            await ctx.message.delete()
    except (discord.HTTPException, discord.Forbidden):
        pass


async def _voice_command_reply(
    voice_client: discord.VoiceClient,
    content: str,
    *,
    delete_after: float | None = 5.0,
) -> None:
    """Gửi phản hồi trực tiếp vào voice channel bot đang ở."""
    channel = voice_client.channel
    try:
        await channel.send(content, delete_after=delete_after)
    except (discord.HTTPException, discord.Forbidden):
        logger.warning(
            "Không thể gửi phản hồi !say vào voice channel %s.",
            getattr(channel, "id", "unknown"),
        )


@bot.command(name="say")
async def say(ctx: commands.Context, *, text: str) -> None:
    text = text.strip()

    async def react_ok() -> None:
        try:
            await ctx.message.add_reaction("✅")
        except (discord.HTTPException, discord.Forbidden):
            pass

    async def react_error() -> None:
        try:
            await ctx.message.add_reaction("❌")
        except (discord.HTTPException, discord.Forbidden):
            pass

    if not text:
        await react_error()
        # Nếu chưa có voice client thì vẫn phản hồi tại kênh lệnh.
        if ctx.guild is not None and ctx.guild.voice_client is not None:
            await _voice_command_reply(ctx.guild.voice_client, "❌ Bạn cần nhập nội dung muốn đọc.")
        else:
            await react_error()
        return

    guild = ctx.guild
    voice_client = (
        discord.utils.get(bot.voice_clients, guild=guild) if guild is not None else None
    )
    if voice_client is None or not voice_client.is_connected():
        await react_error()
        return

    speaker_name = ctx.author.display_name
    spoken_text = f"{speaker_name} vừa nói {text}"
    if len(spoken_text) > MAX_TTS_LENGTH:
        await react_error()
        return

    if voice_client.is_playing() or voice_client.is_paused():
        await _voice_command_reply(
            voice_client,
            "❌ Bot đang phát nhạc! Vui lòng không dùng `!say` lúc này để tránh làm gián đoạn bài hát.",
        )
        await react_error()
        return

    started = await text_to_speech(spoken_text, voice_client)
    if not started:
        await react_error()
        return

    # Không gửi thêm tin nhắn; chỉ đánh dấu lệnh đã xử lý thành công.
    await react_ok()


@bot.command(name="join")
async def join(ctx: commands.Context) -> None:
    voice_state = ctx.author.voice
    if voice_state is None or not isinstance(
        voice_state.channel, discord.VoiceChannel
    ):
        await ctx.send("Bạn cần vào voice channel trước khi dùng lệnh này.")
        return

    channel = voice_state.channel
    guild = ctx.guild
    if guild is None:
        await ctx.send("Lệnh này chỉ dùng được trong máy chủ.", delete_after=5)
        return

    bot.target_voice_channels[guild.id] = channel.id
    bot.persist_voice_targets()
    voice_client = discord.utils.get(bot.voice_clients, guild=guild)

    try:
        if voice_client is None or not voice_client.is_connected():
            await channel.connect(reconnect=True, timeout=30.0)
        elif voice_client.channel.id != channel.id:
            await voice_client.move_to(channel)
    except Exception:
        bot.target_voice_channels.pop(guild.id, None)
        bot.persist_voice_targets()
        logger.exception("Không thể kết nối vào voice channel %s.", channel.id)
        await ctx.send("Không vào được voice channel. Hãy kiểm tra quyền Connect và Speak của bot.")
        return

    await ctx.send(f"Đã vào voice channel **{channel.name}**!.")


@bot.command(name="leave")
async def leave(ctx: commands.Context) -> None:
    guild = ctx.guild
    if guild is None:
        await ctx.send("Lệnh này chỉ dùng được trong máy chủ.")
        return

    bot.target_voice_channels.pop(guild.id, None)
    bot.persist_voice_targets()
    if guild.id in bot.music_players:
        await bot.music_players[guild.id].destroy()

    voice_client = discord.utils.get(bot.voice_clients, guild=guild)
    if voice_client is not None and voice_client.is_connected():
        await voice_client.disconnect(force=True)
        await ctx.send("Bot đã rời voice channel.")
    else:
        await ctx.send("Bot hiện không ở trong voice channel nào.")


@bot.event
async def on_message(message: discord.Message) -> None:
    if message.author.bot:
        return

    # Khi người khác nhắn DM cho bot, tự động báo đây chỉ là bot và
    # hướng họ liên hệ trực tiếp với chủ bot. Chủ bot vẫn có thể dùng
    # command trong DM bình thường.
    if message.guild is None and message.author.id != BOT_OWNER_ID:
        try:
            embed = discord.Embed(
                title="🤖 Đây là Bot tự động",
                description=(
                    "Xin chào! 👋\n\n"
                    "Bạn đang nhắn tin trực tiếp với một **bot Discord tự động**. "
                    "Mình không phải người thật và không thể tự xử lý các vấn đề "
                    "cần chủ bot hỗ trợ.\n\n"
                    "🎵 Bot chủ yếu phục vụ **phát nhạc và hỗ trợ các tính năng "
                    "trong server**. Những tin nhắn gửi vào DM cho bot sẽ không được "
                    "chủ bot trả lời trực tiếp tại đây.\n\n"
                    "📩 Nếu bạn có câu hỏi, cần hỗ trợ, báo lỗi hoặc muốn liên hệ "
                    "với người quản lý bot, vui lòng liên hệ **Bé Tứ**.\n\n"
                    "💡 Bạn cũng có thể sử dụng các lệnh của bot trong server "
                    "nếu server đã bật quyền sử dụng bot."
                ),
                color=discord.Color.from_rgb(88, 101, 242),
            )
            if bot.user:
                embed.set_thumbnail(url=bot.user.display_avatar.url)
            embed.add_field(
                name="📌 Cần hỗ trợ / Báo lỗi",
                value=(
                    "Nếu bot gặp lỗi, lệnh không hoạt động, phát nhạc có vấn đề "
                    "hoặc bạn cần hỏi về bot, hãy liên hệ **Bé Tứ**.\n\n"
                    "👤 Người phụ trách: **Bé Tứ**"
                ),
                inline=False,
            )
            embed.add_field(
                name="🎵 Bot có thể làm gì?",
                value=(
                    "• Phát nhạc trong voice channel\n"
                    "• Điều khiển phát / tạm dừng / tiếp tục\n"
                    "• Skip, tua bài và lặp bài\n"
                    "• Hỗ trợ thông báo bằng giọng nói TTS\n"
                    "• Các tính năng tự động khác của server"
                ),
                inline=False,
            )
            embed.set_footer(text="💙 Đây là tin nhắn tự động • Vui lòng liên hệ Bé Tứ nếu cần hỗ trợ")
            await message.author.send(embed=embed)
        except (discord.Forbidden, discord.HTTPException):
            logger.warning(
                "Không thể trả lời DM cho user %s.",
                message.author.id,
            )
        return

    await bot.process_commands(message)
    if message.content.startswith("!"):
        try:
            await message.delete()
        except (discord.Forbidden, discord.HTTPException):
            pass



@bot.event
async def on_command_error(ctx: commands.Context, error: commands.CommandError) -> None:
    if isinstance(error, commands.CommandNotFound):
        return
    if isinstance(error, commands.CheckFailure):
        await ctx.send("❌ Bạn không có quyền dùng lệnh này.")
        return
    if isinstance(error, commands.MissingRequiredArgument):
        await ctx.send(f"❌ Thiếu tham số `{error.param.name}`. Dùng `!help` để xem hướng dẫn.")
        return
    if isinstance(error, commands.BadArgument):
        await ctx.send("❌ Tham số không hợp lệ. Dùng `!help` để xem cách dùng.")
        return
    logger.exception("Unhandled prefix command error: %s", error)
    await ctx.send("❌ Có lỗi xảy ra khi thực hiện lệnh.")

load_opus_library()
bot.run(DISCORD_TOKEN)
