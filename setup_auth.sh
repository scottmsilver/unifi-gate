#!/bin/bash

# ============================================================================
# UniFi Gate Authentication Setup Script
#
# Interactive wizard to set up Firebase + Cloudflare Worker authentication.
# Safe to re-run - checks if each step is already complete.
# ============================================================================

set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
NC='\033[0m' # No Color
BOLD='\033[1m'

# Project directory
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Config file to track progress
CONFIG_FILE="$SCRIPT_DIR/.auth_setup_config"

# Load existing config if present
if [ -f "$CONFIG_FILE" ]; then
    source "$CONFIG_FILE"
fi

# Helper functions
print_header() {
    echo ""
    echo -e "${BLUE}════════════════════════════════════════════════════════════════${NC}"
    echo -e "${BOLD}${CYAN}  $1${NC}"
    echo -e "${BLUE}════════════════════════════════════════════════════════════════${NC}"
    echo ""
}

print_step() {
    echo -e "${YELLOW}▶ $1${NC}"
}

print_success() {
    echo -e "${GREEN}✓ $1${NC}"
}

print_error() {
    echo -e "${RED}✗ $1${NC}"
}

print_info() {
    echo -e "${CYAN}ℹ $1${NC}"
}

print_warning() {
    echo -e "${YELLOW}⚠ $1${NC}"
}

prompt_continue() {
    echo ""
    read -p "Press Enter to continue (or Ctrl+C to exit)..."
    echo ""
}

prompt_yes_no() {
    local prompt="$1"
    local default="${2:-n}"

    if [ "$default" = "y" ]; then
        read -p "$prompt [Y/n]: " response
        response=${response:-y}
    else
        read -p "$prompt [y/N]: " response
        response=${response:-n}
    fi

    [[ "$response" =~ ^[Yy] ]]
}

save_config() {
    cat > "$CONFIG_FILE" << EOF
# Auth Setup Configuration - Auto-generated
FIREBASE_PROJECT_ID="$FIREBASE_PROJECT_ID"
FIREBASE_WEB_CLIENT_ID="$FIREBASE_WEB_CLIENT_ID"
FIREBASE_API_KEY="$FIREBASE_API_KEY"
CLOUDFLARE_KV_NAMESPACE_ID="$CLOUDFLARE_KV_NAMESPACE_ID"
CLOUDFLARE_ACCOUNT_ID="$CLOUDFLARE_ACCOUNT_ID"
CLOUDFLARE_TUNNEL_URL="$CLOUDFLARE_TUNNEL_URL"
ADMIN_EMAIL="$ADMIN_EMAIL"
STEP_COMPLETED="$STEP_COMPLETED"
EOF
}

mark_step_complete() {
    STEP_COMPLETED="$STEP_COMPLETED,$1"
    save_config
}

is_step_complete() {
    [[ "$STEP_COMPLETED" == *"$1"* ]]
}

# ============================================================================
# STEP 0: Check Prerequisites
# ============================================================================
check_prerequisites() {
    print_header "Step 0: Checking Prerequisites"

    local missing=()

    # Check for required CLIs
    print_step "Checking for required tools..."

    if command -v firebase &> /dev/null; then
        print_success "firebase-tools installed ($(firebase --version 2>/dev/null | head -1))"
    else
        missing+=("firebase-tools")
        print_error "firebase-tools not found"
    fi

    if command -v wrangler &> /dev/null; then
        print_success "wrangler installed ($(wrangler --version 2>/dev/null))"
    else
        missing+=("wrangler")
        print_error "wrangler not found"
    fi

    if command -v python &> /dev/null || command -v python3 &> /dev/null; then
        local py_cmd=$(command -v python3 || command -v python)
        print_success "Python installed ($($py_cmd --version))"
    else
        missing+=("python")
        print_error "Python not found"
    fi

    if command -v node &> /dev/null; then
        print_success "Node.js installed ($(node --version))"
    else
        missing+=("node")
        print_error "Node.js not found"
    fi

    # If anything is missing, show install commands
    if [ ${#missing[@]} -gt 0 ]; then
        echo ""
        print_warning "Missing tools. Install them with:"
        echo ""
        for tool in "${missing[@]}"; do
            case $tool in
                firebase-tools)
                    echo "  npm install -g firebase-tools"
                    ;;
                wrangler)
                    echo "  npm install -g wrangler"
                    ;;
                python)
                    echo "  # Install Python 3.8+ from https://python.org"
                    ;;
                node)
                    echo "  # Install Node.js from https://nodejs.org"
                    ;;
            esac
        done
        echo ""
        print_error "Please install missing tools and re-run this script."
        exit 1
    fi

    print_success "All prerequisites installed!"
    mark_step_complete "prerequisites"
}

# ============================================================================
# STEP 1: Firebase Login
# ============================================================================
setup_firebase_login() {
    print_header "Step 1: Firebase Authentication"

    print_step "Checking Firebase login status..."

    if firebase projects:list &> /dev/null; then
        print_success "Already logged in to Firebase"

        # Show current user
        local email=$(firebase login:list 2>/dev/null | grep -oE '[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}' | head -1)
        if [ -n "$email" ]; then
            print_info "Logged in as: $email"
        fi
    else
        print_step "Opening Firebase login..."
        echo ""
        print_info "A browser window will open. Please log in with your Google account."
        prompt_continue

        firebase login

        if firebase projects:list &> /dev/null; then
            print_success "Firebase login successful!"
        else
            print_error "Firebase login failed. Please try again."
            exit 1
        fi
    fi

    mark_step_complete "firebase_login"
}

