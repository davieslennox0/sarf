# Reinstalling the Sui / Walrus binaries

Removed 2026-08-08 (332 MB) while reclaiming disk. They were NOT Sarf
leftovers — `fanside` shells out to both — but fanside is being sunset, so
they went with it.

Versions that were installed:

| binary | version | size |
|---|---|---|
| `sui` | `1.73.2-1f6e1e6dd72d` | 204 MB |
| `walrus` | `1.48.1-9c5590a81e29` | 64 MB |
| `walrus-publisher` | `1.48.1-9c5590a81e29` | 64 MB |

If fanside's sunset slips and it needs to run again, the callers are:

- `/root/fanside/backend/sui_client.py:24` — `shutil.which("sui") or "/usr/local/bin/sui"`
- `/root/fanside/backend/main.py` — three `subprocess.run(["walrus", "read-quilt", ...])`
  calls on the image-serving path

Reinstall from the Mysten releases (mainnet channel), then drop the binaries
back in `/usr/local/bin` and `chmod +x`:

    https://github.com/MystenLabs/sui/releases
    https://github.com/MystenLabs/walrus/releases

Nothing in Sarf calls either one — X Layer is an ordinary EVM chain reached
over JSON-RPC.
