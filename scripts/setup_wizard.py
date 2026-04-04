import getpass
import ipaddress
import json
import os
import socket
import sys
import threading

import requests
from requests.packages.urllib3.exceptions import InsecureRequestWarning

# Add parent directory to path to import unifi_native_api
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from unifi_native_api import UniFiNativeAPI
except ImportError:
    print("Error: Could not import UniFiNativeAPI. Ensure unifi_native_api.py is in the project root.")
    sys.exit(1)

# Disable SSL warnings
requests.packages.urllib3.disable_warnings(InsecureRequestWarning)


def print_header(title):
    print("\n" + "=" * 60)
    print(f" {title}")
    print("=" * 60)


def get_input(prompt, default=None, is_password=False):
    if default:
        prompt_text = f"{prompt} [{default}]: "
    else:
        prompt_text = f"{prompt}: "

    if is_password:
        value = getpass.getpass(prompt_text)
    else:
        value = input(prompt_text)

    if not value and default:
        return default
    return value.strip()


def validate_dev_token(host, token):
    """Validate a Developer API token by making a test request."""
    try:
        url = f"https://{host}/api/v1/developer/doors"
        headers = {"Authorization": f"Bearer {token}"}
        resp = requests.get(url, headers=headers, verify=False, timeout=5)
        return resp.status_code == 200
    except Exception:
        return False


def generate_secure_password(length=20):
    """Generate a secure random password."""
    import secrets
    import string

    alphabet = string.ascii_letters + string.digits + "!@#$%^&*"
    return "".join(secrets.choice(alphabet) for _ in range(length))


# --- Network Discovery Logic ---
def get_local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        local_ip = s.getsockname()[0]
        s.close()
        return local_ip
    except Exception:
        return "127.0.0.1"


def get_subnet_ips(local_ip):
    try:
        ip = ipaddress.IPv4Address(local_ip)
        network = ipaddress.IPv4Network(f"{ip}/24", strict=False)
        return [str(ip) for ip in network.hosts()]
    except ValueError:
        return []


def scan_host(ip, port, results):
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(2.0)  # Increased timeout for reliability
        result = sock.connect_ex((ip, port))
        sock.close()
        if result == 0:
            try:
                url = f"https://{ip}"
                # Try both root and login path
                resp = requests.get(url, timeout=3, verify=False, allow_redirects=True)

                title = ""
                if "<title>" in resp.text:
                    start = resp.text.find("<title>") + 7
                    end = resp.text.find("</title>", start)
                    title = resp.text[start:end].strip()

                is_unifi = False

                # 1. Check Headers
                if "X-CSRF-Token" in resp.headers or "x-csrf-token" in resp.headers:
                    is_unifi = True

                # 2. Check Title/Content
                text_lower = resp.text.lower()
                if "unifi" in title.lower() or "ubiquiti" in text_lower:
                    is_unifi = True
                elif "ui.com" in text_lower:
                    is_unifi = True
                elif "unifi identity" in text_lower:
                    is_unifi = True

                # 3. Active Probe: Safe Login Attempt (to check for UniFi-specific error response)
                if not is_unifi:
                    try:
                        login_url = f"{url}/api/auth/login"
                        # Send dummy credentials to trigger a structured error response
                        dummy_data = {"username": "scan_probe", "password": "dummy_password"}
                        probe_resp = requests.post(login_url, json=dummy_data, timeout=3, verify=False)

                        # Check for UniFi specific headers again
                        if "X-CSRF-Token" in probe_resp.headers or "x-csrf-token" in probe_resp.headers:
                            is_unifi = True

                        # Check for JSON structure typical of UniFi
                        try:
                            data = probe_resp.json()
                            if isinstance(data, dict):
                                # UniFi often returns 'code' and 'msg' in errors
                                if "code" in data or "msg" in data or "meta" in data:
                                    is_unifi = True
                        except:
                            pass
                    except:
                        pass

                try:
                    hostname = socket.gethostbyaddr(ip)[0]
                except:
                    hostname = ""

                results.append({"ip": ip, "hostname": hostname, "title": title, "is_unifi": is_unifi})
            except:
                pass
    except:
        pass


def find_unifi_controllers():
    local_ip = get_local_ip()
    print(f"Scanning network ({local_ip}/24) for UniFi Controllers...")

    ips = get_subnet_ips(local_ip)
    results = []
    threads = []

    for ip in ips:
        t = threading.Thread(target=scan_host, args=(ip, 443, results))
        t.start()
        threads.append(t)

    for t in threads:
        t.join()

    results.sort(key=lambda x: (not x["is_unifi"], x["ip"]))
    return results


