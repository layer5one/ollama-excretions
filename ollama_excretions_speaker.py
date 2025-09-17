#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Cleaned Ollama Excretions Speaker
- Uses KittenTTS or Kokoro-CLI
- CLI-compatible, GUI-ready
- Auto-speaks streamed Ollama token output
"""

import argparse, asyncio, json, os, re, subprocess, time
from typing import AsyncIterator, Optional

import anyio
import httpx
from fastapi import FastAPI, Request, Response
from fastapi.responses import StreamingResponse, JSONResponse


# --- TTS Engines ---

class BaseTTSEngine:
    def __init__(self, voice: Optional[str] = None):
        self.voice = voice
    async def speak(self, text: str): raise NotImplementedError
    async def close(self): pass

class KokoroEngine(BaseTTSEngine):
    def __init__(self, voice: Optional[str] = None):
        super().__init__(voice or "af_bella")
    async def speak(self, text: str):
        if not text.strip(): return
        subprocess.run([
            "kokoro-tts", "--text", text, "--voice", self.voice
        ], check=True)

class KittenEngine(BaseTTSEngine):
    def __init__(self, variant: str = "nano", voice: Optional[str] = None):
        super().__init__(voice or "expr-voice-2-f")
        from kittentts import KittenTTS
        import sounddevice as sd
        self.sd = sd
        self.sr = 24000
        self.tts = KittenTTS(
            "KittenML/kitten-tts-nano-0.2" if variant == "nano"
            else "KittenML/kitten-tts-mini-0.1"
        )
    async def speak(self, text: str):
        if not text.strip(): return
        audio = self.tts.generate(text, voice=self.voice)
        self.sd.play(audio, self.sr, blocking=True)

# --- Sentence Chunker ---

_SENT_SPLIT = re.compile(r"([\.\!?…]+[\s\"]+|[\n\r]+)")

class ClauseSpeaker:
    def __init__(self, engine: BaseTTSEngine, min_chars=40, max_delay=0.8):
        self.engine = engine
        self.buffer = ""
        self.last_flush = time.monotonic()
        self.min_chars = min_chars
        self.max_delay = max_delay
    async def feed(self, piece: str):
        self.buffer += piece
        out_chunks = []
        parts = _SENT_SPLIT.split(self.buffer)
        if len(parts) > 1:
            complete = "".join(parts[:-1])
            tail = parts[-1]
        else:
            complete, tail = "", self.buffer
        if complete.strip(): out_chunks.append(complete)
        if (time.monotonic() - self.last_flush) > self.max_delay and len(tail) > self.min_chars:
            m = re.search(r".*[,;:]+[\s]+", tail)
            if m:
                out_chunks.append(m.group(0))
                tail = tail[m.end():]
        for chunk in out_chunks:
            await self.engine.speak(chunk)
            self.last_flush = time.monotonic()
        self.buffer = tail
    async def flush(self):
        if self.buffer.strip():
            await self.engine.speak(self.buffer)
        self.buffer = ""

# --- Reverse Proxy ---

class Proxy:
    def __init__(self, upstream: str, speaker: ClauseSpeaker, gui_hook=None):
        self.upstream = upstream.rstrip("/")
        self.client = httpx.AsyncClient(base_url=self.upstream, timeout=None)
        self.speaker = speaker
        self.gui_hook = gui_hook

    async def stream_proxy(self, req: Request, path: str) -> StreamingResponse:
        body = await req.body()
        try:
            j = json.loads(body) if body else {}
            if isinstance(j, dict) and j.get("stream") is False:
                j["stream"] = True
                body = json.dumps(j).encode("utf-8")
        except Exception: pass

        upstream_resp = await self.client.stream(
            req.method, path,
            headers={k:v for k,v in req.headers.items() if k.lower() != "host"},
            content=body
        )

        async def iterator() -> AsyncIterator[bytes]:
            async with upstream_resp:
                async for chunk in upstream_resp.aiter_bytes():
                    yield chunk
                    try:
                        for line in chunk.splitlines():
                            if not line.startswith(b"data:"): continue
                            data = line[5:].strip()
                            if not data or data == b"[DONE]": continue
                            obj = json.loads(data)
                            token = obj.get("response") or obj.get("message", {}).get("content")
                            if token:
                                if self.gui_hook: self.gui_hook(token)
                                await self.speaker.feed(token)
                    except Exception: pass
            await self.speaker.flush()

        return StreamingResponse(iterator(), status_code=upstream_resp.status_code, headers=dict(upstream_resp.headers))

    async def json_proxy(self, req: Request, path: str) -> Response:
        body = await req.body()
        r = await self.client.request(req.method, path, headers={k:v for k,v in req.headers.items() if k.lower() != "host"}, content=body)
        return Response(content=r.content, status_code=r.status_code, headers=dict(r.headers))

# --- FastAPI App ---

def build_app(proxy: Proxy) -> FastAPI:
    app = FastAPI(title="Ollama Excretions Speaker")

    @app.api_route("/{full_path:path}", methods=["GET","POST","PUT","PATCH","DELETE","OPTIONS"])
    async def catch_all(req: Request, full_path: str):
        path = "/" + full_path
        if path.startswith("/api/chat") or path.startswith("/api/generate"):
            return await proxy.stream_proxy(req, path)
        return await proxy.json_proxy(req, path)

    @app.get("/")
    async def root(): return JSONResponse({"ok": True, "upstream": proxy.upstream})
    return app

# --- Optional GUI ---

def start_gui(get_queue_fn):
    import tkinter as tk
    from tkinter.scrolledtext import ScrolledText
    root = tk.Tk()
    root.title("Ollama Excretions Speaker")
    text = ScrolledText(root, height=18, width=100, bg="black", fg="red")
    text.pack(fill="both", expand=True)
    canvas = tk.Canvas(root, height=40, bg="black", highlightthickness=0)
    canvas.pack(fill="x")
    width = [0]
    def tick():
        try:
            q = get_queue_fn()
            if q:
                text.insert("end", q)
                text.see("end")
                width[0] = min(canvas.winfo_width(), width[0] + len(q)*2)
        except: pass
        canvas.delete("all")
        canvas.create_rectangle(0, 0, max(5, width[0]), 40, fill="red", outline="")
        width[0] = max(0, width[0]-6)
        root.after(50, tick)
    root.after(100, tick)
    root.mainloop()

# --- Main Entry ---

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--listen", default="127.0.0.1:11435")
    p.add_argument("--upstream", default=os.environ.get("UPSTREAM", "http://127.0.0.1:11434"))
    p.add_argument("--engine", default="kokoro", choices=["kokoro","kitten-nano","kitten-mini"])
    p.add_argument("--voice", default=None)
    p.add_argument("--gui", action="store_true")
    args = p.parse_args()

    if args.engine == "kokoro":
        engine = KokoroEngine(voice=args.voice)
    else:
        variant = "nano" if args.engine == "kitten-nano" else "mini"
        engine = KittenEngine(variant=variant, voice=args.voice)

    gui_queue = []
    def gui_hook(token: str): gui_queue.append(token)
    speaker = ClauseSpeaker(engine)
    proxy = Proxy(upstream=args.upstream, speaker=speaker, gui_hook=(gui_hook if args.gui else None))
    app = build_app(proxy)

    import uvicorn
    host, port = args.listen.split(":")
    if args.gui:
        async def runner():
            config = uvicorn.Config(app, host=host, port=int(port), log_level="info", loop="asyncio")
            server = uvicorn.Server(config)
            await server.serve()
        def drain():
            s = "".join(gui_queue)
            gui_queue.clear()
            return s
        import threading
        threading.Thread(target=lambda: asyncio.run(runner()), daemon=True).start()
        start_gui(drain)
    else:
        uvicorn.run(app, host=host, port=int(port), log_level="info")

if __name__ == "__main__":
    main()
