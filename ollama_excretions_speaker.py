#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Ollama Excretions Speaker — lightweight reverse-proxy that voices all streamed outputs.
- Speaks incrementally using Kokoro-TTS or KittenTTS (nano or mini).
- One file. Headless by default; optional Tk GUI (--gui).
- Local-only or LAN-wide (by pointing clients to this proxy).

Usage examples:
  python ollama_excretions_speaker.py --engine kokoro --listen 0.0.0.0:11435 --upstream http://127.0.0.1:11434
  python ollama_excretions_speaker.py --engine kitten-nano --gui

"""

import argparse, asyncio, json, os, re, sys, time
from typing import AsyncIterator, Optional

import anyio
import httpx
from fastapi import FastAPI, Request, Response
from fastapi.responses import StreamingResponse, JSONResponse, PlainTextResponse

# ---------- Minimal, lazy TTS engines ----------

class BaseTTSEngine:
    def __init__(self, voice: Optional[str] = None):
        self.voice = voice
    async def speak(self, text: str):
        raise NotImplementedError
    async def close(self): pass

class KokoroEngine(BaseTTSEngine):
    """
    Kokoro TTS (kokoro-tts-82m). CPU-friendly.
    https://github.com/nazdridoy/kokoro-tts
    """
    def __init__(self, voice: Optional[str] = None):
        super().__init__(voice)
        from kokoro import TTS  # lazy import
        import sounddevice as sd
        self.tts = TTS()  # default model (82M)
        self.sd = sd
        self.sr = 24000

        # Kokoro voices vary; default to an English voice
        self.voice = voice or "af_bella"  # choose an available kokoro voice if installed

    async def speak(self, text: str):
        if not text.strip(): return
        # Kokoro returns float32 numpy array
        audio = self.tts.generate(text, voice=self.voice)  # sr=24000
        self.sd.play(audio, self.sr, blocking=True)

class KittenEngine(BaseTTSEngine):
    """
    KittenTTS (nano or mini). Ultra-light.
    https://huggingface.co/KittenML/kitten-tts-nano-0.2
    https://huggingface.co/KittenML/kitten-tts-mini-0.1
    """
    def __init__(self, variant: str = "nano", voice: Optional[str] = None):
        super().__init__(voice)
        from kittentts import KittenTTS  # lazy import
        import sounddevice as sd
        self.sd = sd
        self.sr = 24000
        if variant == "nano":
            model_id = "KittenML/kitten-tts-nano-0.2"
        elif variant == "mini":
            model_id = "KittenML/kitten-tts-mini-0.1"
        else:
            raise ValueError("Kitten variant must be 'nano' or 'mini'")
        self.tts = KittenTTS(model_id)
        # sensible default voice
        self.voice = voice or "expr-voice-2-f"

    async def speak(self, text: str):
        if not text.strip(): return
        audio = self.tts.generate(text, voice=self.voice)  # returns float32 np array @ 24k
        self.sd.play(audio, self.sr, blocking=True)

# ---------- sentence / clause chunker for low-latency playback ----------

_SENT_SPLIT = re.compile(r"([\.!?…]+[\s\"]+|[\n\r]+)")  # crude but effective

class ClauseSpeaker:
    def __init__(self, engine: BaseTTSEngine, min_chars=40, max_delay=0.8):
        self.engine = engine
        self.buffer = ""
        self.last_flush = time.monotonic()
        self.min_chars = min_chars
        self.max_delay = max_delay

    async def feed(self, piece: str):
        self.buffer += piece
        # flush on sentence boundary or after a small delay
        out_chunks = []
        # split by sentences but keep delimiters
        parts = _SENT_SPLIT.split(self.buffer)
        # everything except the tail is complete
        if len(parts) > 1:
            complete = "".join(parts[:-1])
            tail = parts[-1]
        else:
            complete = ""
            tail = self.buffer

        if complete.strip():
            out_chunks.append(complete)
        # time-based fallback to keep latency down
        if (time.monotonic() - self.last_flush) > self.max_delay and len(tail) > self.min_chars:
            # carve a clause end if possible (comma/semicolon)
            m = re.search(r".*[,;:]+[\s]+", tail)
            if m:
                out_chunks.append(m.group(0))
                tail = tail[m.end():]

        # speak all complete chunks now
        for chunk in out_chunks:
            await self.engine.speak(chunk)
            self.last_flush = time.monotonic()
        # retain tail
        self.buffer = tail

    async def flush(self):
        if self.buffer.strip():
            await self.engine.speak(self.buffer)
        self.buffer = ""

# ---------- Reverse proxy with streaming interception ----------

class Proxy:
    def __init__(self, upstream: str, speaker: ClauseSpeaker, gui_hook=None):
        self.upstream = upstream.rstrip("/")
        self.client = httpx.AsyncClient(base_url=self.upstream, timeout=None)
        self.speaker = speaker
        self.gui_hook = gui_hook

    async def stream_proxy(self, req: Request, path: str) -> StreamingResponse:
        # Forward headers/body to upstream
        body = await req.body()
        # Ensure stream=true; we want tokens
        try:
            j = json.loads(body) if body else {}
            if isinstance(j, dict) and j.get("stream") is False:
                j["stream"] = True
                body = json.dumps(j).encode("utf-8")
        except Exception:
            pass

        upstream_resp = await self.client.stream(
            req.method, path, headers={k:v for k,v in req.headers.items() if k.lower() != "host"}, content=body
        )

        async def iterator() -> AsyncIterator[bytes]:
            async with upstream_resp:
                async for chunk in upstream_resp.aiter_bytes():
                    # Pass through to client immediately
                    yield chunk
                    # Try to parse SSE lines containing JSON with "message": {"content": "..."} or "response":"..."
                    try:
                        # We parse per line; chunk may contain multiple lines
                        for line in chunk.splitlines():
                            if not line.startswith(b"data:"): 
                                continue
                            data = line[5:].strip()
                            if not data or data == b"[DONE]":
                                continue
                            obj = json.loads(data)
                            # Two common shapes:
                            # /api/chat: {"message":{"content":"token"} ...}
                            # /api/generate: {"response":"token", ...}
                            token = None
                            if "message" in obj and isinstance(obj["message"], dict):
                                token = obj["message"].get("content")
                            if token is None:
                                token = obj.get("response")
                            if token:
                                if self.gui_hook:
                                    self.gui_hook(token)
                                await self.speaker.feed(token)
                    except Exception:
                        # swallow parsing errors; keep stream alive
                        pass
            # final flush
            await self.speaker.flush()

        # preserve streaming headers
        headers = dict(upstream_resp.headers)
        return StreamingResponse(iterator(), status_code=upstream_resp.status_code, headers=headers)

    async def json_proxy(self, req: Request, path: str) -> Response:
        # Non-stream fallbacks (tags, embeddings, etc.)
        body = await req.body()
        r = await self.client.request(req.method, path, headers={k:v for k,v in req.headers.items() if k.lower() != "host"}, content=body)
        return Response(content=r.content, status_code=r.status_code, headers=dict(r.headers))

# ---------- FastAPI app ----------

def build_app(proxy: Proxy) -> FastAPI:
    app = FastAPI(title="Ollama Excretions Speaker")

    @app.api_route("/{full_path:path}", methods=["GET","POST","PUT","PATCH","DELETE","OPTIONS"])
    async def catch_all(req: Request, full_path: str):
        path = "/" + full_path
        # Intercept only chat/generate (streaming)
        if path.startswith("/api/chat") or path.startswith("/api/generate"):
            return await proxy.stream_proxy(req, path)
        return await proxy.json_proxy(req, path)

    @app.get("/")
    async def root():
        return JSONResponse({"ok": True, "upstream": proxy.upstream})
    return app

# ---------- Optional Tk GUI ----------
def start_gui(get_queue_fn):
    import tkinter as tk
    from tkinter.scrolledtext import ScrolledText

    root = tk.Tk()
    root.title("Ollama Excretions Speaker")
    text = ScrolledText(root, height=18, width=100, bg="black", fg="red")
    text.pack(fill="both", expand=True)

    # very simple level meter: redraws a red bar by recent length
    canvas = tk.Canvas(root, height=40, bg="black", highlightthickness=0)
    canvas.pack(fill="x")
    width = [0]

    def tick():
        try:
            q = get_queue_fn()
            if q:
                text.insert("end", q)
                text.see("end")
                # crude level: recent character burst
                width[0] = min(canvas.winfo_width(), width[0] + len(q)*2)
        except Exception:
            pass
        canvas.delete("all")
        canvas.create_rectangle(0, 0, max(5, width[0]), 40, fill="red", outline="")
        # decay
        width[0] = max(0, width[0]-6)
        root.after(50, tick)

    root.after(100, tick)
    root.mainloop()

# ---------- Main ----------

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--listen", default="127.0.0.1:11435", help="Proxy bind (host:port)")
    p.add_argument("--upstream", default=os.environ.get("UPSTREAM", "http://127.0.0.1:11434"),
                   help="Real Ollama server base URL")
    p.add_argument("--engine", default="kokoro", choices=["kokoro","kitten-nano","kitten-mini"])
    p.add_argument("--voice", default=None, help="Engine-specific voice id")
    p.add_argument("--gui", action="store_true", help="Launch minimal Tk GUI")
    args = p.parse_args()

    # engine selection
    if args.engine == "kokoro":
        engine = KokoroEngine(voice=args.voice)
    elif args.engine == "kitten-nano":
        engine = KittenEngine(variant="nano", voice=args.voice)
    else:
        engine = KittenEngine(variant="mini", voice=args.voice)

    gui_queue = []
    def gui_hook(token: str):
        gui_queue.append(token)

    speaker = ClauseSpeaker(engine)
    proxy = Proxy(upstream=args.upstream, speaker=speaker, gui_hook=(gui_hook if args.gui else None))
    app = build_app(proxy)

    host, port = args.listen.split(":")
    import uvicorn
    if args.gui:
        # run API in background and start Tk
        async def runner():
            config = uvicorn.Config(app, host=host, port=int(port), log_level="info", loop="asyncio")
            server = uvicorn.Server(config)
            await server.serve()
        def drain():
            s = "".join(gui_queue)
            gui_queue.clear()
            return s
        import threading
        t = threading.Thread(target=lambda: asyncio.run(runner()), daemon=True)
        t.start()
        start_gui(drain)
    else:
        uvicorn.run(app, host=host, port=int(port), log_level="info")

if __name__ == "__main__":
    main()
