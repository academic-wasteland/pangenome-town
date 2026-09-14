#!/bin/sh
set -eu
if [ -n "${OPENROUTER_API_KEY:-}" ]; then echo "OPENROUTER_API_KEY set"; exit 0; fi
if grep -qs '^OPENROUTER_API_KEY=' "$HOME/.gc/secrets.env"; then echo "OPENROUTER_API_KEY in ~/.gc/secrets.env"; exit 0; fi
echo "OPENROUTER_API_KEY missing: add it to ~/.gc/secrets.env (chmod 600)"; exit 2
