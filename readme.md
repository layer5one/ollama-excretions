# Linux/macOS shims

Put these next to `ollama_excretions_speaker.py`, then `chmod +x` them.

**`kokoro_excretions`**

```bash
#!/usr/bin/env bash
python3 "$(dirname "$0")/ollama_excretions_speaker.py" \
  --engine kokoro \
  --listen 0.0.0.0:11435 \
  --upstream http://127.0.0.1:11434 "$@"
```

**`kitten_excretions`**

```bash
#!/usr/bin/env bash
python3 "$(dirname "$0")/ollama_excretions_speaker.py" \
  --engine kitten-nano \
  --listen 0.0.0.0:11435 \
  --upstream http://127.0.0.1:11434 "$@"
```

> Want the bigger Kitten? Run: `kitten_excretions --engine kitten-mini`
> Want GUI? Add `--gui`. Want a different upstream? `--upstream http://altar:11434`.

---

# Windows .bat launchers

**`kokoro_excretions.bat`**

```bat
@echo off
python "%~dp0ollama_excretions_speaker.py" --engine kokoro --listen 0.0.0.0:11435 --upstream http://127.0.0.1:11434 %*
```

**`kitten_excretions.bat`**

```bat
@echo off
python "%~dp0ollama_excretions_speaker.py" --engine kitten-nano --listen 0.0.0.0:11435 --upstream http://127.0.0.1:11434 %*
```

---

# Pi / headless (systemd)

`/etc/systemd/system/ollama-excretions.service`

```ini
[Unit]
Description=Ollama Excretions Speaker (Kokoro)
After=network-online.target sound.target
Wants=network-online.target

[Service]
User=pi
WorkingDirectory=/opt/ollama-excretions
ExecStart=/usr/bin/python3 /opt/ollama-excretions/ollama_excretions_speaker.py \
  --engine kokoro \
  --listen 0.0.0.0:11435 \
  --upstream http://127.0.0.1:11434
Restart=always
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now ollama-excretions
```

---

# Client side (to “make it native”)

Point anything that talks to Ollama at the proxy:

```bash
export OLLAMA_HOST=http://<box>:11435
# or hardcode the base URL in your tool configs
```

Everything that streams from `/api/chat` or `/api/generate` will be spoken while it prints.

---

Want me to add a third shim like `hl.exe -thereisnospoon` vibes (e.g., `excretions.exe --gui --voice af_bella` packager for Windows), or are the shell/bat launchers enough for muscle memory?
