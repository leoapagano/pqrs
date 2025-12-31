# UPS Statistics
- The UPS Statistics page keeps track of the following information:
	- UPS battery charge level
	- UPS status (on wall power, on battery, overloaded)
		- If discharging, an estimate for how much time is left
	- Load factor currently, as well as in:
		- The last minute
		- The last hour
		- The last 24 hours
		- The last 7 days
		- The last 30 days
	- System uptime percentage
	- Wall power uptime percentage
- You can access this information at ups.leoapagano.com (behind Tailscale).

## Software Stack
- This is intended to be run on a Debian LXC guest on a Proxmox host.
- The following tools are used on the frontend:
	- Python (programming language)
	- Flask (web server framework)
	- Gunicorn (production-ready WSGI server)
	- NGINX (reverse proxy)
- And on the backend:
	- SQLite (backend database)
	- NUT (read-only UPS data access)

## VM Configuration
### Introduction
- Create debian lxc with the usual setup - don't launch right away
- Add to lxc config:

```
lxc.mount.entry = /dev/bus/usb dev/bus/usb none bind,optional,create=dir
lxc.cgroup2.devices.allow: c 189:* rwm
```

- Start the new lxc up
- Inside the lxc:

```shell
apt update
apt upgrade -y
apt install -y gunicorn3 nginx nut nut-client nut-server python3 python3-flask
```

- Before continuing, make sure that:
	- you can SSH into `leo@10.77.17.1` (quark)
	- `leo@10.77.17.1` has passwordless sudo access
	- These are needed in order for automatic graceful shutdown at 20% charge to work!

- Also, create an empty directory at:
	- `/var/lib/ups-stats/`

### NUT Setup
- To setup NUT, add this to `/etc/nut/ups.conf`:

```
[cyberups]
    driver = usbhid-ups
    port = auto
    desc = "CyberPower UPS"
    pollinterval = 1
    pollfreq = 1
```

- In this file, you should also look for `maxretry = 3` and delete it.
- Then, in `/etc/nut/nut.conf` (replaces `MODE=none`):

```
MODE=standalone
```

- Add to `/etc/nut/upsd.conf`:

```
LISTEN 127.0.0.1 3493
```

- Add to `/etc/nut/upsd.users`:

```
[admin]
    password = yourpassword
    actions = SET
    instcmds = ALL

[upsmon]
    password = yourpassword
    upsmon master
```

- Add to `/etc/nut/upsmon.conf`:

```
MONITOR cyberups@localhost 1 upsmon yourpassword master
```

- Run as root:

```shell
upsdrvctl start
systemctl enable nut-server.service
systemctl enable nut-monitor.service
systemctl start nut-server.service
systemctl start nut-monitor.service
```

- Now try:

```shell
watch -n 1 upsc cyberups@localhost
```

- If that works before and after a reboot, you're ready to go!
- Query endpoints like this:

```shell
upsc cyberups@localhost ups.status
Init SSL without certificate database
OL
```

- Endpoints include:
	- `ups.status` (OL=online, OB=battery, etc.)
	- `battery.runtime` (battery est. runtime in seconds)
	- `battery.charge` (% battery charge)
	- `ups.load` (% load UPS)

### System Setup
Files modified on this VM include:

`/opt/ups-stats/frontend.py` and `/opt/ups-stats/backend.py`
- Copy from this repo
- TODO: set up and document CI/CD for this

`/etc/systemd/system/ups-stats-backend.service`
```
[Unit]
Description=UPS Statistics Backend
After=network.target nut-server.service

[Service]
Type=simple
User=root
Environment="UPS_DB_PATH=/var/lib/ups-stats/metrics.db"
WorkingDirectory=/opt/ups-stats
ExecStart=/usr/bin/python3 /opt/ups-stats/backend.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```
Then:
- `sudo systemctl enable ups-stats-backend`
- `sudo systemctl start ups-stats-backend`

`/etc/systemd/system/ups-stats-frontend.service`
```
[Unit]
Description=UPS Statistics Frontend
After=network.target ups-stats-backend.service

[Service]
Type=simple
User=www-data
Group=www-data
Environment="UPS_DB_PATH=/var/lib/ups-stats/metrics.db"
WorkingDirectory=/opt/ups-stats
ExecStart=/usr/bin/gunicorn --workers 2 --bind 127.0.0.1:8000 frontend:app
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```
Then:
- `sudo systemctl enable ups-stats-frontend`
- `sudo systemctl start ups-stats-frontend`

`/etc/nginx/sites-available/ups-stats`
```
server {
    listen 80;
    server_name ups.leoapagano.com;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    }
}
```
Then:
- `sudo systemctl enable nginx`
- `sudo ln -s /etc/nginx/sites-available/ups-stats /etc/nginx/sites-enabled/`
- `sudo nginx -t` (if making changes to the above or updating)
- `sudo systemctl start nginx`
