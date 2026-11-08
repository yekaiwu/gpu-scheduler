"""Install templates. The canonical copies of the plists live in deploy/; a test keeps them identical."""

AGENT_PLIST = """\
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>{{LABEL}}</string>
  <key>ProgramArguments</key><array>
    <string>{{VENV_PYTHON}}</string><string>-I</string><string>-m</string><string>gsched</string><string>daemon</string>
  </array>
  <key>EnvironmentVariables</key><dict>
    <key>GSCHED_HOME</key><string>{{HOME_DIR}}</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><dict><key>SuccessfulExit</key><false/></dict>
  <key>ThrottleInterval</key><integer>15</integer>
  <key>ProcessType</key><string>Standard</string>
  <key>StandardOutPath</key><string>{{HOME_DIR}}/logs/launchd.out.log</string>
  <key>StandardErrorPath</key><string>{{HOME_DIR}}/logs/launchd.err.log</string>
</dict></plist>
"""

DASHBOARD_PLIST = """\
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>{{LABEL}}</string>
  <key>ProgramArguments</key><array>
    <string>{{VENV_PYTHON}}</string><string>-I</string><string>-m</string><string>gsched</string><string>dashboard</string><string>--port</string><string>{{PORT}}</string>
  </array>
  <key>EnvironmentVariables</key><dict>
    <key>GSCHED_HOME</key><string>{{HOME_DIR}}</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>15</integer>
  <key>ProcessType</key><string>Background</string>
  <key>StandardOutPath</key><string>{{HOME_DIR}}/logs/dashboard.out.log</string>
  <key>StandardErrorPath</key><string>{{HOME_DIR}}/logs/dashboard.err.log</string>
</dict></plist>
"""

WRAPPER = """\
#!/bin/sh
# gsched wrapper written by `gsched install`. -I keeps user site-packages and PYTHON* variables out.
exec "{{VENV_PYTHON}}" -I -m gsched "$@"
"""