# ============================================================================
# STEP 2: Firebase Project
# ============================================================================
setup_firebase_project() {
    print_header "Step 2: Firebase Project Setup"

    # Check if we already have a project configured
    if [ -n "$FIREBASE_PROJECT_ID" ]; then
        print_info "Previously configured project: $FIREBASE_PROJECT_ID"
        if prompt_yes_no "Use this project?"; then
            # Create/update .firebaserc file
            cat > "$SCRIPT_DIR/.firebaserc" << EOF
{
  "projects": {
    "default": "$FIREBASE_PROJECT_ID"
  }
}
EOF
            print_success "Using project: $FIREBASE_PROJECT_ID"
            mark_step_complete "firebase_project"
            return
        fi
    fi

    # List existing projects
    print_step "Fetching your Firebase projects..."
    echo ""

    local projects=$(firebase projects:list --json 2>/dev/null | grep -oE '"projectId": "[^"]+"' | cut -d'"' -f4)

    if [ -n "$projects" ]; then
        echo "Your existing Firebase projects:"
        echo ""
        local i=1
        while IFS= read -r project; do
            echo "  $i) $project"
            ((i++))
        done <<< "$projects"
        echo "  $i) Create a new project"
        echo ""

        read -p "Select a project (1-$i): " selection

        local project_count=$(echo "$projects" | wc -l)

        if [ "$selection" -le "$project_count" ] 2>/dev/null; then
            FIREBASE_PROJECT_ID=$(echo "$projects" | sed -n "${selection}p")
        else
            # Create new project
            echo ""
            read -p "Enter new project ID (e.g., unifi-gate): " FIREBASE_PROJECT_ID

            if [ -z "$FIREBASE_PROJECT_ID" ]; then
                print_error "Project ID cannot be empty"
                return 1
            fi

            print_step "Creating Firebase project: $FIREBASE_PROJECT_ID"
            firebase projects:create "$FIREBASE_PROJECT_ID" --display-name "UniFi Gate" || {
                print_warning "Project may already exist or name is taken. Trying to use it anyway..."
            }
        fi
    else
        echo "No existing projects found."
        read -p "Enter new project ID (e.g., unifi-gate): " FIREBASE_PROJECT_ID

        if [ -z "$FIREBASE_PROJECT_ID" ]; then
            print_error "Project ID cannot be empty"
            return 1
        fi

        print_step "Creating Firebase project: $FIREBASE_PROJECT_ID"
        firebase projects:create "$FIREBASE_PROJECT_ID" --display-name "UniFi Gate" || {
            print_warning "Project may already exist. Trying to use it anyway..."
        }
    fi

    # Create .firebaserc file to set active project (avoids 'firebase use' needing firebase init)
    print_step "Setting active project..."
    cat > "$SCRIPT_DIR/.firebaserc" << EOF
{
  "projects": {
    "default": "$FIREBASE_PROJECT_ID"
  }
}
EOF
    print_success "Created .firebaserc with project: $FIREBASE_PROJECT_ID"

    print_success "Firebase project configured: $FIREBASE_PROJECT_ID"
    save_config
    mark_step_complete "firebase_project"
}

