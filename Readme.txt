================================================================================
 Prober Machine RFID Application - Setup & Deployment Guide
================================================================================

1. Running on Windows / PC (Development & Testing):
--------------------------------------------------
   - Python 3.9+ recommended.
   - Install dependencies:
       pip install -r requirements.txt
   - Run the application:
       python Main_Prober_with_error.py
   - Web UI accessible at:
       http://127.0.0.1:8001


2. Running on NXP i.MX8 (Linux Yocto) / Raspberry Pi:
-----------------------------------------------------
   - Clone or copy this repository to the target board (e.g. /home/root/Prober_machine_work).
   - Run the all-in-one setup & auto-start script:
       bash setup_autostart.sh
   
   - The script automatically:
       a) Installs dependencies from requirements.txt
       b) Configures serial port permissions (dialout group)
       c) Creates & enables the systemd service (/etc/systemd/system/prober.service)
       d) Starts the service immediately with auto-restart on failure.

   - Useful service management commands on i.MX8:
       systemctl status prober.service   # Check live status
       systemctl restart prober.service  # Restart application
       systemctl stop prober.service     # Stop application
       journalctl -u prober.service -f   # View live RFID scan & Flask logs


3. Required Dependencies (requirements.txt):
--------------------------------------------
   - flask
   - flask-cors
   - requests
   - pyserial
   - mysql-connector-python