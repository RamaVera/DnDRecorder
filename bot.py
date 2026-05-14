import asyncio
import sys
import os
import datetime
import traceback
import logging
import discord
from discord.ext import commands, voice_recv

with open(".env", "r") as f:
    for line in f:
        if "=" in line:
            key, value = line.strip().split("=", 1)
            os.environ[key] = value

TOKEN = os.environ["TOKEN"]

# 1. PARCHE CRÍTICO PARA WINDOWS
if sys.platform == 'win32':
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

# Configuración de logs para ver qué pasa con la voz
logging.basicConfig(level=logging.INFO)

intents = discord.Intents.all()
bot = commands.Bot(command_prefix="!", intents=intents)

AUDIO_DIR = "grabaciones"
os.makedirs(AUDIO_DIR, exist_ok=True)


@bot.event
async def on_ready():
    print("=================================")
    print(f"BOT ONLINE: {bot.user}")
    print("=================================")


# =========================
# COMANDO JOIN (CORREGIDO)
# =========================
@bot.command()
async def join(ctx):
    print("\n--- INICIANDO JOIN ---")

    if ctx.author.voice is None:
        return await ctx.send("❌ Tenés que estar en un canal de voz.")

    channel = ctx.author.voice.channel

    # Limpieza de conexiones previas
    existing_vc = discord.utils.get(bot.voice_clients, guild=ctx.guild)
    if existing_vc:
        await existing_vc.disconnect(force=True)
        await asyncio.sleep(1)

    try:
        print(f"Intentando conectar a: {channel}")

        # EL CAMBIO CLAVE: Usar VoiceRecvClient para poder usar .listen() después
        vc = await channel.connect(cls=voice_recv.VoiceRecvClient, timeout=20.0, reconnect=True)

        print(f"Conectado exitosamente: {vc}")
        await ctx.send(f"🎙️ Unido a **{channel.name}** (Modo Grabación Activo)")

    except Exception as e:
        print(f"ERROR EN JOIN: {e}")
        traceback.print_exc()
        await ctx.send(f"❌ Error al conectar.")


# =========================
# COMANDO START (CORREGIDO)
# =========================
class SafeWaveSink(voice_recv.WaveSink):
    def write(self, user, data):
        try:
            # Si el paquete de audio está bien, lo escribe
            super().write(user, data)
        except Exception:
            # Si el paquete viene corrupto (el error Opus), lo ignoramos
            # para que el proceso no muera
            pass


class RawOpusSink(voice_recv.AudioSink):
    def __init__(self, filename):
        super().__init__()
        # Abrimos un archivo simple, no un .wav, porque el audio estará cifrado/comprimido
        self.file = open(filename, 'wb')

    def wants_opus(self) -> bool:
        # Esto saltea el decodificador que está fallando
        return True

    def write(self, user, data):
        # Escribimos los bytes de Opus directamente
        if data.opus:
            self.file.write(data.opus)

    def cleanup(self):
        self.file.close()


@bot.command()
async def start(ctx):
    vc = ctx.voice_client
    if not vc or not isinstance(vc, voice_recv.VoiceRecvClient):
        return await ctx.send("¡Uní al bot con !join primero!")

    ahora = datetime.datetime.now().strftime("%H%M%S")
    # Guardamos como .opus para procesarlo después
    filepath = os.path.join(AUDIO_DIR, f"raw_audio_{ahora}.opus")

    try:
        sink = RawOpusSink(filepath)

        await ctx.send("📡 Iniciando recepción de paquetes crudos...")
        await asyncio.sleep(2)

        vc.listen(sink)
        await ctx.send(f"🔴 **Grabando bytes.** Archivo: `{filepath}`")
    except Exception as e:
        await ctx.send(f"Error: {e}")



# =========================
# COMANDO STOP
# =========================
@bot.command()
async def stop(ctx):
    vc = ctx.voice_client
    if vc and isinstance(vc, voice_recv.VoiceRecvClient) and vc.is_listening():
        vc.stop_listening()
        await ctx.send("⏹️ Grabación finalizada. Podés encontrar el archivo en la carpeta `/grabaciones`.")
    else:
        await ctx.send("❌ No se está grabando nada actualmente.")


# =========================
# COMANDO LEAVE
# =========================
@bot.command()
async def leave(ctx):
    vc = ctx.voice_client
    if vc:
        if vc.is_listening():
            vc.stop_listening()
        await vc.disconnect()
        await ctx.send("👋 Desconectado y limpieza realizada.")
    else:
        await ctx.send("❌ No estoy en ningún canal.")


bot.run(TOKEN)