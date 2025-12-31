import json
import requests
import sqlite3
import subprocess
import time

from frontend import pps, ppl, DB_PATH


def init_db():
	"""Initialize the database with tiered tables."""
	conn = sqlite3.connect(DB_PATH)
	conn.execute("PRAGMA journal_mode=WAL")  # Better concurrent access
	
	# Raw per-second load data (keep last hour)
	conn.execute("""
		CREATE TABLE IF NOT EXISTS metrics_raw (
			timestamp REAL PRIMARY KEY,
			load_factor REAL,
			battery_charge REAL
		)
	""")
	
	# Per-minute load rollups (keep last 24 hours)
	conn.execute("""
		CREATE TABLE IF NOT EXISTS metrics_minute (
			minute_ts INTEGER PRIMARY KEY,
			avg_load REAL,
			sample_count INTEGER
		)
	""")
	
	# Per-hour load rollups (keep last 30 days)
	conn.execute("""
		CREATE TABLE IF NOT EXISTS metrics_hour (
			hour_ts INTEGER PRIMARY KEY,
			avg_load REAL,
			sample_count INTEGER
		)
	""")
	
	# Downtime events (system offline) - exact timestamps, kept forever
	conn.execute("""
		CREATE TABLE IF NOT EXISTS downtime_events (
			id INTEGER PRIMARY KEY AUTOINCREMENT,
			start_ts REAL NOT NULL,
			end_ts REAL
		)
	""")
	
	# Battery events (on battery power) - exact timestamps, kept forever
	conn.execute("""
		CREATE TABLE IF NOT EXISTS battery_events (
			id INTEGER PRIMARY KEY AUTOINCREMENT,
			start_ts REAL NOT NULL,
			end_ts REAL
		)
	""")
	
	# Metadata table for tracking start time
	conn.execute("""
		CREATE TABLE IF NOT EXISTS metadata (
			key TEXT PRIMARY KEY,
			value REAL
		)
	""")
	
	# Indexes for faster queries
	conn.execute("CREATE INDEX IF NOT EXISTS idx_raw_ts ON metrics_raw(timestamp)")
	conn.execute("CREATE INDEX IF NOT EXISTS idx_minute_ts ON metrics_minute(minute_ts)")
	conn.execute("CREATE INDEX IF NOT EXISTS idx_hour_ts ON metrics_hour(hour_ts)")
	
	conn.commit()
	return conn


def fetch_api_endpoint(endpoint):
	"""Fetches an API endpoint, such as ups.realpower.nominal.
	Attempts to return as an int, will return as a string otherwise."""
	cmd = [
		"upsc",
		"cyberups@localhost",
		endpoint
	]
	try:
		raw = subprocess.run(cmd, capture_output=True, timeout=5).stdout.splitlines()[-1].decode()
		try:
			return int(raw)
		except ValueError:
			return raw
	except (subprocess.TimeoutExpired, IndexError, Exception) as e:
		print(f"Error fetching {endpoint}: {e}")
		return None


def do_minute_rollup(conn, minute_ts):
	"""Roll up raw data from the previous minute into metrics_minute."""
	prev_minute = minute_ts - 60
	conn.execute("""
		INSERT OR REPLACE INTO metrics_minute (minute_ts, avg_load, sample_count)
		SELECT 
			? as minute_ts,
			AVG(load_factor),
			COUNT(*)
		FROM metrics_raw
		WHERE timestamp >= ? AND timestamp < ?
		HAVING COUNT(*) > 0
	""", (prev_minute, prev_minute, minute_ts))


def do_hour_rollup(conn, hour_ts):
	"""Roll up minute data from the previous hour into metrics_hour."""
	prev_hour = hour_ts - 3600
	conn.execute("""
		INSERT OR REPLACE INTO metrics_hour (hour_ts, avg_load, sample_count)
		SELECT 
			? as hour_ts,
			SUM(avg_load * sample_count) / SUM(sample_count),
			SUM(sample_count)
		FROM metrics_minute
		WHERE minute_ts >= ? AND minute_ts < ?
		HAVING SUM(sample_count) > 0
	""", (prev_hour, prev_hour, hour_ts))


