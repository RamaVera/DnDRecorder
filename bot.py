import asyncio
import sys
import os
import struct
import wave
import datetime
import traceback
import logging
import discord
from discord.ext import commands, voice_recv
import discord.gateway

# ─────────────────────────────────────────────────────
# DAVE (Discord Audio Video Encryption) — davey binding
# ─────────────────────────────────────────────────────
# pip install davey
try:
    import davey
    DAVE_AVAILABLE = True
except ImportError:
    DAVE_AVAILABLE = False
    logging.warning("⚠️  davey no instalado. Corré: pip install davey")

SILENCE_FRAME = bytes([0xF8, 0xFF, 0xFE])

# Opcodes DAVE en el Voice Gateway
DAVE_PREPARE_TRANSITION         = 21
DAVE_EXECUTE_TRANSITION         = 22
DAVE_TRANSITION_READY           = 23
DAVE_PREPARE_EPOCH              = 24
MLS_EXTERNAL_SENDER             = 25
MLS_KEY_PACKAGE                 = 26
MLS_PROPOSALS                   = 27
MLS_COMMIT_WELCOME              = 28
MLS_ANNOUNCE_COMMIT_TRANSITION  = 29
MLS_WELCOME                     = 30
MLS_INVALID_COMMIT_WELCOME      = 31
DAVE_OPCODES = set(range(21, 32))


# ─────────────────────────────────────────────────────
# Sesión DAVE por guild
# ─────────────────────────────────────────────────────
_dave_sessions: dict[int, "DAVESession"] = {}  # guild_id → DAVESession


class DAVESession:
    def __init__(self, user_id: int, channel_id: int):
        self.user_id = user_id
        self.channel_id = channel_id
        self.ready = False
        self._session = None
        self._init()

    def _init(self):
        if not DAVE_AVAILABLE:
            return
        try:
            self._session = davey.DaveSession(1, self.user_id, self.channel_id)
            logging.info(f"DAVESession creada (user={self.user_id}, channel={self.channel_id})")
        except Exception as e:
            logging.error(f"Error creando DAVESession: {e}")

    def set_external_sender(self, data: bytes):
        if self._session:
            try:
                self._session.set_external_sender(data)
            except Exception as e:
                logging.error(f"DAVE set_external_sender error: {e}")

    def get_key_package(self) -> bytes:
        if self._session:
            try:
                return self._session.get_serialized_key_package()
            except Exception as e:
                logging.error(f"DAVE get_key_package error: {e}")
        return b""

    def process_proposals(self, op_type: int, data: bytes, user_ids: list):
        if self._session:
            try:
                return self._session.process_proposals(op_type, data, user_ids)
            except Exception as e:
                logging.error(f"DAVE process_proposals error: {e}")
        return None

    def process_commit(self, data: bytes):
        if self._session:
            try:
                self._session.process_commit(data)
                self.ready = True
                logging.info("✅ DAVE sesión lista (process_commit)")
            except Exception as e:
                logging.error(f"DAVE process_commit error: {e}")

    def process_welcome(self, data: bytes):
        if self._session:
            try:
                self._session.process_welcome(data)
                self.ready = True
                logging.info("✅ DAVE sesión lista (process_welcome)")
            except Exception as e:
                logging.error(f"DAVE process_welcome error: {e}")

    def decrypt(self, user_id: int, opus_data: bytes) -> bytes:
        if self._session and self.ready:
            try:
                return self._session.decrypt(user_id, davey.MediaType.audio, opus_data)
            except Exception as e:
                logging.debug(f"DAVE decrypt error (user {user_id}): {e}")
        return opus_data

    def reset(self):
        logging.warning("DAVE: reset de sesión")
        self.ready = False
        self._init()


# ─────────────────────────────────────────────────────
# Patch del Voice Gateway para manejar opcodes DAVE
#
# Discord envía los mensajes DAVE como:
#   - Frames de texto JSON  → opcode en campo "op"
#   - Frames binarios       → opcode embebido en los bytes
#
# TODO: validar formato exacto de frames binarios con
#       tráfico real. Formato probable basado en Craig:
#       [1 byte opcode][2 bytes header][payload...]
# ─────────────────────────────────────────────────────

_orig_received_message = discord.gateway.DiscordVoiceWebSocket.received_message


