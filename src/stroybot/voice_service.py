"""Private Russian ASR. PCM streaming never leaves the owner's servers."""

import asyncio
import json
import os
import tempfile
import time
from pathlib import Path

from aiohttp import WSMsgType, web
from vosk import KaldiRecognizer, Model, SetLogLevel

SetLogLevel(-1)
MODEL = Model(os.environ.get("VOSK_MODEL_PATH", str(Path.cwd() / ".local/models/vosk-model-small-ru-0.22")))
MAX_SECONDS = 180
MAX_PCM = MAX_SECONDS * 32000
slot = asyncio.Lock()


def result(rec, audio, final=False):
    if final:
        return json.loads(rec.FinalResult()).get("text", ""), True
    complete = rec.AcceptWaveform(audio)
    data = json.loads(rec.Result() if complete else rec.PartialResult())
    return data.get("text" if complete else "partial", ""), complete


async def streaming(request):
    if slot.locked():
        return web.json_response(
            {"error": "Распознавание занято. Повторите через несколько секунд."}, status=429
        )
    async with slot:
        ws = web.WebSocketResponse(max_msg_size=65536, heartbeat=20)
        await ws.prepare(request)
        rec = KaldiRecognizer(MODEL, 16000)
        confirmed = []
        count = 0
        started = time.monotonic()
        await ws.send_json({"ready": True, "sample_rate": 16000, "max_seconds": MAX_SECONDS})
        try:
            while not ws.closed:
                message = await asyncio.wait_for(ws.receive(), timeout=20)
                if time.monotonic() - started > MAX_SECONDS + 20:
                    raise ValueError("Запись ограничена тремя минутами.")
                if message.type == WSMsgType.BINARY:
                    if len(message.data) % 2:
                        raise ValueError("Некорректный аудиофрагмент.")
                    count += len(message.data)
                    if count > MAX_PCM:
                        raise ValueError("Запись ограничена тремя минутами.")
                    text, complete = await asyncio.to_thread(result, rec, message.data)
                    if complete and text:
                        confirmed.append(text)
                    await ws.send_json(
                        {
                            "text": " ".join(confirmed + ([text] if text and not complete else [])),
                            "final": False,
                        }
                    )
                elif message.type == WSMsgType.TEXT:
                    data = json.loads(message.data)
                    if data.get("eof"):
                        text, _ = await asyncio.to_thread(result, rec, b"", True)
                        if text:
                            confirmed.append(text)
                        await ws.send_json({"text": " ".join(confirmed), "final": True})
                        break
                else:
                    break
        except (TimeoutError, ValueError, json.JSONDecodeError) as error:
            await ws.send_json({"error": str(error) or "Запись прервана. Попробуйте ещё раз."})
        finally:
            await ws.close()
        return ws


async def transcribe(request):
    if slot.locked():
        return web.json_response(
            {"error": "Распознавание занято. Попробуйте через несколько секунд."}, status=429
        )
    async with slot:
        audio = await request.read()
        if not audio:
            return web.json_response({"error": "Аудиозапись пуста."}, status=400)
        with tempfile.TemporaryDirectory(prefix="m100-audio-") as temp:
            source = Path(temp) / "audio"
            pcm = Path(temp) / "audio.pcm"
            source.write_bytes(audio)
            process = await asyncio.create_subprocess_exec(
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-nostdin",
                "-i",
                str(source),
                "-t",
                str(MAX_SECONDS + 1),
                "-ac",
                "1",
                "-ar",
                "16000",
                "-f",
                "s16le",
                str(pcm),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            try:
                await asyncio.wait_for(process.wait(), 45)
            except TimeoutError:
                process.kill()
                await process.wait()
                return web.json_response({"error": "Обработка аудио превысила время ожидания."}, status=400)
            if pcm.exists() and pcm.stat().st_size > MAX_PCM:
                return web.json_response(
                    {"error": "Запись длиннее трёх минут. Разделите её на части."}, status=400
                )
            if process.returncode or not pcm.exists():
                return web.json_response({"error": "Не удалось прочитать формат аудиозаписи."}, status=400)
            rec = KaldiRecognizer(MODEL, 16000)
            words = []
            with pcm.open("rb") as stream:
                while chunk := stream.read(8000):
                    text, complete = await asyncio.to_thread(result, rec, chunk)
                    if text and complete:
                        words.append(text)
            text, _ = await asyncio.to_thread(result, rec, b"", True)
            if text:
                words.append(text)
            return web.json_response({"text": " ".join(words)})


async def health(request):
    return web.json_response({"status": "ok", "engine": "vosk", "language": "ru", "busy": slot.locked()})


app = web.Application(client_max_size=19 * 1024 * 1024)
app.add_routes(
    [web.get("/health", health), web.get("/stream", streaming), web.post("/transcribe", transcribe)]
)
if __name__ == "__main__":
    web.run_app(app, host="127.0.0.1", port=int(os.getenv("SPEECH_PORT", "8766")), access_log=None, print=None)
