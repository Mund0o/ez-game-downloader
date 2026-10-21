"""Where the shared crash knowledge lives, and the public keys that make it safe (server/knowledge).

* ``ENCRYPT_KEY`` (X25519): every report is sealed to it with a fresh random key — only the server can open it.
* ``SIGN_KEY`` (Ed25519): what the server publishes is signed with its private half — the app uses nothing else.

Deployed 2026-10-21 (Cloudflare Worker "ezgd-knowledge", D1 database "ezgd-knowledge"); the private keys are in
~/.local/share/ezgd-crowd-keys/secrets.json on the developer's computer and in the worker's secrets — nowhere else.
"""

SERVER = "https://ezgd-knowledge.pair-private-link.workers.dev"
ENCRYPT_KEY = "2AXv8tgrdBoD1I4Lkc7PzErWv4CRf/t7XKwNx9CNrhQ="
SIGN_KEY = "IGunI9kbC98oaF57nR+I1Mxcbzfds2qIt6X7wTopyxQ="
