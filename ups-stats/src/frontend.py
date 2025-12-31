from flask import Flask, render_template_string
import sqlite3
import subprocess
import time
import os
from werkzeug.middleware.proxy_fix import ProxyFix


app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_host=1)

DB_PATH = os.environ.get("UPS_DB_PATH", "/var/lib/ups-stats/metrics.db")

TEMPLATE = """
<!doctype html>
<html>
<head>
	<meta name="viewport" content="width=device-width, initial-scale=1">
	<title>UPS Statistics</title>
	<style>
		body { font-family: monospace; background: #f4f4f4; padding: 20px; }
		ul { list-style-type: none; padding-left: 13px; }
		.header { display: flex; flex-direction: row-reverse; align-items: center; }
		.header h1 { margin: 0; }
		.header h2 { font-weight: 400; margin: 0; }   
		.main { background: white; padding: 15px; border-radius: 8px; }
		.main * { font-size: 16px; }
		.signature { font-size: 6px; }
		.spacer { flex: 1; }
		.standalone { font-size: 18px; }
		.title-block { display: flex; flex-direction: column; flex: 1; }
		.g { color: #009900 }
		.y { color: #ffcc00 }
		.o { color: #ff9900 }
		.r { color: #ff0033 }
		
		@media (max-width: 512px) {
			.header { flex-direction: column; align-items: flex-start; }
			.spacer { display: none; }
			.title-block { margin-bottom: 10px; }
			.signature { margin: 0; }
		}
	</style>
</head>
<body>
	<div class="header">
		<pre class="signature">
<span class="y">[P][P][P]             [R][R][R]  [S][S][S]</span>
<span class="y">[P]   [P]             [R]   [R]  [S]</span>
<span class="o">[P][P][P]  [q][q][q]  [R][R][R]  [S][S][S]</span>
<span class="o">[P]        [q]   [q]  [R][R]           [S]</span>
<span class="r">[P]        [q][q][q]  [R]   [R]  [S][S][S]</span>
<span class="r">                 [q]</span>
<span class="r">                 [q]</span>
		</pre>
		<div class="spacer"></div>
		<div class="title-block">
			<h1>UPS Statistics</h1>
			<h2>ups.leoapagano.com</h2>
		</div>
	</div>
	<hr>
	<p class='standalone'>Refresh to recheck - these values do not automatically update.</p>
	<div class="main">{{ status|safe }}</div>
	<p class='standalone'>(c) 2025 Leo Pagano | All rights reserved.</p>
</body>
</html>
"""


def pps(s):
	"""Pretty prints a number of seconds."""
	if s == -1:
		return "(not enough data)"
	elif s >= 3600:
		return f"{s // 3600}h{(s % 3600) // 60}m{(s % 3600) % 60}s"
	elif s >= 60:
		return f"{s // 60}m{s % 60}s"
	else:
		return f"{s}s"


def ppl(load):
	"""Pretty prints UPS load."""
	return f"{load:.2f}% ({load*6:.2f}W)"


