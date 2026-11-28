"""Transactional email delivery for myBurnRate Cloud (Resend HTTP API + test fake)."""
from __future__ import annotations

import html
import os
import threading
from dataclasses import dataclass
from typing import Protocol

import httpx

from spend_app import __version__

RESEND_API_URL = "https://api.resend.com/emails"
DEFAULT_EMAIL_FROM = "myBurnRate <no-reply@myburnrate.io>"


@dataclass(frozen=True)
class OutboundEmail:
    to: str
    subject: str
    text: str
    html: str
    tag: str = ""


class EmailDeliveryError(RuntimeError):
    """Raised when the upstream transactional email provider fails."""


class EmailSender(Protocol):
    def send(self, message: OutboundEmail) -> None: ...


class MemoryEmailSender:
    """Thread-safe in-memory email sink for deterministic tests and local dev."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.sent: list[OutboundEmail] = []

    def send(self, message: OutboundEmail) -> None:
        with self._lock:
            self.sent.append(message)

    def clear(self) -> None:
        with self._lock:
            self.sent.clear()

    def last(self, *, to: str | None = None, tag: str | None = None) -> OutboundEmail | None:
        with self._lock:
            for item in reversed(self.sent):
                if to is not None and item.to.lower() != to.lower():
                    continue
                if tag is not None and item.tag != tag:
                    continue
                return item
        return None


class ResendEmailSender:
    """Minimal HTTPS Resend sender (trust_env=False, pinned endpoint, no redirects)."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        from_address: str | None = None,
        timeout: float = 10.0,
        client: httpx.Client | None = None,
    ) -> None:
        self._api_key = (api_key if api_key is not None else os.getenv("BURNRATE_RESEND_API_KEY", "")).strip()
        self._from = (
            from_address
            if from_address is not None
            else os.getenv("BURNRATE_EMAIL_FROM", DEFAULT_EMAIL_FROM)
        ).strip() or DEFAULT_EMAIL_FROM
        self._timeout = float(timeout)
        self._client = client

    def send(self, message: OutboundEmail) -> None:
        if not self._api_key:
            raise EmailDeliveryError("Email delivery is not configured on this cloud server.")
        for field_val in (self._from, message.to, message.subject):
            if "\r" in field_val or "\n" in field_val:
                raise EmailDeliveryError("Invalid control character in email header.")
        payload = {
            "from": self._from,
            "to": [message.to],
            "subject": message.subject,
            "text": message.text,
            "html": message.html,
        }
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
            "User-Agent": f"BURNRATE/{__version__}",
        }
        owns_client = self._client is None
        client = self._client or httpx.Client(
            timeout=self._timeout,
            trust_env=False,
            follow_redirects=False,
        )
        try:
            response = client.post(RESEND_API_URL, json=payload, headers=headers)
            if response.status_code not in {200, 201, 202}:
                raise EmailDeliveryError(f"Email provider returned HTTP {response.status_code}.")
        except EmailDeliveryError:
            raise
        except Exception as exc:
            raise EmailDeliveryError(f"Email delivery failed ({type(exc).__name__}).") from None
        finally:
            if owns_client:
                client.close()


def default_email_sender() -> EmailSender:
    return ResendEmailSender()


def _wrap_html(title: str, body_html: str) -> str:
    safe_title = html.escape(title)
    return (
        "<!doctype html><html><body style=\"font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;"
        "background:#f8fafc;color:#0f172a;padding:24px;\">"
        "<div style=\"max-width:480px;margin:0 auto;background:#ffffff;border:1px solid #e2e8f0;"
        "border-radius:12px;padding:24px;\">"
        f"<h2 style=\"margin:0 0 12px;font-size:18px;\">{safe_title}</h2>"
        f"{body_html}"
        "<p style=\"margin:20px 0 0;font-size:12px;color:#64748b;\">myBurnRate Cloud — Usage metadata sync only; "
        "prompts and source code never leave your computer.</p></div></body></html>"
    )


def verification_email(to: str, code: str, *, minutes: int = 15) -> OutboundEmail:
    safe_code = html.escape(code)
    text = (
        f"Your myBurnRate verification code is: {code}\n\n"
        f"Enter this 6-digit code in the myBurnRate desktop app to activate your account.\n"
        f"This code expires in {minutes} minutes. If you did not request this, you can ignore this email."
    )
    body = (
        "<p style=\"margin:0 0 14px;color:#334155;\">Enter this 6-digit code in the myBurnRate desktop app to "
        "verify your email and finish creating your account:</p>"
        f"<div style=\"font-size:28px;font-weight:800;letter-spacing:6px;background:#f1f5f9;padding:14px 18px;"
        f"border-radius:8px;text-align:center;font-family:monospace;\">{safe_code}</div>"
        f"<p style=\"margin:14px 0 0;font-size:13px;color:#475569;\">Expires in {minutes} minutes. "
        "If you didn't request this, you can safely ignore this message.</p>"
    )
    return OutboundEmail(
        to=to,
        subject=f"{code} is your myBurnRate verification code",
        text=text,
        html=_wrap_html("Verify your myBurnRate email", body),
        tag="register_verify",
    )