async def _dave_received_message(self, msg):
    if isinstance(msg, bytes):
        await _handle_dave_binary(self, msg)
        return

    op = msg.get("op")

    if op == 4:
        await _on_session_description(self)
    elif op in DAVE_OPCODES:
        await _handle_dave_json(self, op, msg.get("d", {}))
        return

    await _orig_received_message(self, msg)


async def _on_session_description(ws):
    guild_id = _get_guild_id(ws)
    if not guild_id or guild_id in _dave_sessions:
        return
    if not DAVE_AVAILABLE:
        return
    user_id = getattr(ws, "_user_id", 0) or 0
    channel_id = getattr(ws, "_channel_id", 0) or 0
    _dave_sessions[guild_id] = DAVESession(user_id, channel_id)
    logging.info(f"DAVESession registrada para guild={guild_id}")


async def _handle_dave_json(ws, op: int, data: dict):
    guild_id = _get_guild_id(ws)
    session = _dave_sessions.get(guild_id)

    if op == DAVE_PREPARE_EPOCH:
        version = data.get("protocol_version", 1)
        logging.info(f"DAVE: preparando epoch v{version}")
        if version == 0 and session:
            session.ready = False
    elif op == DAVE_EXECUTE_TRANSITION:
        logging.info("DAVE: ejecutando transición")
    elif op == DAVE_PREPARE_TRANSITION:
        logging.info("DAVE: preparando transición")


async def _handle_dave_binary(ws, data: bytes):
    if len(data) < 2:
        return

    opcode = data[0]
    guild_id = _get_guild_id(ws)
    session = _dave_sessions.get(guild_id)

    logging.info(f"DAVE binary: opcode={opcode}, len={len(data)}, guild={guild_id}")

    if not session:
        logging.warning("DAVE binary: no hay sesión activa")
        return

    if opcode == MLS_EXTERNAL_SENDER:
        logging.info("DAVE: MLS_EXTERNAL_SENDER")
        session.set_external_sender(data[3:])

    elif opcode == MLS_KEY_PACKAGE:
        logging.info("DAVE: MLS_KEY_PACKAGE → enviando key package")
        key_pkg = session.get_key_package()
        if key_pkg:
            await ws.send(struct.pack("!B", MLS_KEY_PACKAGE) + key_pkg)

    elif opcode == MLS_PROPOSALS:
        logging.info("DAVE: MLS_PROPOSALS")
        if len(data) < 4:
            return
        op_type = data[3]
        result = session.process_proposals(op_type, data[4:], [])
        if result:
            commit_payload = result.commit
            if hasattr(result, "welcome") and result.welcome:
                commit_payload += result.welcome
            await ws.send(struct.pack("!B", MLS_COMMIT_WELCOME) + commit_payload)

    elif opcode == MLS_ANNOUNCE_COMMIT_TRANSITION:
        logging.info("DAVE: MLS_ANNOUNCE_COMMIT_TRANSITION → sesión activa")
        if len(data) >= 5:
            transition_id = struct.unpack("!H", data[3:5])[0]
            session.process_commit(data[5:])
            await ws.send({"op": DAVE_TRANSITION_READY, "d": {"transition_id": transition_id}})

    elif opcode == MLS_WELCOME:
        logging.info("DAVE: MLS_WELCOME")
        session.process_welcome(data[5:] if len(data) > 5 else b"")

    elif opcode == MLS_INVALID_COMMIT_WELCOME:
        logging.warning("DAVE: commit inválido → reset sesión")
        session.reset()


def _get_guild_id(ws) -> int:
    for attr in ("_guild_id", "guild_id"):
        val = getattr(ws, attr, None)
        if val:
            return int(val)
    vc = getattr(ws, "_connection", None)
    if vc:
        guild = getattr(vc, "guild", None)
        if guild:
            return guild.id
    return 0


discord.gateway.DiscordVoiceWebSocket.received_message = _dave_received_message


# ─────────────────────────────────────────────────────
# Audio Sink con DAVE decryption
#
# wants_opus=True → recibimos Opus crudo (DAVE-encriptado)
# → decrypt con davey → decode Opus → PCM → WAV
# ─────────────────────────────────────────────────────

