from typing import Any

from .utils import read_asset_file


class _SafeFormatDict(dict[str, Any]):
    def __missing__(self, _key: str) -> str:
        return ""


class AudiobookDLException(Exception):
    error_description = "generic"
    code: str = "generic"
    category: str = "internal"
    retryable: bool = False
    action_hint: str | None = None

    def __init__(self, error_description=None, message: str | None = None, **kwargs) -> None:
        if message:
            super().__init__(message)
        if error_description:
            self.error_description = error_description
        self.code = self.error_description
        self.data = kwargs

    def _template_data(self) -> dict[str, Any]:
        return dict(self.data)

    def to_error_info(self) -> dict[str, Any]:
        """Return a machine-readable error payload for GUI/API consumers."""
        return {
            "code": self.code,
            "category": self.category,
            "retryable": self.retryable,
            "action_hint": self.action_hint,
            "message": str(self),
            "data": dict(self.data),
        }

    def __str__(self) -> str:
        base = super().__str__().strip()
        if not base:
            if self.error_description == "cancelled":
                base = "Download cancelled"
            else:
                try:
                    template = read_asset_file(f"assets/errors/{self.error_description}.txt")
                    rendered = template.format_map(_SafeFormatDict(self._template_data()))
                    base = rendered.strip()
                except Exception:
                    base = ""
                if not base:
                    base = self.error_description.replace("_", " ").capitalize()
        detail = str(self.data.get("detail") or "").strip()
        if detail and detail not in base:
            base = f"{base}\n\nDetails: {detail}" if base else detail
        return base

class DataNotPresent(AudiobookDLException):
    error_description = "data_not_present"
    category = "source"

class FailedCombining(AudiobookDLException):
    error_description = "failed_combining"
    category = "processing"
    retryable = True

class MissingDependency(AudiobookDLException):
    error_description = "missing_dependency"
    category = "environment"
    action_hint = "Install the missing dependency and try again."

class NoFilesFound(AudiobookDLException):
    error_description = "no_files_found"
    category = "source"

class NoSourceFound(AudiobookDLException):
    error_description = "no_source_found"
    category = "usage"
    action_hint = "Check the URL or pick a supported source."

    def _template_data(self) -> dict[str, Any]:
        data = dict(self.data)
        if "sources" not in data:
            import audiobookdl.sources as sources

            data["sources"] = "\n".join(
                f" • {name}" for name in sources.get_source_names()
            )
        return data

class RequestError(AudiobookDLException):
    error_description = "request_error"
    category = "network"
    retryable = True

class UserNotAuthorized(AudiobookDLException):
    error_description = "user_not_authorized"
    category = "auth"
    action_hint = "Check your credentials or sign in again."

class CloudflareBlocked(AudiobookDLException):
    error_description = "cloudflare_blocked"
    category = "network"
    action_hint = "Try again later or use browser cookies for this source."

class MissingBookAccess(AudiobookDLException):
    error_description = "book_access"
    category = "auth"
    action_hint = "Verify your account has access to this book."

class BookNotFound(AudiobookDLException):
    error_description = "book_not_found"
    category = "source"

class BookNotReleased(AudiobookDLException):
    error_description = "book_not_released"
    category = "source"

class BookHasNoAudiobook(AudiobookDLException):
    error_description = "book_has_no_audiobook"
    category = "source"

class BookHasNoEbook(AudiobookDLException):
    error_description = "book_has_no_ebook"
    category = "source"

class ConfigNotFound(AudiobookDLException):
    error_description = "config_not_found"
    category = "usage"

class GenericAudiobookDLException(AudiobookDLException):
    error_description: str = "generic"

    def __init__(self, heading: str, body: str | None = None) -> None:
        super().__init__(message=f"{heading}: {body}" if body else heading)
        self.data = {'heading': heading, 'body': body if body else ""}

class DownloadError(AudiobookDLException):
    error_description: str = "download_error"
    category = "network"
    retryable = True


class DownloadCancelled(AudiobookDLException):
    error_description: str = "cancelled"
    category = "cancelled"
