#!/usr/bin/env python3
"""
User Management CLI - Manage users and sync to Cloudflare KV.

Commands:
    set-admin <email>     - Bootstrap first admin or promote user to admin
    list                  - List all users with status and role
    invite <email>        - Create invite link for a new user
    approve <email>       - Approve a pending user
    reject <email>        - Reject a pending user
    delete <email>        - Delete a user
    sync                  - Push approved users to Cloudflare KV

Environment variables:
    CLOUDFLARE_ACCOUNT_ID  - Cloudflare account ID
    CLOUDFLARE_API_TOKEN   - Cloudflare API token with KV write access
    CLOUDFLARE_KV_NAMESPACE_ID - KV namespace ID for approved users
"""

import argparse
import os
import sys

from kv_sync import CloudflareKV, get_approved_users_from_kv, sync_approved_users_to_kv
from user_store import UserRole, UserStatus, UserStore

# Shared helper functions to reduce duplication


def get_user_or_exit(store: UserStore, email: str):
    """Get a user by email or exit with error if not found."""
    user = store.get_user(email)
    if not user:
        print(f"Error: User {email} not found")
        sys.exit(1)
    return user


def require_kv_configured(kv: CloudflareKV) -> None:
    """Exit with error if KV is not configured."""
    if not kv.is_configured():
        print("Cloudflare KV not configured. Set the following environment variables:")
        print("  CLOUDFLARE_ACCOUNT_ID")
        print("  CLOUDFLARE_API_TOKEN")
        print("  CLOUDFLARE_KV_NAMESPACE_ID")
        sys.exit(1)


def print_sync_reminder() -> None:
    """Print reminder to sync changes to Cloudflare KV."""
    print("\nRemember to run 'sync' to update Cloudflare KV!")


def cmd_set_admin(store: UserStore, email: str) -> None:
    """Set a user as admin (creates if doesn't exist)."""
    user = store.get_user(email)

    if user:
        # Update existing user to admin
        store.update_user(email, role=UserRole.ADMIN, status=UserStatus.APPROVED)
        print(f"Updated {email} to admin role (approved)")
    else:
        # Create new admin user
        store.create_user(
            email=email,
            role=UserRole.ADMIN,
            status=UserStatus.APPROVED,
        )
        print(f"Created admin user: {email}")

    print_sync_reminder()


def cmd_list(store: UserStore) -> None:
    """List all users."""
    users = store.list_users()

    if not users:
        print("No users found.")
        return

    # Header
    print(f"{'Email':<40} {'Role':<8} {'Status':<10} {'Invited By':<30}")
    print("-" * 90)

    for user in sorted(users, key=lambda u: (u.status != "approved", u.email)):
        invited_by = user.invited_by or "-"
        status_icon = {
            "approved": "[OK]",
            "pending": "[?]",
            "rejected": "[X]",
        }.get(user.status, "")
        print(f"{user.email:<40} {user.role:<8} {status_icon} {user.status:<6} {invited_by:<30}")

    print(f"\nTotal: {len(users)} users")

    # Summary
    approved = len([u for u in users if u.status == "approved"])
    pending = len([u for u in users if u.status == "pending"])
    rejected = len([u for u in users if u.status == "rejected"])
    print(f"Approved: {approved}, Pending: {pending}, Rejected: {rejected}")


def cmd_approve(store: UserStore, email: str) -> None:
    """Approve a pending user."""
    user = get_user_or_exit(store, email)

    if user.status == UserStatus.APPROVED.value:
        print(f"User {email} is already approved")
        return

    store.update_user(email, status=UserStatus.APPROVED)
    print(f"Approved user: {email}")
    print_sync_reminder()


def cmd_reject(store: UserStore, email: str) -> None:
    """Reject a user."""
    get_user_or_exit(store, email)

    store.update_user(email, status=UserStatus.REJECTED)
    print(f"Rejected user: {email}")
    print_sync_reminder()


def cmd_delete(store: UserStore, email: str) -> None:
    """Delete a user."""
    # Verify user exists first
    get_user_or_exit(store, email)

    store.delete_user(email)
    print(f"Deleted user: {email}")
    print_sync_reminder()


