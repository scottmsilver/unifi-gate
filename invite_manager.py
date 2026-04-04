"""
Invite Manager - Resend email integration for sending invite emails.

Requires RESEND_API_KEY environment variable.
Optionally uses INVITE_BASE_URL for the invite link (defaults to request origin).
"""

import os
from dataclasses import dataclass
from typing import Optional

try:
    import resend

    RESEND_AVAILABLE = True
except ImportError:
    RESEND_AVAILABLE = False


@dataclass
class InviteEmailResult:
    success: bool
    message_id: Optional[str] = None
    error: Optional[str] = None


class InviteManager:
    """Manages sending invite emails via Resend."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        from_email: str = "UniFi Gate <noreply@gate.example.com>",
        app_name: str = "UniFi Gate",
    ):
        self.api_key = api_key or os.environ.get("RESEND_API_KEY")
        self.from_email = from_email
        self.app_name = app_name

        if self.api_key and RESEND_AVAILABLE:
            resend.api_key = self.api_key

    def is_configured(self) -> bool:
        """Check if Resend is properly configured."""
        return bool(self.api_key and RESEND_AVAILABLE)

    def send_invite(
        self,
        to_email: str,
        invite_token: str,
        invited_by: str,
        base_url: str,
    ) -> InviteEmailResult:
        """
        Send an invite email.

        Args:
            to_email: Recipient email address
            invite_token: The invite token for the URL
            invited_by: Email of the admin who sent the invite
            base_url: Base URL for the invite link (e.g., https://gate.example.com)
        """
        if not self.is_configured():
            if not RESEND_AVAILABLE:
                return InviteEmailResult(
                    success=False,
                    error="Resend library not installed. Run: pip install resend",
                )
            return InviteEmailResult(
                success=False,
                error="RESEND_API_KEY not configured",
            )

        # Build the invite URL
        invite_url = f"{base_url.rstrip('/')}/invite/{invite_token}"

        # Build the email
        subject = f"You've been invited to {self.app_name}"
        html_body = self._build_html_email(to_email, invite_url, invited_by)
        text_body = self._build_text_email(to_email, invite_url, invited_by)

        try:
            params = {
                "from": self.from_email,
                "to": [to_email],
                "subject": subject,
                "html": html_body,
                "text": text_body,
            }

            response = resend.Emails.send(params)
            return InviteEmailResult(
                success=True,
                message_id=response.get("id"),
            )
        except Exception as e:
            return InviteEmailResult(
                success=False,
                error=str(e),
            )

    def _build_html_email(self, to_email: str, invite_url: str, invited_by: str) -> str:
        """Build the HTML email body."""
        return f"""
<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
</head>
<body style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; line-height: 1.6; color: #333; max-width: 600px; margin: 0 auto; padding: 20px;">
    <div style="background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); padding: 30px; border-radius: 10px 10px 0 0;">
        <h1 style="color: white; margin: 0; font-size: 24px;">{self.app_name}</h1>
    </div>

    <div style="background: #f9f9f9; padding: 30px; border-radius: 0 0 10px 10px;">
        <p style="font-size: 16px;">Hi,</p>

        <p style="font-size: 16px;">
            <strong>{invited_by}</strong> has invited you to join {self.app_name}.
        </p>

        <p style="font-size: 16px;">
            Click the button below to create your account and get access:
        </p>

        <div style="text-align: center; margin: 30px 0;">
            <a href="{invite_url}"
               style="background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
                      color: white;
                      padding: 15px 40px;
                      text-decoration: none;
                      border-radius: 5px;
                      font-weight: bold;
                      font-size: 16px;
                      display: inline-block;">
                Accept Invitation
            </a>
        </div>

        <p style="font-size: 14px; color: #666;">
            Or copy and paste this link into your browser:
            <br>
            <a href="{invite_url}" style="color: #667eea; word-break: break-all;">{invite_url}</a>
        </p>

        <hr style="border: none; border-top: 1px solid #ddd; margin: 30px 0;">

        <p style="font-size: 12px; color: #999;">
            This invitation will expire in 48 hours.
            <br>
            If you didn't expect this invitation, you can safely ignore this email.
        </p>
    </div>
</body>
</html>
"""

    def _build_text_email(self, to_email: str, invite_url: str, invited_by: str) -> str:
        """Build the plain text email body."""
        return f"""
{self.app_name}
{'=' * len(self.app_name)}

Hi,

{invited_by} has invited you to join {self.app_name}.

Click the link below to create your account and get access:

{invite_url}

This invitation will expire in 48 hours.

If you didn't expect this invitation, you can safely ignore this email.
"""


# Convenience function for simple usage
def send_invite_email(
    to_email: str,
    invite_token: str,
    invited_by: str,
    base_url: str,
    from_email: Optional[str] = None,
    app_name: Optional[str] = None,
) -> InviteEmailResult:
    """
    Send an invite email using default configuration.

    Uses RESEND_API_KEY from environment.
    """
    kwargs = {}
    if from_email:
        kwargs["from_email"] = from_email
    if app_name:
        kwargs["app_name"] = app_name

    manager = InviteManager(**kwargs)
    return manager.send_invite(to_email, invite_token, invited_by, base_url)
