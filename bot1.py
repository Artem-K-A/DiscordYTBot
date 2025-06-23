import discord
from discord import app_commands, ui
from discord.ext import commands, tasks
import yt_dlp as youtube_dl
import asyncio
from collections import deque
import math

# Настройки для аудио
YTDL_OPTIONS = {
    'format': 'bestaudio/best',
    'outtmpl': '%(extractor)s-%(id)s-%(title)s.%(ext)s',
    'restrictfilenames': True,
    'noplaylist': True,
    'nocheckcertificate': True,
    'ignoreerrors': False,
    'logtostderr': False,
    'quiet': True,
    'no_warnings': True,
    'default_search': 'auto',
    'source_address': '0.0.0.0',
    'postprocessors': [{
        'key': 'FFmpegExtractAudio',
        'preferredcodec': 'wav',
        'preferredquality': '192',
    }],
}

# Глобальные переменные для управления состоянием
queues = {}
now_playing = {}
player_controls = {}
volume_levels = {}

class Track:
    def __init__(self, info, url):
        self.title = info.get('title', 'Неизвестный трек')
        self.url = url
        self.audio_url = info.get('url')
        
        # Получаем прямую ссылку на аудио с обработкой None значений
        if 'formats' in info:
            # Фильтруем только аудио форматы с URL
            audio_formats = [f for f in info['formats'] 
                             if f.get('acodec') != 'none' 
                             and f.get('url')
                             and f.get('abr') is not None]
            
            if audio_formats:
                # Безопасное сравнение битрейтов
                try:
                    best_audio = max(audio_formats,
                                    key=lambda f: float(f.get('abr', 0)))  # Преобразуем в float
                    self.audio_url = best_audio['url']
                except:
                    # Если не удалось сравнить, берем первый доступный формат
                    self.audio_url = audio_formats[0]['url']
        
        self.duration = info.get('duration', 0)
        self.thumbnail = info.get('thumbnail', '')
        self.uploader = info.get('uploader', 'Неизвестный автор')
        
    def get_embed(self):
        embed = discord.Embed(
            title="Сейчас играет",
            description=f"[{self.title}]({self.url})",
            color=discord.Color.blue()
        )
        if self.thumbnail:
            embed.set_thumbnail(url=self.thumbnail)
        if self.uploader:
            embed.add_field(name="Автор", value=self.uploader, inline=True)
        return embed

class PlayerControls(ui.View):
    def __init__(self, guild_id):
        super().__init__(timeout=None)
        self.guild_id = guild_id
        
    @ui.button(emoji="⏯️", style=discord.ButtonStyle.secondary)
    async def toggle_play(self, interaction: discord.Interaction, button: ui.Button):
        vc = interaction.guild.voice_client
        if not vc:
            return await interaction.response.send_message("Бот не подключен к голосовому каналу!", ephemeral=True)
            
        if vc.is_paused():
            vc.resume()
            await interaction.response.send_message("▶️ Воспроизведение возобновлено", ephemeral=True)
        else:
            vc.pause()
            await interaction.response.send_message("⏸️ Воспроизведение приостановлено", ephemeral=True)
        
        await self.update_message(interaction)
        
    @ui.button(emoji="⏭️", style=discord.ButtonStyle.secondary)
    async def skip_track(self, interaction: discord.Interaction, button: ui.Button):
        vc = interaction.guild.voice_client
        if not vc:
            return await interaction.response.send_message("Бот не подключен к голосовому каналу!", ephemeral=True)
            
        if vc.is_playing() or vc.is_paused():
            vc.stop()
            await interaction.response.send_message("⏭️ Трек пропущен", ephemeral=True)
        else:
            await interaction.response.send_message("❌ Нет активного воспроизведения", ephemeral=True)
        
    @ui.button(emoji="🔉", style=discord.ButtonStyle.secondary)
    async def volume_down(self, interaction: discord.Interaction, button: ui.Button):
        guild_id = interaction.guild.id
        current_volume = volume_levels.get(guild_id, 1.0)
        new_volume = max(0.0, current_volume - 0.1)
        volume_levels[guild_id] = new_volume
        
        vc = interaction.guild.voice_client
        if vc and vc.source:
            vc.source.volume = new_volume
            
        await interaction.response.send_message(f"🔉 Громкость: {int(new_volume * 100)}%", ephemeral=True)
        await self.update_message(interaction)
        
    @ui.button(emoji="🔊", style=discord.ButtonStyle.secondary)
    async def volume_up(self, interaction: discord.Interaction, button: ui.Button):
        guild_id = interaction.guild.id
        current_volume = volume_levels.get(guild_id, 1.0)
        new_volume = min(2.0, current_volume + 0.1)
        volume_levels[guild_id] = new_volume
        
        vc = interaction.guild.voice_client
        if vc and vc.source:
            vc.source.volume = new_volume
            
        await interaction.response.send_message(f"🔊 Громкость: {int(new_volume * 100)}%", ephemeral=True)
        await self.update_message(interaction)
        
    @ui.button(emoji="🗑️", style=discord.ButtonStyle.danger)
    async def stop_player(self, interaction: discord.Interaction, button: ui.Button):
        vc = interaction.guild.voice_client
        if vc:
            if vc.is_playing() or vc.is_paused():
                vc.stop()
            await vc.disconnect()
            
            guild_id = interaction.guild.id
            if guild_id in queues:
                queues[guild_id].clear()
            if guild_id in now_playing:
                del now_playing[guild_id]
            
            await interaction.response.send_message("⏹️ Воспроизведение остановлено и бот отключен")
        else:
            await interaction.response.send_message("❌ Бот не подключен к голосовому каналу", ephemeral=True)
    
    async def update_message(self, interaction: discord.Interaction):
        guild_id = interaction.guild.id
        if guild_id in player_controls:
            message = player_controls[guild_id]
            try:
                await message.edit(view=self)
            except:
                pass

intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True

bot = commands.Bot(command_prefix='!', intents=intents)

@bot.event
async def on_ready():
    print(f'Бот {bot.user} запущен!')
    try:
        synced = await bot.tree.sync()
        print(f"Синхронизировано {len(synced)} команд")
    except Exception as e:
        print(f"Ошибка синхронизации: {e}")

async def play_next(guild):
    guild_id = guild.id
    if guild_id in queues and queues[guild_id]:
        next_track = queues[guild_id].popleft()
        now_playing[guild_id] = next_track
        
        vc = guild.voice_client
        if not vc:
            return
            
        volume = volume_levels.get(guild_id, 1.0)
        
        try:
            # Проверяем наличие аудио URL
            if not next_track.audio_url:
                raise ValueError(f"Аудио URL не найден для трека: {next_track.title}")
                
            source = discord.FFmpegPCMAudio(
                next_track.audio_url,
                before_options='-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5',
                options='-vn'
            )
            
            # Добавляем контроль громкости
            source = discord.PCMVolumeTransformer(source, volume=volume)
            
            def after_playing(error):
                if error:
                    print(f"Ошибка воспроизведения: {error}")
                asyncio.run_coroutine_threadsafe(play_next(guild), bot.loop)
            
            vc.play(source, after=after_playing)
            
            # Обновляем сообщение с контролами
            if guild_id in player_controls:
                try:
                    message = player_controls[guild_id]
                    view = PlayerControls(guild_id)
                    await message.edit(content=f"🎵 **Сейчас играет:** [{next_track.title}]({next_track.url})", embed=next_track.get_embed(), view=view)
                except Exception as e:
                    print(f"Ошибка обновления сообщения: {e}")
        except Exception as e:
            print(f"Ошибка воспроизведения: {e}")
            # Пробуем следующий трек
            await asyncio.sleep(1)
            await play_next(guild)
    else:
        # Очищаем состояние при пустой очереди
        if guild_id in now_playing:
            del now_playing[guild_id]
        if guild_id in player_controls:
            try:
                message = player_controls[guild_id]
                await message.edit(content="🎵 Очередь воспроизведения пуста", view=None, embed=None)
            except:
                pass

@bot.tree.command(name="play", description="Воспроизвести YouTube видео или добавить в очередь")
@app_commands.describe(url="Ссылка на YouTube видео или Shorts")
async def play(interaction: discord.Interaction, url: str):
    await interaction.response.defer()
    
    user = interaction.user
    if not user.voice:
        return await interaction.followup.send("❌ Пожалуйста, зайдите в голосовой канал!")
    
    voice_channel = user.voice.channel
    guild_id = interaction.guild.id
    
    if guild_id not in queues:
        queues[guild_id] = deque()
    if guild_id not in volume_levels:
        volume_levels[guild_id] = 1.0
    
    try:
        # Подключение к каналу
        vc = interaction.guild.voice_client
        if not vc:
            vc = await voice_channel.connect()
        elif vc.channel != voice_channel:
            await vc.move_to(voice_channel)
        
        # Загрузка информации о треке
        with youtube_dl.YoutubeDL(YTDL_OPTIONS) as ydl:
            info = ydl.extract_info(url, download=False)
            track = Track(info, url)
        
        # Добавляем трек в очередь
        queues[guild_id].append(track)
        queue_position = len(queues[guild_id])
        
        # Если ничего не играет, начинаем воспроизведение
        if not vc.is_playing() and not vc.is_paused():
            await play_next(interaction.guild)
            response = f"🎵 **Начинаю воспроизведение:** [{track.title}]({url})"
        else:
            response = f"🎵 **Добавлено в очередь (#{queue_position}):** [{track.title}]({url})"
        
        # Создаем или обновляем сообщение с контролами
        view = PlayerControls(guild_id)
        if guild_id in now_playing:
            current_track = now_playing[guild_id]
            embed = current_track.get_embed()
            content = f"🎵 **Сейчас играет:** [{current_track.title}]({current_track.url})"
        else:
            embed = None
            content = response
        
        if guild_id in player_controls:
            try:
                message = player_controls[guild_id]
                await message.edit(content=content, embed=embed, view=view)
                await interaction.followup.send(response)
            except:
                message = await interaction.followup.send(content, embed=embed, view=view)
                player_controls[guild_id] = message
        else:
            message = await interaction.followup.send(content, embed=embed, view=view)
            player_controls[guild_id] = message
        
    except Exception as e:
        await interaction.followup.send(f"❌ Ошибка: {str(e)}")
        import traceback
        traceback.print_exc()