# ============================================================================
# STEP 3: Enable Google Sign-In (Manual)
# ============================================================================
setup_google_signin() {
    print_header "Step 3: Enable Google Sign-In"

    if is_step_complete "google_signin"; then
        print_success "Google Sign-In already configured"
        if ! prompt_yes_no "Re-configure?"; then
            return
        fi
    fi

    print_warning "This step requires the Firebase Console (no CLI available)"
    echo ""
    echo "Please complete these steps in your browser:"
    echo ""
    echo -e "  ${BOLD}1.${NC} Open: ${CYAN}https://console.firebase.google.com/project/$FIREBASE_PROJECT_ID/authentication/providers${NC}"
    echo ""
    echo -e "  ${BOLD}2.${NC} Click ${BOLD}\"Google\"${NC} under \"Additional providers\""
    echo ""
    echo -e "  ${BOLD}3.${NC} Toggle ${BOLD}\"Enable\"${NC}"
    echo ""
    echo -e "  ${BOLD}4.${NC} Set your support email"
    echo ""
    echo -e "  ${BOLD}5.${NC} Click ${BOLD}\"Save\"${NC}"
    echo ""
    echo -e "  ${BOLD}6.${NC} Copy the ${BOLD}\"Web client ID\"${NC} shown (ends with .apps.googleusercontent.com)"
    echo ""

    # Try to open browser
    local url="https://console.firebase.google.com/project/$FIREBASE_PROJECT_ID/authentication/providers"
    if command -v xdg-open &> /dev/null; then
        print_info "Opening browser..."
        xdg-open "$url" 2>/dev/null &
    elif command -v open &> /dev/null; then
        print_info "Opening browser..."
        open "$url" 2>/dev/null &
    fi

    echo ""
    read -p "Paste the Web client ID here: " FIREBASE_WEB_CLIENT_ID

    if [[ ! "$FIREBASE_WEB_CLIENT_ID" == *".apps.googleusercontent.com" ]]; then
        print_warning "That doesn't look like a valid client ID (should end with .apps.googleusercontent.com)"
        read -p "Continue anyway? [y/N]: " confirm
        if [[ ! "$confirm" =~ ^[Yy] ]]; then
            return
        fi
    fi

    print_success "Web Client ID saved"

    # Add authorized domain
    echo ""
    print_warning "IMPORTANT: You must also add your domain to the authorized domains list"
    echo ""
    echo "Please complete these steps:"
    echo ""
    echo -e "  ${BOLD}1.${NC} Open: ${CYAN}https://console.firebase.google.com/project/$FIREBASE_PROJECT_ID/authentication/settings${NC}"
    echo ""
    echo -e "  ${BOLD}2.${NC} Scroll to ${BOLD}\"Authorized domains\"${NC}"
    echo ""
    echo -e "  ${BOLD}3.${NC} Click ${BOLD}\"Add domain\"${NC}"
    echo ""

    # Auto-detect domain from cloudflared config
    local auth_domain=""
    if [ -f "$HOME/.cloudflared/config.yml" ]; then
        auth_domain=$(grep -E '^\s*-?\s*hostname:' "$HOME/.cloudflared/config.yml" | head -1 | awk '{print $NF}')
    fi

    if [ -n "$auth_domain" ]; then
        echo -e "  ${BOLD}4.${NC} Add: ${CYAN}$auth_domain${NC}"
    else
        echo -e "  ${BOLD}4.${NC} Add your domain (e.g., home.example.com)"
    fi
    echo ""
    echo -e "  ${BOLD}5.${NC} Click ${BOLD}\"Add\"${NC}"
    echo ""

    # Open browser to auth settings
    local settings_url="https://console.firebase.google.com/project/$FIREBASE_PROJECT_ID/authentication/settings"
    if command -v xdg-open &> /dev/null; then
        print_info "Opening browser to Auth settings..."
        xdg-open "$settings_url" 2>/dev/null &
    elif command -v open &> /dev/null; then
        print_info "Opening browser to Auth settings..."
        open "$settings_url" 2>/dev/null &
    fi

    prompt_continue

    print_success "Google Sign-In configured"
    print_info "Web Client ID: $FIREBASE_WEB_CLIENT_ID"
    save_config
    mark_step_complete "google_signin"
}

# ============================================================================
# STEP 4: Firebase Web App
# ============================================================================
setup_firebase_web_app() {
    print_header "Step 4: Firebase Web App Configuration"

    print_step "Checking for existing web apps..."

    local existing_apps=$(firebase apps:list --json 2>/dev/null | grep -oE '"appId": "[^"]+"' | head -1)

    if [ -z "$existing_apps" ]; then
        print_step "Creating web app..."
        firebase apps:create web "UniFi Gate Web" 2>/dev/null || true
    else
        print_info "Web app already exists"
    fi

    print_step "Fetching web app configuration..."

    local config_output=$(firebase apps:sdkconfig web 2>/dev/null)

    # Extract API key (handles both JSON format: "apiKey": "..." and JS format: apiKey: "...")
    FIREBASE_API_KEY=$(echo "$config_output" | grep -oE '"apiKey":\s*"[^"]+"' | grep -oE 'AIza[^"]+')

    # Fallback to JS format if JSON didn't match
    if [ -z "$FIREBASE_API_KEY" ]; then
        FIREBASE_API_KEY=$(echo "$config_output" | grep -oE 'apiKey:\s*"[^"]+"' | grep -oE 'AIza[^"]+')
    fi

    if [ -z "$FIREBASE_API_KEY" ]; then
        print_warning "Could not extract API key automatically"
        echo ""
        echo "Run this command and copy the apiKey value:"
        echo "  firebase apps:sdkconfig web"
        echo ""
        read -p "Paste the apiKey here: " FIREBASE_API_KEY
    fi

    print_success "Firebase API Key: ${FIREBASE_API_KEY:0:10}..."

    # Firebase config is now injected via environment variables (not templates)
    print_info "Firebase config will be added to .env in step 11"

    save_config
    mark_step_complete "firebase_web"
}

# ============================================================================
# STEP 5: Firebase Android App
# ============================================================================
setup_firebase_android_app() {
    print_header "Step 5: Firebase Android App Configuration"

    local google_services="$SCRIPT_DIR/android-app/app/google-services.json"

    if [ -f "$google_services" ]; then
        print_success "google-services.json already exists"
        if ! prompt_yes_no "Regenerate it?"; then
            # Still need to update LoginScreen
            update_android_client_id
            mark_step_complete "firebase_android"
            return
        fi
    fi

    print_step "Creating Android app in Firebase..."
    firebase apps:create android "UniFi Gate Android" --package-name com.unifi.gate 2>/dev/null || {
        print_info "Android app may already exist"
    }

    print_step "Downloading google-services.json..."
    firebase apps:sdkconfig android -o "$google_services" 2>/dev/null || {
        print_error "Failed to download google-services.json"
        echo ""
        echo "Try manually:"
        echo "  firebase apps:sdkconfig android -o android-app/app/google-services.json"
        prompt_continue
    }

    if [ -f "$google_services" ]; then
        print_success "Downloaded google-services.json"
    fi

    # Add debug keystore SHA-1 fingerprint
    register_debug_sha1

    update_android_client_id

    save_config
    mark_step_complete "firebase_android"
}