class DAVEWavSink(voice_recv.AudioSink):
    def __init__(self, filename: str, guild_id: int):
        super().__init__()
        self._guild_id = guild_id
        self._wav = wave.open(filename, "wb")
        self._wav.setnchannels(2)
        self._wav.setsampwidth(2)
        self._wav.setframerate(48000)
        self._decoders: dict[int, discord.opus.Decoder] = {}

    def wants_opus(self) -> bool:
        return DAVE_AVAILABLE

    def _get_decoder(self, user_id: int) -> discord.opus.Decoder:
        if user_id not in self._decoders:
            self._decoders[user_id] = discord.opus.Decoder()
        return self._decoders[user_id]

    def write(self, user, data):
        uid = user.id if user else 0

        if self.wants_opus():
            opus_data = data.opus
            if not opus_data or opus_data == SILENCE_FRAME:
                return
            session = _dave_sessions.get(self._guild_id)
            if session:
                opus_data = session.decrypt(uid, opus_data)
            try:
                pcm = self._get_decoder(uid).decode(opus_data, fec=False)
                self._wav.writeframes(pcm)
            except discord.opus.OpusError as e:
                logging.debug(f"Opus decode error (uid={uid}): {e}")
        else:
            if data.pcm:
                self._wav.writeframes(data.pcm)

    def cleanup(self):
        self._wav.close()


# ─────────────────────────────────────────────────────
# Bot
# ─────────────────────────────────────────────────────

with open(".env", "r") as f:
    for line in f:
        if "=" in line:
            key, value = line.strip().split("=", 1)
            os.environ[key] = value

TOKEN = os.environ["TOKEN"]

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

logging.basicConfig(level=logging.INFO)

intents = discord.Intents.all()
bot = commands.Bot(command_prefix="!", intents=intents)
AUDIO_DIR = "grabaciones"
os.makedirs(AUDIO_DIR, exist_ok=True)


@bot.event
async def on_ready():
    print("=================================")
    print(f"BOT ONLINE: {bot.user}")
    print(f"DAVE: {'✅ activo' if DAVE_AVAILABLE else '❌ pip install davey'}")
    print("=================================")


@bot.command()
async def join(ctx):
    print("\n--- INICIANDO JOIN ---")
    if ctx.author.voice is None:
        return await ctx.send("❌ Tenés que estar en un canal de voz.")

    channel = ctx.author.voice.channel

    existing = discord.utils.get(bot.voice_clients, guild=ctx.guild)
    if existing:
        await existing.disconnect(force=True)
        await asyncio.sleep(1)

    try:
        vc = await channel.connect(cls=voice_recv.VoiceRecvClient, timeout=20.0, reconnect=True)
        print(f"Conectado: {vc}")
        await ctx.send(f"🎙️ Unido a **{channel.name}**")
    except Exception as e:
        print(f"ERROR EN JOIN: {e}")
        traceback.print_exc()
        await ctx.send("❌ Error al conectar.")


@bot.command()
async def start(ctx):
    vc = ctx.voice_client
    if not vc or not isinstance(vc, voice_recv.VoiceRecvClient):
        return await ctx.send("¡Uní el bot con !join primero!")

    ahora = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    filepath = os.path.join(AUDIO_DIR, f"grabacion_{ahora}.wav")

    try:
        sink = DAVEWavSink(filepath, guild_id=ctx.guild.id)
        vc.listen(sink)
        await ctx.send(f"🔴 **Grabando.** Archivo: `{filepath}`")
    except Exception as e:
        await ctx.send(f"Error: {e}")


@bot.command()
async def stop(ctx):
    vc = ctx.voice_client
    if vc and isinstance(vc, voice_recv.VoiceRecvClient) and vc.is_listening():
        vc.stop_listening()
        await ctx.send("⏹️ Grabación finalizada.")
    else:
        await ctx.send("❌ No estoy grabando.")


@bot.command()
async def leave(ctx):
    vc = ctx.voice_client
    if vc:
        if vc.is_listening():
            vc.stop_listening()
        _dave_sessions.pop(ctx.guild.id, None)
        await vc.disconnect()
        await ctx.send("👋 Desconectado.")
    else:
        await ctx.send("❌ No estoy en ningún canal.")


bot.run(TOKEN)