def select_controller():
    """Runs discovery and lets user pick a controller."""
    controllers = find_unifi_controllers()

    if not controllers:
        print("No controllers found automatically.")
        return get_input("UniFi Controller IP/Host")

    print("\nFound Candidates:")
    for i, c in enumerate(controllers):
        marker = "[UniFi]" if c["is_unifi"] else "[?]"
        name = f" ({c['hostname']})" if c["hostname"] else ""
        print(f"{i+1}. {marker} {c['ip']}{name} - {c['title']}")

    print(f"{len(controllers)+1}. Enter Manually")

    while True:
        choice = get_input("\nSelect Controller", default="1")
        try:
            idx = int(choice) - 1
            if idx == len(controllers):
                return get_input("UniFi Controller IP/Host")
            if 0 <= idx < len(controllers):
                return controllers[idx]["ip"]
        except ValueError:
            pass
        print("Invalid selection.")


# --- Setup Steps ---


def setup_native_api(default_host=None):
    print_header("1. Native API Setup (Advanced Features)")
    print("Log in with any Admin account. We'll create a dedicated service account")
    print("for background operations (no 2FA required on service account).")

    if default_host:
        host = get_input("\nUniFi Controller IP/Host", default_host)
    else:
        host = select_controller()

    print("\nEnter your Admin credentials (2FA is OK - you'll only need it once):")

    while True:
        username = get_input("Admin Username")
        password = get_input("Admin Password", is_password=True)

        if not password:
            print("Skipped Native API setup.")
            return host, False

        print(f"--> Verifying credentials against {host}...")
        try:
            api = UniFiNativeAPI(host=f"https://{host}", username=username, password=password)
            if api.login():
                print("✓ Login successful!")

                # Offer to create a dedicated service account
                print("\n" + "-" * 40)
                print("RECOMMENDED: Create a dedicated service account for background operations.")
                print("This account will have NO 2FA, allowing automatic re-login.")
                create_svc = get_input("Create a service account?", "y")

                final_username = username
                final_password = password

                if create_svc.lower() == "y":
                    # Get Super Admin role first
                    role_id = api.get_super_admin_role_id()
                    if not role_id:
                        print("❌ Could not find Super Admin role. Using original credentials.")
                    else:
                        svc_username = get_input("Service account username", "unifi-gate-svc")

                        while True:
                            svc_password = generate_secure_password()
                            print(f"--> Creating service account '{svc_username}'...")

                            user = api.create_user(
                                username=svc_username,
                                password=svc_password,
                                first_name="UniFi Gate",
                                last_name="Service",
                                role_id=role_id,
                            )

                            if user:
                                print(f"✓ Created service account '{svc_username}'")
                                final_username = svc_username
                                final_password = svc_password
                                break
                            else:
                                # Check if username already exists
                                print(f"❌ Failed to create '{svc_username}'.")
                                retry_choice = get_input("Try a different username?", "y")
                                if retry_choice.lower() != "y":
                                    print("Using original credentials.")
                                    break
                                svc_username = get_input("New service account username")

                # Save Credentials
                data = {"host": host, "username": final_username, "password": final_password}
                with open("credentials_native.json", "w") as f:
                    json.dump(data, f, indent=4)
                print("✓ Saved credentials_native.json")

                # Auto-generate Developer Token
                print("\n" + "-" * 40)
                auto_gen = get_input("Auto-generate Developer API Token?", "y")
                if auto_gen.lower() == "y":
                    import secrets

                    token_name = f"unifi-gate-{secrets.token_hex(4)}"
                    token = api.create_api_token(token_name)
                    if token:
                        dev_data = {"host": host, "token": token}

                        # Validate the newly generated token (with retry for propagation delay)
                        import time

                        print("--> Validating token...")
                        for attempt in range(3):
                            if validate_dev_token(host, token):
                                with open("credentials.json", "w") as f:
                                    json.dump(dev_data, f, indent=4)
                                print(f"✓ Generated token '{token_name}' and saved credentials.json")
                                return host, True  # True indicates dev setup is done
                            if attempt < 2:
                                time.sleep(1)  # Wait before retry

                        # Validation failed but token was created - save it anyway
                        print("⚠ Token validation timed out, but saving anyway (token may need a moment to activate)")
                        with open("credentials.json", "w") as f:
                            json.dump(dev_data, f, indent=4)
                        print(f"✓ Saved credentials.json with token '{token_name}'")
                        return host, True
                    else:
                        print("❌ Failed to auto-generate token. You'll need to do it manually.")

                return host, False  # Dev setup NOT done

            else:
                print("❌ Login failed.")
        except Exception as e:
            print(f"❌ Connection failed: {e}")

        retry = get_input("Retry?", "y")
        if retry.lower() != "y":
            return host, False