def prune_old_data(conn, now):
	"""Remove data older than retention periods."""
	conn.execute("DELETE FROM metrics_raw WHERE timestamp < ?", (now - 3600,))  # 1 hour
	conn.execute("DELETE FROM metrics_minute WHERE minute_ts < ?", (now - 86400,))  # 24 hours
	conn.execute("DELETE FROM metrics_hour WHERE hour_ts < ?", (now - 86400 * 30,))  # 30 days


def average_load_factor_from_db(conn, seconds=3600):
	"""Get average load factor from database for notifications."""
	now = time.time()
	try:
		row = conn.execute(
			"SELECT AVG(load_factor) as avg FROM metrics_raw WHERE timestamp > ?",
			(now - seconds,)
		).fetchone()
		return row[0] if row and row[0] is not None else 0
	except sqlite3.OperationalError:
		return 0


def send_email(header: str, message: str) -> bool:
	"""Send an email notification."""
	url = "http://mail.leoapagano.com/send-email"
	payload = {
		"subject": header,
		"body": message
	}
	headers = {
		"Content-Type": "application/json"
	}
	
	try:
		response = requests.post(url, headers=headers, data=json.dumps(payload), timeout=10)
		return response.status_code == 200
	except requests.RequestException as e:
		print(f"Failed to send email: {e}")
		return False


def send_power_cut_notif(conn):
	"""Send notification when wall power is cut."""
	battery_charge = fetch_api_endpoint("battery.charge")
	avg_load = average_load_factor_from_db(conn, seconds=3600)
	
	header = "Power Supply Interrupted"
	message = f"""The main power supply to PQRS has been interrupted. This could be caused by inclement weather, an electrical grid failure, or PQRS being unplugged, as well as a litany of other possible reasons.

If the generator is working properly, power will be restored shortly. You will be notified by email if and when this happens.

If this does not happen, please keep an eye on PQRS's UPS Statistics page at ups.leoapagano.com.

PQRS will shut down when the UPS's battery has less than 20 percent of its charge remaining, at which point it must be powered back on manually after power is restored.

Current statistics:
- Wall power is disconnected. The battery backup is in use.
- Battery is at {battery_charge}% charge.
- Average load of {ppl(avg_load)} in the last hour."""
		
	if send_email(header, message):
		print(f"[{time.strftime('%H:%M:%S')}] Sent power cut notification")
	else:
		print(f"[{time.strftime('%H:%M:%S')}] Failed to send power cut notification")


def send_power_restored_notif(conn):
	"""Send notification when wall power is restored."""
	battery_charge = fetch_api_endpoint("battery.charge")
	avg_load = average_load_factor_from_db(conn, seconds=3600)
	
	header = "Power Supply Restored"
	message = f"""The main power supply to PQRS has been restored. No further action is required at this time.

Current statistics:
- Wall power is connected. The battery backup is no longer in use.
- Battery is at {battery_charge}% charge.
- Average load of {ppl(avg_load)} in the last hour."""
		
	if send_email(header, message):
		print(f"[{time.strftime('%H:%M:%S')}] Sent power restored notification")
	else:
		print(f"[{time.strftime('%H:%M:%S')}] Failed to send power restored notification")