def cmd_invite(store: UserStore, email: str, base_url: str) -> None:
    """Create an invite for a new user."""
    # Check if user already exists
    existing = store.get_user(email)
    if existing:
        print(f"Error: User {email} already exists (status: {existing.status})")
        sys.exit(1)

    # Create invite
    invite = store.create_invite(email, invited_by="cli")
    invite_url = f"{base_url.rstrip('/')}/invite/{invite.token}"

    print(f"Invite created for: {email}")
    print("\nInvite URL (share with user):")
    print(f"  {invite_url}")
    print(f"\nExpires: {invite.expires_at}")


def cmd_sync(store: UserStore) -> None:
    """Sync approved users to Cloudflare KV."""
    kv = CloudflareKV()
    require_kv_configured(kv)

    # Get approved users
    approved_emails = store.get_approved_emails()
    print(f"Approved users to sync: {len(approved_emails)}")

    for email in approved_emails:
        print(f"  - {email}")

    # Sync to KV
    success, message = sync_approved_users_to_kv(approved_emails, kv)

    if success:
        print(f"\n{message}")
    else:
        print(f"\nError: {message}")
        sys.exit(1)


def cmd_show_kv(store: UserStore) -> None:
    """Show users currently in Cloudflare KV."""
    kv = CloudflareKV()
    require_kv_configured(kv)

    success, result = get_approved_users_from_kv(kv)

    if not success:
        print(f"Error: {result}")
        sys.exit(1)

    if not result:
        print("No users in Cloudflare KV")
        return

    print("Users in Cloudflare KV:")
    for email in sorted(result):
        print(f"  - {email}")
    print(f"\nTotal: {len(result)} users")


def main():
    parser = argparse.ArgumentParser(
        description="Manage users and sync to Cloudflare KV",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
    python manage_users.py set-admin admin@example.com
    python manage_users.py list
    python manage_users.py approve newuser@example.com
    python manage_users.py sync
        """,
    )

    parser.add_argument(
        "-c",
        "--config-dir",
        default=".",
        help="Configuration directory (default: current directory)",
    )

    subparsers = parser.add_subparsers(dest="command", help="Command to run")

    # set-admin command
    set_admin_parser = subparsers.add_parser("set-admin", help="Set a user as admin")
    set_admin_parser.add_argument("email", help="User email address")

    # list command
    subparsers.add_parser("list", help="List all users")

    # approve command
    approve_parser = subparsers.add_parser("approve", help="Approve a pending user")
    approve_parser.add_argument("email", help="User email address")

    # reject command
    reject_parser = subparsers.add_parser("reject", help="Reject a user")
    reject_parser.add_argument("email", help="User email address")

    # delete command
    delete_parser = subparsers.add_parser("delete", help="Delete a user")
    delete_parser.add_argument("email", help="User email address")

    # invite command
    invite_parser = subparsers.add_parser("invite", help="Create invite for a new user")
    invite_parser.add_argument("email", help="Email address to invite")
    invite_parser.add_argument(
        "--base-url",
        default=os.environ.get("INVITE_BASE_URL", "http://localhost:8000"),
        help="Base URL for invite link (default: INVITE_BASE_URL env var or http://localhost:8000)",
    )

    # sync command
    subparsers.add_parser("sync", help="Sync approved users to Cloudflare KV")

    # show-kv command
    subparsers.add_parser("show-kv", help="Show users in Cloudflare KV")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    # Initialize user store
    store = UserStore(config_dir=args.config_dir)

    # Dispatch command
    if args.command == "set-admin":
        cmd_set_admin(store, args.email)
    elif args.command == "list":
        cmd_list(store)
    elif args.command == "approve":
        cmd_approve(store, args.email)
    elif args.command == "reject":
        cmd_reject(store, args.email)
    elif args.command == "delete":
        cmd_delete(store, args.email)
    elif args.command == "invite":
        cmd_invite(store, args.email, args.base_url)
    elif args.command == "sync":
        cmd_sync(store)
    elif args.command == "show-kv":
        cmd_show_kv(store)


if __name__ == "__main__":
    main()