def predict_battery_runtime(threshold=0):
	"""
	Predicts remaining battery runtime using historical drain rate.
	Returns seconds until battery reaches threshold percentage.
	Returns -1 if not enough data (need at least one completed percentage drop).
	Uses average time per percentage drop for stable estimates with discrete battery values.
	Falls back to 24 hours if not actively draining.
	"""
	conn = get_db()
	
	try:
		# Get when the current battery event started
		event = conn.execute("""
			SELECT start_ts FROM battery_events 
			WHERE end_ts IS NULL 
			ORDER BY start_ts DESC LIMIT 1
		""").fetchone()
		
		if event is None:
			return -1  # Not on battery power
		
		outage_start = event["start_ts"]
		
		# Get all battery readings since the outage started, ordered by time
		rows = conn.execute("""
			SELECT timestamp, battery_charge 
			FROM metrics_raw 
			WHERE timestamp >= ? AND battery_charge IS NOT NULL
			ORDER BY timestamp ASC
		""", (outage_start,)).fetchall()
		
		if len(rows) < 2:
			return -1  # No data
		
		# Find timestamps when battery percentage actually changed
		# We want the first timestamp for each distinct charge level
		transitions = []
		last_charge = None
		for row in rows:
			charge = row["battery_charge"]
			if last_charge is None or charge < last_charge:
				transitions.append((row["timestamp"], charge))
				last_charge = charge
		
		if len(transitions) < 2:
			# No completed percentage drops yet
			return -1
		
		# Calculate average time per percentage point drop
		first_time, first_charge = transitions[0]
		last_time, last_charge = transitions[-1]
		
		total_drop = first_charge - last_charge  # Total percentage points dropped
		total_time = last_time - first_time  # Time for those drops
		
		if total_drop <= 0:
			return 86400  # Not draining
		
		# Average seconds per percentage point
		seconds_per_percent = total_time / total_drop
		
		# Calculate seconds until we hit threshold
		remaining_percent = last_charge - threshold
		
		if remaining_percent <= 0:
			return 0
		
		seconds_remaining = remaining_percent * seconds_per_percent
		
		# Sanity check: cap at 24 hours
		return min(int(seconds_remaining), 86400)
		
	except (sqlite3.OperationalError, ZeroDivisionError, TypeError):
		return -1
	finally:
		conn.close()


def get_db():
	"""Get a read-only database connection."""
	conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
	conn.row_factory = sqlite3.Row
	return conn


def fetch_api_endpoint(endpoint):
	"""Fetches an API endpoint, such as ups.realpower.nominal.
	Attempts to return as an int, will return as a string otherwise."""
	cmd = [
		"upsc",
		"cyberups@localhost",
		endpoint
	]
	raw = subprocess.run(cmd, capture_output=True).stdout.splitlines()[-1].decode()
	try:
		return int(raw)
	except ValueError:
		return raw


def average_load_factor(seconds=86400):
	"""Determines the average load factor using tiered rollups for efficiency."""
	conn = get_db()
	now = time.time()
	
	try:
		if seconds <= 3600:
			# Use raw per-second data (last hour)
			row = conn.execute(
				"SELECT AVG(load_factor) as avg FROM metrics_raw WHERE timestamp > ?",
				(now - seconds,)
			).fetchone()
		elif seconds <= 86400:
			# Use per-minute rollups (last 24 hours)
			row = conn.execute("""
				SELECT SUM(avg_load * sample_count) / SUM(sample_count) as avg
				FROM metrics_minute WHERE minute_ts > ?
			""", (now - seconds,)).fetchone()
		else:
			# Use per-hour rollups (up to 30 days)
			row = conn.execute("""
				SELECT SUM(avg_load * sample_count) / SUM(sample_count) as avg
				FROM metrics_hour WHERE hour_ts > ?
			""", (now - seconds,)).fetchone()
		
		return row["avg"] if row["avg"] is not None else 0
	except sqlite3.OperationalError:
		# Table doesn't exist yet (collector hasn't run)
		return 0
	finally:
		conn.close()


def system_uptime():
	"""Determines the system uptime percentage (online on wall OR battery)."""
	conn = get_db()
	now = time.time()
	
	try:
		# Get when we started tracking
		row = conn.execute("SELECT value FROM metadata WHERE key = 'tracking_start'").fetchone()
		if row is None:
			return 1.0  # No data yet, assume 100%
		
		tracking_start = row["value"]
		total_tracked_time = now - tracking_start
		
		if total_tracked_time <= 0:
			return 1.0
		
		# Sum all completed downtime events
		row = conn.execute("""
			SELECT COALESCE(SUM(end_ts - start_ts), 0) as total_downtime
			FROM downtime_events
			WHERE end_ts IS NOT NULL
		""").fetchone()
		total_downtime = row["total_downtime"]
		
		# Add any ongoing downtime (event with no end_ts)
		row = conn.execute("""
			SELECT start_ts FROM downtime_events WHERE end_ts IS NULL
		""").fetchone()
		if row is not None:
			total_downtime += now - row["start_ts"]
		
		uptime_ratio = (total_tracked_time - total_downtime) / total_tracked_time
		return max(0.0, min(1.0, uptime_ratio))  # Clamp to 0-1
		
	except sqlite3.OperationalError:
		return 1.0  # Tables don't exist yet
	finally:
		conn.close()


