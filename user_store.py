"""
User Store - JSON-based user management for Firebase authentication.

Stores users and invite tokens in users.json with the following structure:
{
  "users": {
    "user@example.com": {
      "role": "admin" | "user",
      "status": "pending" | "approved" | "rejected",
      "invited_by": "admin@example.com" | null,
      "invited_at": "2024-01-27T..." | null,
      "approved_at": "2024-01-27T..." | null,
      "rejected_at": "2024-01-27T..." | null
    }
  },
  "invites": {
    "token123": {
      "email": "new@example.com",
      "invited_by": "admin@example.com",
      "created_at": "...",
      "expires_at": "..."
    }
  }
}
"""

import json
import os
import secrets
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Dict, List, Optional


class UserRole(Enum):
    ADMIN = "admin"
    USER = "user"


class UserStatus(Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


@dataclass
class User:
    email: str
    role: str
    status: str
    invited_by: Optional[str] = None
    invited_at: Optional[str] = None
    approved_at: Optional[str] = None
    rejected_at: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "role": self.role,
            "status": self.status,
            "invited_by": self.invited_by,
            "invited_at": self.invited_at,
            "approved_at": self.approved_at,
            "rejected_at": self.rejected_at,
        }

    @classmethod
    def from_dict(cls, email: str, data: Dict[str, Any]) -> "User":
        return cls(
            email=email,
            role=data.get("role", UserRole.USER.value),
            status=data.get("status", UserStatus.PENDING.value),
            invited_by=data.get("invited_by"),
            invited_at=data.get("invited_at"),
            approved_at=data.get("approved_at"),
            rejected_at=data.get("rejected_at"),
        )


@dataclass
class Invite:
    token: str
    email: str
    invited_by: str
    created_at: str
    expires_at: str
    auto_approve: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "email": self.email,
            "invited_by": self.invited_by,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "auto_approve": self.auto_approve,
        }

    @classmethod
    def from_dict(cls, token: str, data: Dict[str, Any]) -> "Invite":
        return cls(
            token=token,
            email=data["email"],
            invited_by=data["invited_by"],
            created_at=data["created_at"],
            expires_at=data["expires_at"],
            auto_approve=data.get("auto_approve", False),
        )

    def is_expired(self) -> bool:
        expires = datetime.fromisoformat(self.expires_at)
        return datetime.now() > expires