def send_low_battery_notif(conn):
	"""Send notification when battery falls below 20%. Also tells the host to power itself off."""
	battery_charge = fetch_api_endpoint("battery.charge")
	avg_load = average_load_factor_from_db(conn, seconds=3600)
	
	header = "UPS Battery Low | Shutting Down"
	message = f"""The main power supply to PQRS has been interrupted, and the UPS's battery is now at or below 20%.

As a result, all VMs are being terminated and PQRS will power down in the next couple of minutes to avoid a crash.

Please note that once wall power is restored, PQRS must be powered back on manually.

Current statistics:
- Wall power is disconnected. The battery backup is in use.
- Battery is at {battery_charge}% charge.
- Average load of {ppl(avg_load)} in the last hour."""
		
	if send_email(header, message):
		print(f"[{time.strftime('%H:%M:%S')}] Sent low battery notification")
	else:
		print(f"[{time.strftime('%H:%M:%S')}] Failed to send low battery notification")
	
	# Initiate remote shutdown
	print(f"[{time.strftime('%H:%M:%S')}] Initiating remote shutdown...")
	fail = False
	try:
		result = subprocess.run(
			["ssh", "leo@10.77.17.1", "sudo systemctl poweroff"],
			capture_output=True,
			timeout=30
		)
		if result.returncode == 0:
			print(f"[{time.strftime('%H:%M:%S')}] Remote shutdown command sent successfully")
		else:
			print(f"[{time.strftime('%H:%M:%S')}] Remote shutdown failed: {result.stderr.decode()}")
	except subprocess.TimeoutExpired:
		print(f"[{time.strftime('%H:%M:%S')}] Remote shutdown command timed out")
		fail = True
	except Exception as e:
		print(f"[{time.strftime('%H:%M:%S')}] Remote shutdown error: {e}")
		fail = True
		
	# Send message informing of failed shutdown if still up
	if fail:
		header = "ACTION NEEDED: UPS Battery Low | Shutdown Failed"
		message = f"""For some reason, PQRS failed to shut itself down.
		
		Please manually shut it down now or the system and all VMs running on it may crash abruptly when power runs out."""
			
		if send_email(header, message):
			print(f"[{time.strftime('%H:%M:%S')}] Sent shutdown failure notification")
		else:
			print(f"[{time.strftime('%H:%M:%S')}] Failed to send shutdown failure notification")


def detect_collection_gap(conn):
	"""Check for gaps since last data collection and record as downtime."""
	now = time.time()
	conn.row_factory = sqlite3.Row
	
	# Find the most recent data point from any source
	row = conn.execute("SELECT MAX(timestamp) as last_ts FROM metrics_raw").fetchone()
	last_raw = row["last_ts"] if row and row["last_ts"] else None
	
	row = conn.execute("SELECT MAX(minute_ts) as last_ts FROM metrics_minute").fetchone()
	last_minute = row["last_ts"] if row and row["last_ts"] else None
	
	# Use the most recent timestamp we have
	candidates = [t for t in [last_raw, last_minute] if t is not None]
	if not candidates:
		return  # No previous data, nothing to check
	
	last_collection = max(candidates)
	gap = now - last_collection
	
	# If gap > 30 seconds, assume collector was down
	if gap > 30:
		gap_str = f"{gap/3600:.1f}h" if gap >= 3600 else f"{gap/60:.1f}m" if gap >= 60 else f"{gap:.0f}s"
		print(f"Detected collection gap of {gap_str} (from {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(last_collection))} to now)")
		
		# Close any open events that were interrupted by the gap
		conn.execute("UPDATE downtime_events SET end_ts = ? WHERE end_ts IS NULL", (last_collection,))
		conn.execute("UPDATE battery_events SET end_ts = ? WHERE end_ts IS NULL", (last_collection,))
		
		# Record this entire gap as system downtime (we don't know the true status)
		conn.execute("""
			INSERT INTO downtime_events (start_ts, end_ts)
			VALUES (?, ?)
		""", (last_collection, now))
		conn.commit()
		print(f"Recorded gap as system downtime")