register_debug_sha1() {
    print_step "Registering debug keystore SHA-1 fingerprint..."

    # Get the debug keystore SHA-1
    local sha1=$(keytool -list -v -keystore ~/.android/debug.keystore -alias androiddebugkey -storepass android 2>/dev/null | grep SHA1 | awk '{print $2}')

    if [ -z "$sha1" ]; then
        print_warning "Could not extract SHA-1 from debug keystore"
        return
    fi

    print_info "Debug SHA-1: $sha1"

    # Get the Firebase Android app ID
    local app_id=$(cat "$SCRIPT_DIR/android-app/app/google-services.json" 2>/dev/null | grep -o '"mobilesdk_app_id": "[^"]*"' | head -1 | cut -d'"' -f4)

    if [ -z "$app_id" ]; then
        print_warning "Could not find Firebase app ID in google-services.json"
        return
    fi

    # Register the SHA-1 with Firebase
    firebase apps:android:sha:create "$app_id" "$sha1" 2>/dev/null && {
        print_success "Registered SHA-1 fingerprint with Firebase"
    } || {
        print_info "SHA-1 may already be registered (this is OK)"
    }

    # Re-download google-services.json to include the new fingerprint
    local google_services="$SCRIPT_DIR/android-app/app/google-services.json"
    rm -f "$google_services"
    firebase apps:sdkconfig android -o "$google_services" 2>/dev/null
    print_success "Updated google-services.json with SHA-1"
}

update_android_client_id() {
    print_step "Updating Android LoginScreen with Web Client ID..."

    local login_screen="$SCRIPT_DIR/android-app/app/src/main/java/com/unifi/gate/ui/LoginScreen.kt"

    if [ -f "$login_screen" ] && [ -n "$FIREBASE_WEB_CLIENT_ID" ]; then
        if grep -q "YOUR_WEB_CLIENT_ID" "$login_screen"; then
            sed -i.bak \
                "s|YOUR_WEB_CLIENT_ID.apps.googleusercontent.com|$FIREBASE_WEB_CLIENT_ID|g" \
                "$login_screen"
            rm -f "${login_screen}.bak"
            print_success "Updated LoginScreen.kt"
        else
            print_info "LoginScreen.kt appears to already be configured"
        fi
    else
        print_warning "Could not update LoginScreen.kt - please update manually"
    fi
}

# ============================================================================
# STEP 6: Cloudflare Login
# ============================================================================
setup_cloudflare_login() {
    print_header "Step 6: Cloudflare Authentication"

    print_step "Checking Cloudflare login status..."

    if wrangler whoami &> /dev/null; then
        local cf_info=$(wrangler whoami 2>/dev/null)
        print_success "Already logged in to Cloudflare"
        echo "$cf_info" | head -5

        # Extract account ID
        CLOUDFLARE_ACCOUNT_ID=$(echo "$cf_info" | grep -oE '[a-f0-9]{32}' | head -1)
        if [ -n "$CLOUDFLARE_ACCOUNT_ID" ]; then
            print_info "Account ID: $CLOUDFLARE_ACCOUNT_ID"
            save_config
        fi
    else
        print_step "Opening Cloudflare login..."
        echo ""
        print_info "A browser window will open. Please log in with your Cloudflare account."
        prompt_continue

        wrangler login

        if wrangler whoami &> /dev/null; then
            print_success "Cloudflare login successful!"
            CLOUDFLARE_ACCOUNT_ID=$(wrangler whoami 2>/dev/null | grep -oE '[a-f0-9]{32}' | head -1)
            save_config
        else
            print_error "Cloudflare login failed. Please try again."
            exit 1
        fi
    fi

    mark_step_complete "cloudflare_login"
}