class UserStore:
    """JSON-based user and invite storage."""

    INVITE_EXPIRY_HOURS = 48

    def __init__(self, config_dir: str = "."):
        self.config_dir = config_dir
        self.users_file = os.path.join(config_dir, "users.json")
        self._ensure_file_exists()

    def _ensure_file_exists(self) -> None:
        """Create users.json if it doesn't exist."""
        if not os.path.exists(self.users_file):
            self._save_data({"users": {}, "invites": {}})

    def _load_data(self) -> Dict[str, Any]:
        """Load the users.json file."""
        try:
            with open(self.users_file, "r") as f:
                return json.load(f)
        except (json.JSONDecodeError, FileNotFoundError):
            return {"users": {}, "invites": {}}

    def _save_data(self, data: Dict[str, Any]) -> None:
        """Save data to users.json."""
        with open(self.users_file, "w") as f:
            json.dump(data, f, indent=2)

    # =========== User Operations ===========

    def get_user(self, email: str) -> Optional[User]:
        """Get a user by email."""
        data = self._load_data()
        user_data = data.get("users", {}).get(email)
        if user_data:
            return User.from_dict(email, user_data)
        return None

    def create_user(
        self,
        email: str,
        role: UserRole = UserRole.USER,
        status: UserStatus = UserStatus.PENDING,
        invited_by: Optional[str] = None,
    ) -> User:
        """Create a new user."""
        data = self._load_data()
        now = datetime.now().isoformat()

        user = User(
            email=email,
            role=role.value,
            status=status.value,
            invited_by=invited_by,
            invited_at=now if invited_by else None,
            approved_at=now if status == UserStatus.APPROVED else None,
        )

        data["users"][email] = user.to_dict()
        self._save_data(data)
        return user

    def update_user(
        self,
        email: str,
        role: Optional[UserRole] = None,
        status: Optional[UserStatus] = None,
    ) -> Optional[User]:
        """Update a user's role or status."""
        data = self._load_data()
        if email not in data.get("users", {}):
            return None

        user_data = data["users"][email]
        now = datetime.now().isoformat()

        if role is not None:
            user_data["role"] = role.value

        if status is not None:
            user_data["status"] = status.value
            if status == UserStatus.APPROVED:
                user_data["approved_at"] = now
                user_data["rejected_at"] = None
            elif status == UserStatus.REJECTED:
                user_data["rejected_at"] = now
                user_data["approved_at"] = None

        self._save_data(data)
        return User.from_dict(email, user_data)

    def delete_user(self, email: str) -> bool:
        """Delete a user."""
        data = self._load_data()
        if email in data.get("users", {}):
            del data["users"][email]
            self._save_data(data)
            return True
        return False

    def list_users(self, status_filter: Optional[UserStatus] = None) -> List[User]:
        """List all users, optionally filtered by status."""
        data = self._load_data()
        users = []
        for email, user_data in data.get("users", {}).items():
            user = User.from_dict(email, user_data)
            if status_filter is None or user.status == status_filter.value:
                users.append(user)
        return users

    def is_approved(self, email: str) -> bool:
        """Check if a user is approved."""
        user = self.get_user(email)
        return user is not None and user.status == UserStatus.APPROVED.value

    def is_admin(self, email: str) -> bool:
        """Check if a user is an admin."""
        user = self.get_user(email)
        return user is not None and user.role == UserRole.ADMIN.value and user.status == UserStatus.APPROVED.value

    def get_approved_emails(self) -> List[str]:
        """Get list of approved user emails (for Cloudflare KV sync)."""
        return [u.email for u in self.list_users(UserStatus.APPROVED)]

    # =========== Invite Operations ===========

    def create_invite(self, email: str, invited_by: str, auto_approve: bool = False) -> Invite:
        """Create a new invite token for an email."""
        data = self._load_data()
        now = datetime.now()
        expires = now + timedelta(hours=self.INVITE_EXPIRY_HOURS)

        # Generate a secure random token
        token = secrets.token_urlsafe(32)

        invite = Invite(
            token=token,
            email=email,
            invited_by=invited_by,
            created_at=now.isoformat(),
            expires_at=expires.isoformat(),
            auto_approve=auto_approve,
        )

        data["invites"][token] = invite.to_dict()
        self._save_data(data)
        return invite

    def set_invite_auto_approve(self, token: str, auto_approve: bool = True) -> Optional[Invite]:
        """Set auto_approve on an existing invite."""
        data = self._load_data()
        if token not in data.get("invites", {}):
            return None
        data["invites"][token]["auto_approve"] = auto_approve
        self._save_data(data)
        return Invite.from_dict(token, data["invites"][token])

    def get_invite(self, token: str) -> Optional[Invite]:
        """Get an invite by token."""
        data = self._load_data()
        invite_data = data.get("invites", {}).get(token)
        if invite_data:
            return Invite.from_dict(token, invite_data)
        return None

    def validate_invite(self, token: str) -> tuple[bool, Optional[str], Optional[str]]:
        """
        Validate an invite token.
        Returns: (is_valid, email, error_message)
        """
        invite = self.get_invite(token)

        if invite is None:
            return False, None, "Invalid invite token"

        if invite.is_expired():
            return False, None, "Invite has expired"

        # Check if user already exists
        existing_user = self.get_user(invite.email)
        if existing_user:
            return False, invite.email, "User already exists"

        return True, invite.email, None

    def accept_invite(self, token: str, email: str) -> Optional[User]:
        """
        Accept an invite and create a user.
        The email from Firebase auth must match the invite email.
        If auto_approve is set, user is approved immediately.
        """
        invite = self.get_invite(token)

        if invite is None:
            return None

        if invite.is_expired():
            return None

        # Email must match the invite
        if invite.email.lower() != email.lower():
            return None

        # Create the user - approved if auto_approve, otherwise pending
        status = UserStatus.APPROVED if invite.auto_approve else UserStatus.PENDING
        user = self.create_user(
            email=invite.email,
            role=UserRole.USER,
            status=status,
            invited_by=invite.invited_by,
        )

        # Remove the used invite
        self.delete_invite(token)

        return user

    def delete_invite(self, token: str) -> bool:
        """Delete an invite token."""
        data = self._load_data()
        if token in data.get("invites", {}):
            del data["invites"][token]
            self._save_data(data)
            return True
        return False

    def cleanup_expired_invites(self) -> int:
        """Remove expired invites. Returns count of removed invites."""
        data = self._load_data()
        invites = data.get("invites", {})
        expired = []

        for token, invite_data in invites.items():
            invite = Invite.from_dict(token, invite_data)
            if invite.is_expired():
                expired.append(token)

        for token in expired:
            del data["invites"][token]

        if expired:
            self._save_data(data)

        return len(expired)

    def list_invites(self) -> List[Invite]:
        """List all active (non-expired) invites."""
        data = self._load_data()
        invites = []
        for token, invite_data in data.get("invites", {}).items():
            invite = Invite.from_dict(token, invite_data)
            if not invite.is_expired():
                invites.append(invite)
        return invites