@bot.tree.command(name="queue", description="Показать текущую очередь воспроизведения")
async def show_queue(interaction: discord.Interaction):
    guild_id = interaction.guild.id
    if guild_id not in queues or not queues[guild_id]:
        return await interaction.response.send_message("🎵 Очередь воспроизведения пуста")
    
    queue_list = []
    for i, track in enumerate(queues[guild_id], 1):
        queue_list.append(f"{i}. [{track.title}]({track.url})")
    
    embed = discord.Embed(
        title="Очередь воспроизведения",
        description="\n".join(queue_list[:10]),
        color=discord.Color.green()
    )
    
    if len(queues[guild_id]) > 10:
        embed.set_footer(text=f"И ещё {len(queues[guild_id]) - 10} треков...")
    
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="skip", description="Пропустить текущий трек")
async def skip(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if not vc or not vc.is_playing():
        return await interaction.response.send_message("❌ Сейчас ничего не играет", ephemeral=True)
    
    vc.stop()
    await interaction.response.send_message("⏭️ Трек пропущен")

@bot.tree.command(name="pause", description="Приостановить воспроизведение")
async def pause(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if not vc or not vc.is_playing():
        return await interaction.response.send_message("❌ Сейчас ничего не играет", ephemeral=True)
    
    if vc.is_paused():
        return await interaction.response.send_message("⏸️ Воспроизведение уже приостановлено", ephemeral=True)
    
    vc.pause()
    await interaction.response.send_message("⏸️ Воспроизведение приостановлено")

@bot.tree.command(name="resume", description="Возобновить воспроизведение")
async def resume(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if not vc:
        return await interaction.response.send_message("❌ Бот не подключен к голосовому каналу", ephemeral=True)
    
    if not vc.is_paused():
        return await interaction.response.send_message("▶️ Воспроизведение уже идет", ephemeral=True)
    
    vc.resume()
    await interaction.response.send_message("▶️ Воспроизведение возобновлено")

@bot.tree.command(name="volume", description="Установить громкость (0-200%)")
@app_commands.describe(level="Уровень громкости (0-200)")
async def set_volume(interaction: discord.Interaction, level: int):
    guild_id = interaction.guild.id
    volume = max(0, min(200, level)) / 100.0
    volume_levels[guild_id] = volume
    
    vc = interaction.guild.voice_client
    if vc and vc.source:
        vc.source.volume = volume
    
    await interaction.response.send_message(f"🔊 Громкость установлена на {level}%")
    
    if guild_id in player_controls:
        try:
            message = player_controls[guild_id]
            view = PlayerControls(guild_id)
            await message.edit(view=view)
        except:
            pass

@bot.tree.command(name="stop", description="Остановить воспроизведение и очистить очередь")
async def stop(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    guild_id = interaction.guild.id
    
    if vc:
        if vc.is_playing() or vc.is_paused():
            vc.stop()
        
        await vc.disconnect()
        
        if guild_id in queues:
            queues[guild_id].clear()
        if guild_id in now_playing:
            del now_playing[guild_id]
        
        if guild_id in player_controls:
            try:
                message = player_controls[guild_id]
                await message.delete()
                del player_controls[guild_id]
            except:
                pass
        
        await interaction.response.send_message("⏹️ Воспроизведение остановлено и бот отключен")
    else:
        await interaction.response.send_message("❌ Бот не подключен к голосовому каналу", ephemeral=True)

@bot.tree.command(name="nowplaying", description="Показать текущий трек")
async def now_playing_cmd(interaction: discord.Interaction):
    guild_id = interaction.guild.id
    if guild_id in now_playing:
        track = now_playing[guild_id]
        embed = track.get_embed()
        await interaction.response.send_message(embed=embed)
    else:
        await interaction.response.send_message("❌ Сейчас ничего не играет", ephemeral=True)

bot.run('Ваш токен для бота')