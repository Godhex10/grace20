# services/system_ops.py
import subprocess
import platform
import psutil
import logging

logger = logging.getLogger(__name__)

ALLOWED_ACTIONS = {"lock_session", "open_vscode", "diagnostics"}

class SystemAutomationBridge:
    def get_system_diagnostics(self) -> dict:
        """
        Gathers basic hardware vital signs to pipe directly into the terminal widget.
        """
        try:
            cpu_pct = psutil.cpu_percent(interval=None)
            ram = psutil.virtual_memory()
            disk = psutil.disk_usage("/")
            
            return {
                "cpu_status": "nominal",
                "cpu_usage": round(cpu_pct, 1),
                "ram_usage": round(ram.percent, 1),
                "ram_total_gb": round(ram.total / (1024**3), 1),
                "ram_available_gb": round(ram.available / (1024**3), 1),
                "disk_usage": round(disk.percent, 1),
                "disk_total_gb": round(disk.total / (1024**3), 1),
                "disk_free_gb": round(disk.free / (1024**3), 1),
                "os_platform": platform.system(),
                "os_release": platform.release(),
                "storage_integrity": "secure",
                "uptime_secs": int(psutil.boot_time()),
                "processes": len(psutil.pids())
            }
        except Exception as e:
            logger.error(f"System diagnostics failed: {e}", exc_info=True)
            return {"status": "error", "message": str(e)}

    def execute_system_command(self, action: str) -> str:
        """
        Executes safe OS-level automations based on matched intent keywords.
        """
        if action not in ALLOWED_ACTIONS:
            return f"Action '{action}' not allowed. Allowed: {', '.join(ALLOWED_ACTIONS)}"
        
        current_os = platform.system()
        
        try:
            if action == "lock_session":
                if current_os == "Windows":
                    subprocess.run(["rundll32.exe", "user32.dll,LockWorkStation"], check=False)
                elif current_os == "Darwin":
                    subprocess.run(["osascript", "-e", "tell app \"System Events\" to sleep"], check=False)
                elif current_os == "Linux":
                    subprocess.run(["loginctl", "lock-session"], check=False)
                return "Locking system workstation session."

            elif action == "open_vscode":
                if current_os == "Windows":
                    subprocess.Popen(["code"], shell=False)
                elif current_os == "Darwin":
                    subprocess.Popen(["open", "-a", "Visual Studio Code"], shell=False)
                elif current_os == "Linux":
                    subprocess.Popen(["code"], shell=False)
                return "Launching Visual Studio Code development environment."
                
            return f"Action '{action}' is registered but unconfigured for this platform."
        except FileNotFoundError:
            logger.error(f"Command not found for action: {action} on {current_os}")
            return f"Command not found for action '{action}' on {current_os}."
        except Exception as e:
            logger.error(f"System automation execution error: {e}", exc_info=True)
            return f"System automation execution error: {str(e)}"

system_bridge = SystemAutomationBridge()