# ============================================================================
# STEP 7: Cloudflare Worker Setup
# ============================================================================
setup_cloudflare_worker() {
    print_header "Step 7: Cloudflare Worker Setup"

    cd "$SCRIPT_DIR/worker"

    # Install dependencies
    if [ ! -d "node_modules" ]; then
        print_step "Installing worker dependencies..."
        npm install
    else
        print_success "Worker dependencies already installed"
    fi

    # Create KV namespace
    print_step "Checking for KV namespace..."

    local kv_list=$(wrangler kv namespace list 2>/dev/null || echo "")

    if echo "$kv_list" | grep -q "APPROVED_USERS"; then
        print_success "KV namespace 'APPROVED_USERS' already exists"
        # Try to extract ID - look for the ID in JSON output
        CLOUDFLARE_KV_NAMESPACE_ID=$(echo "$kv_list" | grep -B5 "APPROVED_USERS" | grep -oE '"id":\s*"[a-f0-9]+"' | grep -oE '[a-f0-9]{32}' | head -1)
        if [ -z "$CLOUDFLARE_KV_NAMESPACE_ID" ]; then
            # Try alternative format
            CLOUDFLARE_KV_NAMESPACE_ID=$(echo "$kv_list" | grep -A1 "APPROVED_USERS" | grep -oE '[a-f0-9]{32}' | head -1)
        fi
    else
        print_step "Creating KV namespace..."
        local kv_output=$(wrangler kv namespace create APPROVED_USERS 2>&1)
        echo "$kv_output"

        # Extract namespace ID from output like: id = "abc123..."
        CLOUDFLARE_KV_NAMESPACE_ID=$(echo "$kv_output" | grep -oE 'id = "[a-f0-9]+"' | grep -oE '[a-f0-9]{32}')

        # Try JSON format if that didn't work
        if [ -z "$CLOUDFLARE_KV_NAMESPACE_ID" ]; then
            CLOUDFLARE_KV_NAMESPACE_ID=$(echo "$kv_output" | grep -oE '"id":\s*"[a-f0-9]+"' | grep -oE '[a-f0-9]{32}')
        fi

        if [ -z "$CLOUDFLARE_KV_NAMESPACE_ID" ]; then
            print_warning "Could not extract KV namespace ID automatically"
            echo ""
            echo "If the command failed due to permissions, run this in your terminal:"
            echo "  cd worker && wrangler kv namespace create APPROVED_USERS"
            echo ""
            read -p "Paste the KV namespace ID here: " CLOUDFLARE_KV_NAMESPACE_ID
        fi
    fi

    if [ -n "$CLOUDFLARE_KV_NAMESPACE_ID" ]; then
        print_success "KV Namespace ID: $CLOUDFLARE_KV_NAMESPACE_ID"

        # Update wrangler.toml
        print_step "Updating wrangler.toml..."
        sed -i.bak \
            "s|id = \"YOUR_KV_NAMESPACE_ID\"|id = \"$CLOUDFLARE_KV_NAMESPACE_ID\"|g" \
            wrangler.toml
        rm -f wrangler.toml.bak
        print_success "Updated wrangler.toml"
    fi

    save_config
    mark_step_complete "cloudflare_kv"

    cd "$SCRIPT_DIR"
}

# ============================================================================
# STEP 8: Worker Secrets
# ============================================================================
setup_worker_secrets() {
    print_header "Step 8: Cloudflare Worker Secrets"

    cd "$SCRIPT_DIR/worker"

    # Set FIREBASE_PROJECT_ID
    print_step "Setting FIREBASE_PROJECT_ID secret..."
    echo "$FIREBASE_PROJECT_ID" | wrangler secret put FIREBASE_PROJECT_ID 2>/dev/null || {
        print_warning "Failed to set secret automatically"
        echo "Run manually: wrangler secret put FIREBASE_PROJECT_ID"
        echo "Then enter: $FIREBASE_PROJECT_ID"
    }
    print_success "Set FIREBASE_PROJECT_ID"

    # Set ORIGIN_URL
    print_step "Setting ORIGIN_URL secret..."

    if [ -z "$CLOUDFLARE_TUNNEL_URL" ]; then
        # Try to auto-detect from cloudflared config
        if [ -f "$HOME/.cloudflared/config.yml" ]; then
            local detected_hostname=$(grep -E '^\s*-?\s*hostname:' "$HOME/.cloudflared/config.yml" | head -1 | awk '{print $NF}')
            if [ -n "$detected_hostname" ]; then
                CLOUDFLARE_TUNNEL_URL="https://$detected_hostname"
                print_success "Auto-detected tunnel URL: $CLOUDFLARE_TUNNEL_URL"
                save_config
            fi
        fi
    fi

    if [ -z "$CLOUDFLARE_TUNNEL_URL" ]; then
        echo ""
        print_info "You need your Cloudflare Tunnel origin URL."
        echo ""
        echo "Find it at: https://one.dash.cloudflare.com/ → Networks → Tunnels"
        echo "Look for the public hostname or tunnel URL (e.g., https://xxx.cfargotunnel.com)"
        echo ""
        read -p "Enter your tunnel/origin URL: " CLOUDFLARE_TUNNEL_URL
        save_config
    fi

    echo "$CLOUDFLARE_TUNNEL_URL" | wrangler secret put ORIGIN_URL 2>/dev/null || {
        print_warning "Failed to set secret automatically"
        echo "Run manually: wrangler secret put ORIGIN_URL"
        echo "Then enter: $CLOUDFLARE_TUNNEL_URL"
    }
    print_success "Set ORIGIN_URL"

    cd "$SCRIPT_DIR"
    mark_step_complete "worker_secrets"
}

# ============================================================================
# STEP 9: Deploy Worker
# ============================================================================
deploy_worker() {
    print_header "Step 9: Deploy Cloudflare Worker"

    cd "$SCRIPT_DIR/worker"

    print_step "Deploying worker..."

    if wrangler deploy; then
        print_success "Worker deployed successfully!"
    else
        print_error "Worker deployment failed"
        echo "Try running manually: cd worker && wrangler deploy"
    fi

    cd "$SCRIPT_DIR"
    mark_step_complete "worker_deploy"
}

