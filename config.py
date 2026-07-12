"""Loads credentials from .env. Fails loudly at import if anything is missing."""

import os

from dotenv import load_dotenv

load_dotenv()

SNAPTRADE_CLIENT_ID = os.environ["SNAPTRADE_CLIENT_ID"]
SNAPTRADE_CONSUMER_KEY = os.environ["SNAPTRADE_CONSUMER_KEY"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]

# Personal (non-commercial) SnapTrade accounts use these literal placeholders
# instead of a per-end-user id/secret pair.
SNAPTRADE_USER_ID = "personal"
SNAPTRADE_USER_SECRET = "personal"

# Sonnet over Opus deliberately: on the SpaceX research question Sonnet answered in
# 11s against Opus's 38s, and got it MORE right (it caught the June 2026 IPO and used
# the live quote). For a voice assistant the latency is the product — nobody stands
# there for 38 seconds of silence.
CLAUDE_MODEL = "claude-sonnet-5"
