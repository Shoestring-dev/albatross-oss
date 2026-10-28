"""The built-in Telegram listener: your own Telegram account reads the groups and channels you pick on the Setup page,
and every token address posted there is logged as a memecoin call, graded like a call pushed to /api/ingest/calls.

  parse.py     token addresses in a message (pure)
  client.py    the Telethon client both processes build, and the picked chat ids
  status.py    the listener's state for the Setup page (table telegram_listener)
  login.py     phone -> code -> 2FA password, in the dashboard; saves TELEGRAM_SESSION
  listener.py  the long-lived task in ingest
"""