# ============================================================================
# STEP 10: Add Domain Route
# ============================================================================
setup_worker_route() {
    print_header "Step 10: Add Domain Route to Worker"

    # Auto-detect hostname from cloudflared config
    local hostname=""
    if [ -f "$HOME/.cloudflared/config.yml" ]; then
        hostname=$(grep -E '^\s*-?\s*hostname:' "$HOME/.cloudflared/config.yml" | head -1 | awk '{print $NF}')
    fi

    if [ -z "$hostname" ]; then
        echo ""
        print_info "Enter the domain you want to route through the auth worker."
        print_info "This should be your public hostname (e.g., home.example.com)"
        echo ""
        read -p "Domain: " hostname
    else
        print_info "Detected hostname: $hostname"
        if ! prompt_yes_no "Use this hostname?"; then
            read -p "Enter hostname: " hostname
        fi
    fi

    if [ -z "$hostname" ]; then
        print_error "Hostname cannot be empty"
        return 1
    fi

    # Extract zone name (last two parts of hostname)
    local zone_name=$(echo "$hostname" | awk -F. '{print $(NF-1)"."$NF}')

    print_step "Adding route $hostname/* to worker..."

    cd "$SCRIPT_DIR/worker"

    # Get zone ID
    print_step "Looking up zone ID for $zone_name..."
    local zone_info=$(wrangler pages project list 2>/dev/null || true)

    # Use wrangler to add route via updating wrangler.toml and redeploying
    # First, update wrangler.toml with the route
    if grep -q "^# \[\[routes\]\]" wrangler.toml; then
        # Uncomment and update the routes section
        sed -i.bak \
            -e "s|^# \[\[routes\]\]|[[routes]]|" \
            -e "s|^# pattern = \"gate.example.com/\*\"|pattern = \"$hostname/*\"|" \
            -e "s|^# zone_name = \"example.com\"|zone_name = \"$zone_name\"|" \
            wrangler.toml
        rm -f wrangler.toml.bak
        print_success "Updated wrangler.toml with route"
    elif ! grep -q "pattern = \"$hostname" wrangler.toml; then
        # Add routes section if not present
        cat >> wrangler.toml << EOF

[[routes]]
pattern = "$hostname/*"
zone_name = "$zone_name"
EOF
        print_success "Added route to wrangler.toml"
    else
        print_info "Route already configured in wrangler.toml"
    fi

    # Redeploy to apply route
    print_step "Redeploying worker with route..."
    if wrangler deploy; then
        print_success "Worker deployed with route: $hostname/*"
    else
        print_error "Deployment failed"
        echo ""
        echo "You may need to add the route manually in Cloudflare Dashboard:"
        echo "  Workers & Pages → unifi-gate-auth → Settings → Triggers → Add Route"
        echo "  Route: $hostname/*"
    fi

    cd "$SCRIPT_DIR"
    mark_step_complete "worker_route"
}

# ============================================================================
# STEP 11: Backend Environment
# ============================================================================
setup_backend_env() {
    print_header "Step 11: Backend Environment"

    local env_file="$SCRIPT_DIR/.env"

    print_step "Setting up environment variables..."

    # Check if .env exists
    if [ -f "$env_file" ]; then
        print_info "Existing .env file found"
        source "$env_file" 2>/dev/null || true
    fi

    # Get Cloudflare API token if not set
    if [ -z "$CLOUDFLARE_API_TOKEN" ]; then
        echo ""
        print_info "You need a Cloudflare API token with Workers KV write access."
        echo ""
        echo "Create one at: https://dash.cloudflare.com/profile/api-tokens"
        echo "Use template: 'Edit Cloudflare Workers' or create custom with KV Storage Edit"
        echo ""
        read -p "Paste your Cloudflare API token: " CLOUDFLARE_API_TOKEN
    fi

    # Write .env file
    cat > "$env_file" << EOF
# Cloudflare Configuration
CLOUDFLARE_ACCOUNT_ID=$CLOUDFLARE_ACCOUNT_ID
CLOUDFLARE_API_TOKEN=$CLOUDFLARE_API_TOKEN
CLOUDFLARE_KV_NAMESPACE_ID=$CLOUDFLARE_KV_NAMESPACE_ID

# Firebase Configuration (client-side keys - safe to expose)
FIREBASE_API_KEY=$FIREBASE_API_KEY
FIREBASE_AUTH_DOMAIN=${FIREBASE_PROJECT_ID}.firebaseapp.com
FIREBASE_PROJECT_ID=$FIREBASE_PROJECT_ID

# Optional: Resend API for invite emails
# RESEND_API_KEY=re_...
EOF

    print_success "Created .env file with Cloudflare and Firebase config"

    # Source it
    export CLOUDFLARE_ACCOUNT_ID
    export CLOUDFLARE_API_TOKEN
    export CLOUDFLARE_KV_NAMESPACE_ID

    mark_step_complete "backend_env"
}

