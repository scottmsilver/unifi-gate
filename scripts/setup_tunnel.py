#!/usr/bin/env python3
"""
Cloudflare Tunnel Setup Script

Interactive script to configure a Cloudflare Tunnel for secure remote access.
Uses cloudflared CLI for tunnel operations.
"""

import re
import subprocess
import sys
from pathlib import Path

# Add project root to path for imports
sys.path.append(str(Path(__file__).parent.parent))
from scripts.setup_access import main as setup_access_main

CONFIG_DIR = Path.home() / ".cloudflared"


def get_input(prompt, default=None):
    """Get user input with optional default."""
    if default:
        user_input = input(f"{prompt} [{default}]: ").strip()
        return user_input if user_input else default
    else:
        return input(f"{prompt}: ").strip()


def run_cmd(cmd, check=True, capture=True):
    """Run a shell command and return output."""
    try:
        result = subprocess.run(
            cmd,
            shell=True,
            check=check,
            capture_output=capture,
            text=True,
        )
        return result.stdout.strip() if capture else None
    except subprocess.CalledProcessError as e:
        if capture:
            print(f"Error: {e.stderr}")
        return None


def check_cloudflared():
    """Check if cloudflared is installed."""
    result = run_cmd("which cloudflared", check=False)
    if not result:
        print("Error: cloudflared is not installed.")
        print("\nInstall instructions:")
        print(
            "  Ubuntu/Debian: curl -fsSL https://pkg.cloudflare.com/cloudflare-main.gpg | sudo tee /usr/share/keyrings/cloudflare-main.gpg &&"
        )
        print(
            "                 echo 'deb [signed-by=/usr/share/keyrings/cloudflare-main.gpg] https://pkg.cloudflare.com/cloudflared focal main' | sudo tee /etc/apt/sources.list.d/cloudflared.list &&"
        )
        print("                 sudo apt update && sudo apt install cloudflared")
        print("  macOS: brew install cloudflared")
        print(
            "  Other: https://developers.cloudflare.com/cloudflare-one/connections/connect-apps/install-and-setup/installation"
        )
        return False
    return True


def check_auth():
    """Check if already authenticated with Cloudflare."""
    cert_file = CONFIG_DIR / "cert.pem"
    if cert_file.exists():
        print(f"✓ Already authenticated ({cert_file} exists)")
        return True
    return False


def authenticate():
    """Authenticate with Cloudflare via browser."""
    print("\n--> Authenticating with Cloudflare...")
    print("A browser window will open. Please log in to your Cloudflare account.")
    input("Press Enter to continue...")

    # This opens a browser for OAuth
    result = subprocess.run(["cloudflared", "tunnel", "login"], check=False)

    if result.returncode == 0:
        print("✓ Authentication successful!")
        return True
    else:
        print("✗ Authentication failed. Please try again.")
        return False


def list_tunnels():
    """List existing tunnels and return as dict {name: uuid}."""
    output = run_cmd("cloudflared tunnel list", check=False)
    tunnels = {}
    if output:
        for line in output.split("\n")[1:]:
            parts = line.split()
            if len(parts) >= 2:
                uuid, name = parts[0], parts[1]
                tunnels[name] = uuid
    return tunnels


def create_tunnel(name):
    """Create a new tunnel and return its UUID."""
    print(f"\n--> Creating tunnel '{name}'...")
    output = run_cmd(f"cloudflared tunnel create {name}", check=False)

    if output:
        print(output)

    # Get UUID from list
    tunnels = list_tunnels()
    return tunnels.get(name)


def route_dns(uuid, hostname):
    """Route DNS for the tunnel."""
    print(f"\n--> Routing DNS for {hostname}...")
    result = run_cmd(f"cloudflared tunnel route dns {uuid} {hostname}", check=False)
    if result is not None:
        print(f"✓ DNS route created for {hostname}")
        return True
    return False


def generate_config(uuid, hostname, local_service):
    """Generate cloudflared config file."""
    config_file = CONFIG_DIR / "config.yml"
    cred_file = CONFIG_DIR / f"{uuid}.json"

    config = f"""tunnel: {uuid}
credentials-file: {cred_file}

ingress:
  - hostname: {hostname}
    service: {local_service}
  - service: http_status:404
"""

    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    with open(config_file, "w") as f:
        f.write(config)

    print(f"✓ Config generated at {config_file}")
    return config_file


def print_header(title):
    """Print a section header."""
    print("\n" + "=" * 60)
    print(f" {title}")
    print("=" * 60)


