# Pinned OpenAI Plugin validator

`validate_plugin.py` is the official Codex Plugin validator copied from OpenAI Codex commit `1883dedc0e3499c8f42e08835540319ad7131d77`:

<https://github.com/openai/codex/blob/1883dedc0e3499c8f42e08835540319ad7131d77/codex-rs/skills/src/assets/samples/plugin-creator/scripts/validate_plugin.py>

SHA-256: `f6d90a9925d8b8583d61a857c0cb4c6f64926349e55ea1119e6ece7e5cfc787f`

`tooling/validate.sh` uses the installed official validator when available and falls back to this pinned copy otherwise. It requires `PyYAML` from `tooling/requirements.txt`. Update the commit and checksum together when the upstream Plugin contract changes.