# ============================================================================
# STEP 12: Bootstrap Admin
# ============================================================================
bootstrap_admin() {
    print_header "Step 12: Bootstrap Admin User"

    cd "$SCRIPT_DIR"

    # Get admin email
    if [ -z "$ADMIN_EMAIL" ]; then
        echo ""
        print_info "Enter the email address for the first admin user."
        print_info "This should be the Google account you'll use to sign in."
        echo ""
        read -p "Admin email: " ADMIN_EMAIL
        save_config
    else
        print_info "Admin email: $ADMIN_EMAIL"
        if ! prompt_yes_no "Use this email?"; then
            read -p "New admin email: " ADMIN_EMAIL
            save_config
        fi
    fi

    # Run manage_users.py
    print_step "Creating admin user..."

    local py_cmd=$(command -v python3 || command -v python)

    $py_cmd manage_users.py set-admin "$ADMIN_EMAIL"

    print_success "Admin user created"

    # List users
    print_step "Current users:"
    $py_cmd manage_users.py list

    mark_step_complete "admin_user"
}

# ============================================================================
# STEP 13: Sync to KV
# ============================================================================
sync_to_kv() {
    print_header "Step 13: Sync Users to Cloudflare KV"

    cd "$SCRIPT_DIR"

    # Source .env
    if [ -f ".env" ]; then
        export $(grep -v '^#' .env | xargs) 2>/dev/null || true
    fi

    # Determine python command
    local py_cmd="python3"
    if [ -f ".venv/bin/python3" ]; then
        py_cmd=".venv/bin/python3"
    fi

    # Check if users.json exists
    if [ ! -f "users.json" ]; then
        print_error "users.json not found - run bootstrap_admin first"
        return 1
    fi

    # Get KV namespace ID
    if [ -z "$CLOUDFLARE_KV_NAMESPACE_ID" ]; then
        # Try to get from wrangler.toml
        CLOUDFLARE_KV_NAMESPACE_ID=$(grep -oE 'id = "[a-f0-9]+"' worker/wrangler.toml 2>/dev/null | head -1 | grep -oE '[a-f0-9]{32}')
    fi

    if [ -z "$CLOUDFLARE_KV_NAMESPACE_ID" ]; then
        print_error "KV namespace ID not found"
        return 1
    fi

    print_step "Syncing approved users to Cloudflare KV..."

    # Use wrangler directly (more reliable than API)
    local synced=0
    local emails=$($py_cmd -c "
import json
with open('users.json') as f:
    d = json.load(f)
for email, user in d.get('users', {}).items():
    if user.get('status') == 'approved':
        print(email)
" 2>/dev/null)

    if [ -z "$emails" ]; then
        print_warning "No approved users to sync"
        mark_step_complete "kv_sync"
        return 0
    fi

    for email in $emails; do
        local user_data=$($py_cmd -c "
import json
with open('users.json') as f:
    d = json.load(f)
u = d.get('users', {}).get('$email', {})
print(json.dumps({'role': u.get('role', 'user'), 'status': u.get('status', 'pending')}))
" 2>/dev/null)

        if [ -n "$user_data" ]; then
            print_info "Syncing $email..."
            cd "$SCRIPT_DIR/worker"
            if timeout 10 wrangler kv key put "$email" "$user_data" --namespace-id="$CLOUDFLARE_KV_NAMESPACE_ID" --remote 2>&1; then
                ((synced++))
            else
                print_warning "Timeout or error syncing $email"
            fi
            cd "$SCRIPT_DIR"
        fi
    done

    if [ $synced -gt 0 ]; then
        print_success "Synced $synced user(s) to Cloudflare KV"
    else
        print_warning "No users synced - they may already be in KV"
    fi

    mark_step_complete "kv_sync"
}

# ============================================================================
# STEP 14: Verification
# ============================================================================
verify_setup() {
    print_header "Step 14: Verify Setup"

    local all_good=true

    echo "Checking configuration..."
    echo ""

    # Check Firebase config in .env (templates now use Jinja2 injection from env vars)
    if [ -f ".env" ] && grep -q "FIREBASE_API_KEY=AIza" .env 2>/dev/null; then
        print_success "Firebase config in .env"
    else
        print_error ".env missing Firebase config (FIREBASE_API_KEY)"
        all_good=false
    fi

    # Check google-services.json
    if [ -f "android-app/app/google-services.json" ]; then
        print_success "google-services.json exists"
    else
        print_error "google-services.json missing"
        all_good=false
    fi

    # Check LoginScreen.kt
    if grep -q "YOUR_WEB_CLIENT_ID" android-app/app/src/main/java/com/unifi/gate/ui/LoginScreen.kt 2>/dev/null; then
        print_error "LoginScreen.kt still has placeholder values"
        all_good=false
    else
        print_success "LoginScreen.kt configured"
    fi

    # Check wrangler.toml
    if grep -q "YOUR_KV_NAMESPACE_ID" worker/wrangler.toml 2>/dev/null; then
        print_error "worker/wrangler.toml still has placeholder values"
        all_good=false
    else
        print_success "worker/wrangler.toml configured"
    fi

    # Check .env
    if [ -f ".env" ]; then
        print_success ".env file exists"
    else
        print_error ".env file missing"
        all_good=false
    fi

    # Check users.json
    if [ -f "users.json" ]; then
        local user_count=$(grep -c '"status"' users.json 2>/dev/null || echo "0")
        print_success "users.json exists ($user_count user(s))"
    else
        print_error "users.json missing - run: python manage_users.py set-admin your@email.com"
        all_good=false
    fi

    echo ""

    if $all_good; then
        print_success "All checks passed!"
    else
        print_warning "Some checks failed - review the errors above"
    fi

    mark_step_complete "verify"
}

# ============================================================================
# Final Summary
# ============================================================================
print_summary() {
    print_header "Setup Complete!"

    echo "Configuration Summary:"
    echo ""
    echo "  Firebase Project:     $FIREBASE_PROJECT_ID"
    echo "  Firebase API Key:     ${FIREBASE_API_KEY:0:15}..."
    echo "  Web Client ID:        ${FIREBASE_WEB_CLIENT_ID:0:30}..."
    echo "  Cloudflare Account:   $CLOUDFLARE_ACCOUNT_ID"
    echo "  KV Namespace:         $CLOUDFLARE_KV_NAMESPACE_ID"
    echo "  Admin Email:          $ADMIN_EMAIL"
    echo ""

    print_header "Next Steps"

    echo "1. Test the setup:"
    echo ""
    echo "   # Development mode (no auth required):"
    echo "   python server.py --dev"
    echo ""
    echo "   # Production mode (auth required):"
    echo "   python server.py"
    echo ""

    echo "2. Build and run the Android app:"
    echo "   cd android-app && ./gradlew installDebug"
    echo ""

    echo "Configuration saved to: $CONFIG_FILE"
    echo "You can re-run this script anytime to verify or update settings."
}

# ============================================================================
# Main Menu
# ============================================================================
main_menu() {
    while true; do
        print_header "UniFi Gate Auth Setup"

        echo "Select an option:"
        echo ""
        echo "  1) Run full setup (all steps)"
        echo "  2) Run from specific step"
        echo "  3) Verify current setup"
        echo "  4) Show configuration"
        echo "  5) Reset and start over"
        echo "  q) Quit"
        echo ""

        read -p "Choice: " choice

        case $choice in
            1)
                run_full_setup
                ;;
            2)
                select_step
                ;;
            3)
                verify_setup
                prompt_continue
                ;;
            4)
                show_config
                prompt_continue
                ;;
            5)
                if prompt_yes_no "Are you sure you want to reset?"; then
                    rm -f "$CONFIG_FILE"
                    STEP_COMPLETED=""
                    print_success "Configuration reset"
                fi
                ;;
            q|Q)
                echo "Goodbye!"
                exit 0
                ;;
            *)
                print_error "Invalid choice"
                ;;
        esac
    done
}