def install_system_service(tunnel_name):
    """Install cloudflared as a system service."""
    print_header("System Service Installation")
    print("This allows the tunnel to run automatically at startup.")
    
    if get_input("Install/Update cloudflared system service?", "y").lower() != "y":
        print("Skipping system service installation.")
        return

    config_src = CONFIG_DIR / "config.yml"
    etc_dir = "/etc/cloudflared"
    config_dest = f"{etc_dir}/config.yml"
    
    # Check if config exists using sudo/ls
    config_exists = subprocess.call(f"sudo ls {config_dest} > /dev/null 2>&1", shell=True) == 0
    
    if config_exists:
        print(f"\n! Configuration file already exists at {config_dest}")
        if get_input("Overwrite existing configuration?", "n").lower() != "y":
            print("Skipping configuration overwrite. Service restart only.")
            run_cmd("sudo systemctl daemon-reload", check=False, capture=False)
            run_cmd("sudo systemctl enable cloudflared", check=False, capture=False)
            run_cmd("sudo systemctl restart cloudflared", check=False, capture=False)
            return

    print("\n--> Installing system service (requires sudo)...")
    
    # 1. Create dir
    run_cmd(f"sudo mkdir -p {etc_dir}", check=True, capture=False)
    
    # 2. Copy credentials (json files)
    run_cmd(f"sudo cp {CONFIG_DIR}/*.json {etc_dir}/", check=True, capture=False)
    
    # 3. Copy config
    run_cmd(f"sudo cp {config_src} {config_dest}", check=True, capture=False)
    
    # 4. Update paths in config
    run_cmd(f"sudo sed -i 's|{CONFIG_DIR}|{etc_dir}|g' {config_dest}", check=True, capture=False)
    
    # 5. Install Service (ignore failure if already installed)
    run_cmd("sudo cloudflared service install", check=False, capture=False)
    
    # 6. Restart/Enable
    run_cmd("sudo systemctl daemon-reload", check=True, capture=False)
    run_cmd("sudo systemctl enable cloudflared", check=True, capture=False)
    run_cmd("sudo systemctl restart cloudflared", check=True, capture=False)
    
    print("✓ Service restarted.")
    run_cmd("sudo systemctl status cloudflared --no-pager -n 3", check=False, capture=False)


def main():
    print_header("Cloudflare Tunnel Setup")
    print("This script will configure a secure tunnel for remote access.")

    # 1. Check cloudflared installation
    if not check_cloudflared():
        return 1

    # 2. Get configuration from user
    print("\n--- Configuration ---")

    # Tunnel name
    tunnel_name = get_input("Tunnel name", "unifi-gate")

    # Hostname (e.g., home.example.com)
    print("\nThe hostname is how you'll access your server remotely.")
    print("Example: home.yourdomain.com")
    hostname = get_input("Hostname for remote access")

    if not hostname:
        print("Error: Hostname is required.")
        return 1

    # Validate hostname format
    if not re.match(r"^[a-zA-Z0-9]([a-zA-Z0-9-]*\.)+[a-zA-Z]{2,}$", hostname):
        print(f"Warning: '{hostname}' may not be a valid hostname format.")
        confirm = get_input("Continue anyway?", "n")
        if confirm.lower() != "y":
            return 1

    # Local service
    local_service = get_input("Local service URL", "http://localhost:8000")

    # 3. Authenticate if needed
    print("\n--- Authentication ---")
    if not check_auth():
        if not authenticate():
            return 1

    # 4. Create or use existing tunnel
    print("\n--- Tunnel Setup ---")
    tunnels = list_tunnels()

    if tunnel_name in tunnels:
        uuid = tunnels[tunnel_name]
        print(f"✓ Tunnel '{tunnel_name}' already exists (UUID: {uuid})")
    else:
        uuid = create_tunnel(tunnel_name)
        if not uuid:
            print(f"Error: Failed to create tunnel '{tunnel_name}'")
            return 1
        print(f"✓ Created tunnel '{tunnel_name}' (UUID: {uuid})")

    # 5. Route DNS
    route_dns(uuid, hostname)

    # 6. Generate config
    generate_config(uuid, hostname, local_service)

    # 7. Run Cloudflare Access Setup
    print("\n--- Cloudflare Access Setup ---")
    access_result = setup_access_main(hostname_arg=hostname)
    if access_result != 0:
        print("✗ Cloudflare Access setup failed or was cancelled.")
        # We don't return here because the user might still want to setup the tunnel service
        # return access_result

    # 8. System Service
    install_system_service(tunnel_name)

    # 9. Final Summary
    print_header("Setup Complete!")

    print(
        f"""
Your tunnel is configured for: {hostname}

To start the tunnel manually (if not running as service):
  cloudflared tunnel run {tunnel_name}

Don't forget to start your local server:
  ./venv/bin/python server.py
"""
    )


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n\nSetup cancelled.")
        sys.exit(1)