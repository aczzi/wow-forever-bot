# Wow forever Bot

```sh
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export DISCORD_TOKEN='votre_nouveau_jeton'
python main.py
```

# wfb.service

DISCORD_TOKEN=ton_jeton_discord

set up /etc/systemd/system/wfb.service

systemctl daemon-reload
systemctl start wfb
