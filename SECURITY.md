# Security and privacy

Local Image Studio listens on `127.0.0.1` only. It is not designed to be exposed
to a LAN or the public internet.

The application stores uploaded reference images in ComfyUI's local `input`
directory and generated images in ComfyUI's local `output` directory. The app
itself does not include telemetry, analytics, accounts, remote inference, or a
hosted moderation layer.

Do not put credentials, API keys, personal photos, model weights, logs, or
private data in this repository.

If you discover a security issue, open a private security advisory on GitHub
rather than publishing exploit details in a regular issue.