def setup_developer_api(default_host=None):
    print_header("2. UniFi Access Developer API Setup")

    # Use the known host directly, only prompt if we don't have one
    if default_host:
        host = default_host
        print(f"\nUsing controller: {host}")
    else:
        host = select_controller()

    print("\nHow to get your API Token:")
    print(f"1. Go to: https://{host}/access/settings/control-plane/integrations")
    print("2. In 'API Tokens', click 'Add Token'.")
    print("3. Name it 'unifi-gate' and copy the token.")

    while True:
        token = get_input("Developer API Token", is_password=True)
        if not token:
            print("Skipped Developer API setup.")
            return

        # Simple validation check using requests (UniFiNativeAPI logic is heavy for just this)
        print(f"--> Verifying Token...")
        try:
            url = f"https://{host}/api/v1/developer/doors"
            headers = {"Authorization": f"Bearer {token}"}
            resp = requests.get(url, headers=headers, verify=False, timeout=5)
            if resp.status_code == 200:
                print(f"✓ Success!")
                data = {"host": host, "token": token}
                with open("credentials.json", "w") as f:
                    json.dump(data, f, indent=4)
                print("✓ Saved credentials.json")
                return
            else:
                print(f"❌ Invalid Token (Status: {resp.status_code})")
        except Exception as e:
            print(f"❌ Connection failed: {e}")

        retry = get_input("Retry?", "y")
        if retry.lower() != "y":
            return


def setup_cloudflare():
    print_header("3. Cloudflare Tunnel & Access Setup")
    print("This is required for secure remote access.")
    print("\nHow to get these:")
    print("1. Go to https://dash.cloudflare.com/profile/api-tokens")
    print("2. Create Token > Template: 'Edit Cloudflare Workers' (or Custom with Account:Read, Access:Edit).")
    print("3. Account ID is in the URL of your dashboard: dash.cloudflare.com/<ACCOUNT_ID>")

    account_id = get_input("\nCloudflare Account ID")
    api_token = get_input("Cloudflare API Token", is_password=True)

    if api_token:
        with open(".env", "w") as f:
            f.write(f"CLOUDFLARE_ACCOUNT_ID='{account_id}'\n")
            f.write(f"CLOUDFLARE_API_TOKEN='{api_token}'\n")
        print("✓ Saved .env")
    else:
        print("Skipped Cloudflare setup.")


def setup_system_service():
    print_header("4. System Service Installation")
    print("This allows UniFi Gate to run automatically at startup.")

    # Detect environment
    try:
        user = os.getlogin()
    except Exception:
        # Fallback if getlogin fails (e.g. in some shells)
        try:
            import pwd
            user = pwd.getpwuid(os.getuid())[0]
        except Exception:
            user = get_input("Could not detect user. Please enter username", default="root")

    project_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    venv_python = os.path.join(project_dir, "venv", "bin", "python")

    if not os.path.exists(venv_python):
        # Fallback for .venv if venv doesn't exist
        venv_python = os.path.join(project_dir, ".venv", "bin", "python")
    
    if not os.path.exists(venv_python):
         # Final fallback, ask user or assume python3
         venv_python = get_input("Could not find venv. Enter python path", default="/usr/bin/python3")


    service_file_name = "unifi-gate.service"
    service_content = f"""[Unit]
Description=UniFi Gate Server
After=network.target

[Service]
User={user}
WorkingDirectory={project_dir}
ExecStart={venv_python} server.py
Restart=always
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
"""

    print(f"\nService File Content ({service_file_name}):")
    print("-" * 40)
    print(service_content.strip())
    print("-" * 40)

    choice = get_input("\nGenerate this service file?", "y")
    if choice.lower() == "y":
        file_path = os.path.join(project_dir, service_file_name)
        with open(file_path, "w") as f:
            f.write(service_content)

        print(f"✓ Saved {service_file_name}")
        print("\nTo install and enable the service, run these commands:")
        print(f"  sudo mv {service_file_name} /etc/systemd/system/")
        print("  sudo systemctl daemon-reload")
        print("  sudo systemctl enable unifi-gate")
        print("  sudo systemctl start unifi-gate")
        print(f"  sudo systemctl status unifi-gate")
    else:
        print("Skipped service generation.")


def main():
    print_header("UniFi Gate Setup Wizard")
    print("This script will help you create the necessary credential files.")

    # 1. Native API & Auto-Token
    dev_setup_done = False
    detected_host = None

    if not os.path.exists("credentials_native.json"):
        detected_host, dev_setup_done = setup_native_api()
    else:
        print("\n✓ credentials_native.json already exists. Skipping.")
        # Try to read host
        try:
            with open("credentials_native.json") as f:
                detected_host = json.load(f).get("host")
        except:
            pass

    # 2. Developer API (if not auto-generated)
    if not dev_setup_done and not os.path.exists("credentials.json"):
        setup_developer_api(default_host=detected_host)
    elif os.path.exists("credentials.json"):
        print("\n✓ credentials.json already exists. Skipping.")

    # 3. Cloudflare
    if not os.path.exists(".env"):
        setup_cloudflare()
    else:
        print("\n✓ .env already exists. Skipping.")

    # 4. System Service
    setup_system_service()

    print_header("Setup Complete")
    print("You can now proceed with installation.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nSetup cancelled.")