run_full_setup() {
    check_prerequisites
    setup_firebase_login
    setup_firebase_project
    setup_google_signin
    setup_firebase_web_app
    setup_firebase_android_app
    setup_cloudflare_login
    setup_cloudflare_worker
    setup_worker_secrets
    deploy_worker
    setup_worker_route
    setup_backend_env
    bootstrap_admin
    sync_to_kv
    verify_setup
    print_summary
}

select_step() {
    echo ""
    echo "Steps:"
    echo "  0) Check prerequisites"
    echo "  1) Firebase login"
    echo "  2) Firebase project"
    echo "  3) Enable Google Sign-In"
    echo "  4) Firebase web app"
    echo "  5) Firebase Android app"
    echo "  6) Cloudflare login"
    echo "  7) Cloudflare Worker setup"
    echo "  8) Worker secrets"
    echo "  9) Deploy worker"
    echo " 10) Add domain route"
    echo " 11) Backend environment"
    echo " 12) Bootstrap admin"
    echo " 13) Sync to KV"
    echo " 14) Verify setup"
    echo ""
    read -p "Start from step: " step

    case $step in
        0) check_prerequisites ;&
        1) setup_firebase_login ;&
        2) setup_firebase_project ;&
        3) setup_google_signin ;&
        4) setup_firebase_web_app ;&
        5) setup_firebase_android_app ;&
        6) setup_cloudflare_login ;&
        7) setup_cloudflare_worker ;&
        8) setup_worker_secrets ;&
        9) deploy_worker ;&
        10) setup_worker_route ;&
        11) setup_backend_env ;&
        12) bootstrap_admin ;&
        13) sync_to_kv ;&
        14) verify_setup; print_summary ;;
        *) print_error "Invalid step" ;;
    esac
}

show_config() {
    print_header "Current Configuration"

    if [ -f "$CONFIG_FILE" ]; then
        cat "$CONFIG_FILE"
    else
        echo "No configuration saved yet."
    fi

    echo ""
    echo "Completed steps: $STEP_COMPLETED"
}

# ============================================================================
# Entry Point
# ============================================================================

# Check if running with arguments
if [ "$1" = "--auto" ] || [ "$1" = "-a" ]; then
    run_full_setup
elif [ "$1" = "--verify" ] || [ "$1" = "-v" ]; then
    verify_setup
elif [ "$1" = "--help" ] || [ "$1" = "-h" ]; then
    echo "Usage: $0 [option]"
    echo ""
    echo "Options:"
    echo "  --auto, -a     Run full setup automatically"
    echo "  --verify, -v   Verify current setup"
    echo "  --help, -h     Show this help"
    echo ""
    echo "Without options, runs interactive menu."
else
    main_menu
fi