def existing_account_signup_alert(to: str) -> OutboundEmail:
    text = (
        "Someone tried to create a myBurnRate Cloud account using your email address.\n\n"
        "You already have an account. Sign in from the myBurnRate desktop app, or use "
        "'Forgot password' if you need a reset code. If this wasn't you, no action is needed."
    )
    body = (
        "<p style=\"margin:0 0 12px;color:#334155;\">Someone attempted to sign up for myBurnRate Cloud with "
        "this email address. Your account already exists and was not changed.</p>"
        "<p style=\"margin:0;color:#475569;font-size:13px;\">Sign in from the desktop app, or request a "
        "6-digit reset code from 'Forgot password' if you need to recover access.</p>"
    )
    return OutboundEmail(
        to=to,
        subject="Sign-up attempt on your myBurnRate account",
        text=text,
        html=_wrap_html("Sign-up attempt noticed", body),
        tag="register_existing_alert",
    )


def password_reset_email(to: str, code: str, *, minutes: int = 15) -> OutboundEmail:
    safe_code = html.escape(code)
    text = (
        f"Your myBurnRate password reset code is: {code}\n\n"
        f"Enter this 6-digit code in the myBurnRate desktop app to choose a new password.\n"
        f"This code expires in {minutes} minutes. If you didn't request a reset, ignore this email."
    )
    body = (
        "<p style=\"margin:0 0 14px;color:#334155;\">Enter this 6-digit code in the myBurnRate desktop app "
        "to reset your password:</p>"
        f"<div style=\"font-size:28px;font-weight:800;letter-spacing:6px;background:#f1f5f9;padding:14px 18px;"
        f"border-radius:8px;text-align:center;font-family:monospace;\">{safe_code}</div>"
        f"<p style=\"margin:14px 0 0;font-size:13px;color:#475569;\">Expires in {minutes} minutes. "
        "All active sessions will be signed out when your password changes.</p>"
    )
    return OutboundEmail(
        to=to,
        subject=f"{code} is your myBurnRate password reset code",
        text=text,
        html=_wrap_html("Reset your myBurnRate password", body),
        tag="password_reset",
    )


def password_changed_alert(to: str, *, disconnected_devices: bool) -> OutboundEmail:
    extra = (
        "All paired computers were also disconnected and will need to sign in or pair again."
        if disconnected_devices
        else "All active owner sessions were signed out. Paired computers will continue syncing unless revoked."
    )
    text = (
        f"Your myBurnRate Cloud password was just changed.\n\n{extra}\n\n"
        "If you did not make this change, reset your password immediately and select "
        "'Also disconnect all paired computers'."
    )
    body = (
        "<p style=\"margin:0 0 12px;color:#334155;\">Your myBurnRate Cloud password was changed.</p>"
        f"<p style=\"margin:0 0 12px;color:#334155;\">{html.escape(extra)}</p>"
        "<p style=\"margin:0;color:#dc2626;font-size:13px;\">If you did not do this, reset your password "
        "immediately and check 'Also disconnect all paired computers'.</p>"
    )
    return OutboundEmail(
        to=to,
        subject="Your myBurnRate password was changed",
        text=text,
        html=_wrap_html("Password changed", body),
        tag="password_changed",
    )


def email_change_verification_email(to: str, code: str, *, minutes: int = 15) -> OutboundEmail:
    safe_code = html.escape(code)
    text = (
        f"Your myBurnRate email-change verification code is: {code}\n\n"
        f"Enter this 6-digit code in Settings to confirm moving your account to {to}.\n"
        f"This code expires in {minutes} minutes."
    )
    body = (
        f"<p style=\"margin:0 0 14px;color:#334155;\">Confirm moving your myBurnRate Cloud account to "
        f"<strong>{html.escape(to)}</strong> by entering this code in Settings:</p>"
        f"<div style=\"font-size:28px;font-weight:800;letter-spacing:6px;background:#f1f5f9;padding:14px 18px;"
        f"border-radius:8px;text-align:center;font-family:monospace;\">{safe_code}</div>"
        f"<p style=\"margin:14px 0 0;font-size:13px;color:#475569;\">Expires in {minutes} minutes.</p>"
    )
    return OutboundEmail(
        to=to,
        subject=f"{code} confirms your new myBurnRate email",
        text=text,
        html=_wrap_html("Confirm your new email", body),
        tag="email_change_verify",
    )


def email_changed_alert(to: str, *, new_email: str) -> OutboundEmail:
    text = (
        f"The email address on your myBurnRate Cloud account was updated to {new_email}.\n\n"
        "If you did not authorize this change, contact support immediately."
    )
    body = (
        f"<p style=\"margin:0 0 12px;color:#334155;\">Your myBurnRate Cloud email address was updated to "
        f"<strong>{html.escape(new_email)}</strong>.</p>"
        "<p style=\"margin:0;color:#475569;font-size:13px;\">If you did not make this change, contact support immediately.</p>"
    )
    return OutboundEmail(
        to=to,
        subject="Your myBurnRate email address was updated",
        text=text,
        html=_wrap_html("Email address updated", body),
        tag="email_changed",
    )


def account_deleted_receipt(to: str) -> OutboundEmail:
    text = (
        "Your myBurnRate Cloud account, synced usage database, sessions, and paired device tokens "
        "have been permanently deleted.\n\n"
        "Local usage history stored on your own computers was not modified."
    )
    body = (
        "<p style=\"margin:0 0 12px;color:#334155;\">Your myBurnRate Cloud account and its synced usage "
        "database, sessions, and paired device tokens have been permanently deleted.</p>"
        "<p style=\"margin:0;color:#475569;font-size:13px;\">Local databases on your own computers were not touched.</p>"
    )
    return OutboundEmail(
        to=to,
        subject="Your myBurnRate Cloud account was deleted",
        text=text,
        html=_wrap_html("Account deleted", body),
        tag="account_deleted",
    )
