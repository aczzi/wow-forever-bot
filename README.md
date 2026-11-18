# Bot de suivi des donjons

Python 3.11 ou supérieur. Installation et démarrage :

```sh
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export DISCORD_TOKEN='votre_nouveau_jeton'
python main.py
```

# discord-donjon.service

 /etc/discord-donjon.env 

DISCORD_TOKEN=ton_jeton_discord
DONJONS_DB=/home/pi/discord_donjon/donjons.sqlite3

/etc/systemd/system/discord-donjon.service

[Unit]
Description=Bot Discord de suivi des donjons
Wants=network-online.target
After=network-online.target

[Service]
Type=simple
User=pi
WorkingDirectory=/home/pi/discord_donjon
EnvironmentFile=/etc/discord-donjon.env
ExecStart=/home/pi/discord_donjon/.venv/bin/python -u /home/pi/discord_donjon/main.py
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target