if __name__ == "__main__":
	print(f"Starting UPS metrics collector, DB: {DB_PATH}")
	conn = init_db()
	
	# Check for collection gaps since last run
	detect_collection_gap(conn)
	
	# Record when we started tracking (for uptime calculation)
	row = conn.execute("SELECT value FROM metadata WHERE key = 'tracking_start'").fetchone()
	if row is None:
		conn.execute("INSERT INTO metadata VALUES ('tracking_start', ?)", (time.time(),))
		conn.commit()
	
	last_minute_rollup = int(time.time() // 60) * 60
	last_hour_rollup = int(time.time() // 3600) * 3600
	last_system_online = True  # Assume system is online at start
	last_on_wall_power = True  # Assume on wall power at start
	low_battery_notif_sent = False  # Track if we've sent the low battery notification
	
	# Main program loop
	while True:
		now = time.time()
		
		# Fetch current metrics
		load_factor = fetch_api_endpoint("ups.load")
		status_raw = fetch_api_endpoint("ups.status")
		battery_charge = fetch_api_endpoint("battery.charge")
		
		if load_factor is not None:
			# Insert raw load and battery data
			conn.execute(
				"INSERT OR REPLACE INTO metrics_raw VALUES (?, ?, ?)",
				(now, load_factor, battery_charge)
			)
		
		# Track status events (only when status changes)
		# UPS statuses: OL = Online (wall power), OB = On Battery
		if status_raw is not None:
			status_str = str(status_raw)
			is_system_online = "OL" in status_str or "OB" in status_str
			is_on_wall_power = "OL" in status_str and "OB" not in status_str
			
			# Track system downtime (completely offline)
			if last_system_online and not is_system_online:
				conn.execute(
					"INSERT INTO downtime_events (start_ts) VALUES (?)",
					(now,)
				)
				print(f"[{time.strftime('%H:%M:%S')}] System went OFFLINE")
			elif not last_system_online and is_system_online:
				conn.execute(
					"UPDATE downtime_events SET end_ts = ? WHERE end_ts IS NULL",
					(now,)
				)
				print(f"[{time.strftime('%H:%M:%S')}] System back ONLINE")
			
			# Track battery events (switched to battery power)
			if last_on_wall_power and not is_on_wall_power and is_system_online:
				conn.execute(
					"INSERT INTO battery_events (start_ts) VALUES (?)",
					(now,)
				)
				print(f"[{time.strftime('%H:%M:%S')}] Switched to BATTERY power")
				send_power_cut_notif(conn)
				low_battery_notif_sent = False  # Reset low battery flag for new outage
			elif not last_on_wall_power and is_on_wall_power:
				conn.execute(
					"UPDATE battery_events SET end_ts = ? WHERE end_ts IS NULL",
					(now,)
				)
				print(f"[{time.strftime('%H:%M:%S')}] Back to WALL power")
				send_power_restored_notif(conn)
				low_battery_notif_sent = False  # Reset low battery flag
			
			last_system_online = is_system_online
			last_on_wall_power = is_on_wall_power
		
		# Check for low battery condition (only while on battery)
		if battery_charge is not None and not last_on_wall_power:
			if battery_charge <= 20 and not low_battery_notif_sent:
				send_low_battery_notif(conn)
				low_battery_notif_sent = True
		
		current_minute = int(now // 60) * 60
		current_hour = int(now // 3600) * 3600
		
		# Every minute: roll up last minute's raw data
		if current_minute > last_minute_rollup:
			do_minute_rollup(conn, current_minute)
			last_minute_rollup = current_minute
			print(f"[{time.strftime('%H:%M:%S')}] Minute rollup complete")
		
		# Every hour: roll up last hour's minute data
		if current_hour > last_hour_rollup:
			do_hour_rollup(conn, current_hour)
			last_hour_rollup = current_hour
			print(f"[{time.strftime('%H:%M:%S')}] Hour rollup complete")
		
		# Prune old load data periodically
		if current_minute > last_minute_rollup - 60:
			prune_old_data(conn, now)
		
		conn.commit()
		time.sleep(1)