def wall_power_uptime():
	"""Determines the wall power uptime percentage (online AND on wall power)."""
	conn = get_db()
	now = time.time()
	
	try:
		# Get when we started tracking
		row = conn.execute("SELECT value FROM metadata WHERE key = 'tracking_start'").fetchone()
		if row is None:
			return 1.0  # No data yet, assume 100%
		
		tracking_start = row["value"]
		total_tracked_time = now - tracking_start
		
		if total_tracked_time <= 0:
			return 1.0
		
		# Sum all system downtime
		row = conn.execute("""
			SELECT COALESCE(SUM(end_ts - start_ts), 0) as total
			FROM downtime_events
			WHERE end_ts IS NOT NULL
		""").fetchone()
		total_not_wall = row["total"]
		
		# Add ongoing system downtime
		row = conn.execute("SELECT start_ts FROM downtime_events WHERE end_ts IS NULL").fetchone()
		if row is not None:
			total_not_wall += now - row["start_ts"]
		
		# Sum all battery events
		row = conn.execute("""
			SELECT COALESCE(SUM(end_ts - start_ts), 0) as total
			FROM battery_events
			WHERE end_ts IS NOT NULL
		""").fetchone()
		total_not_wall += row["total"]
		
		# Add ongoing battery event
		row = conn.execute("SELECT start_ts FROM battery_events WHERE end_ts IS NULL").fetchone()
		if row is not None:
			total_not_wall += now - row["start_ts"]
		
		uptime_ratio = (total_tracked_time - total_not_wall) / total_tracked_time
		return max(0.0, min(1.0, uptime_ratio))  # Clamp to 0-1
		
	except sqlite3.OperationalError:
		return 1.0  # Tables don't exist yet
	finally:
		conn.close()


@app.route("/")
def index():
	endpoints = {
		"battery.charge": fetch_api_endpoint("battery.charge"),
		"battery.runtime": fetch_api_endpoint("battery.runtime"),
		"ups.load": fetch_api_endpoint("ups.load"),
		"ups.status": fetch_api_endpoint("ups.status")
	}
	
	system_uptime_pct = system_uptime() * 100
	wall_power_uptime_pct = wall_power_uptime() * 100
	
	# Use our prediction (returns -1 if not enough data)
	runtime_seconds = predict_battery_runtime(threshold=20)  # Predict to 20% (shutdown threshold)
	
	results = f"""<ul>
<li>Status: <b>{endpoints["battery.charge"]}% | {f"<span class='g'>ON WALL POWER" if (endpoints["ups.status"].startswith("OL")) else f"<span class='r'>ON BATTERY BACKUP{f" - ABOUT {pps(runtime_seconds)} REMAINING" if runtime_seconds != -1 else ""}"}</span></b></li>
<li>Load Factor:</li>
<ul>
	<li>Right now: {ppl(endpoints["ups.load"])}</li>
	<li>Last minute: {ppl(average_load_factor(seconds=60))}</li>
	<li>Last hour: {ppl(average_load_factor(seconds=3600))}</li>
	<li>Last 24 hours: {ppl(average_load_factor(seconds=86400))}</li>
	<li>Last 7 days: {ppl(average_load_factor(seconds=86400*7))}</li>
	<li>Last 30 days: {ppl(average_load_factor(seconds=86400*30))}</li>
</ul>
<li>Uptime:</li>
<ul>
	<li>Total: {system_uptime_pct:.8f}%</li>
	<li>Wall-only: {wall_power_uptime_pct:.8f}%</li>
</ul>
</ul>"""
	return render_template_string(TEMPLATE, status=results)


if __name__ == "__main__":
	app.run()