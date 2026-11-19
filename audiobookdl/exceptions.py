from .logging import error, print_error_file
from .utils import read_asset_file


class _SafeFormatDict(dict):
    def __missing__(self, _key: str) -> str:
        return ""


class AudiobookDLException(Exception):
    error_description = "unknown"

    def __init__(self, error_description = None, message: str | None = None, **kwargs) -> None:
        if message:
            super().__init__(message)
        if error_description:
            self.error_description = error_description
        self.data = kwargs

    def __str__(self) -> str:
        base = super().__str__().strip()
        if not base:
            if self.error_description == "cancelled":
                base = "Download cancelled"
            else:
                try:
                    template = read_asset_file(f"assets/errors/{self.error_description}.txt")
                    rendered = template.format_map(_SafeFormatDict(self.data))
                    base = rendered.strip()
                except Exception:
                    base = self.error_description.replace("_", " ").capitalize()
        detail = str(self.data.get("detail") or "").strip()
        if detail and detail not in base:
            base = f"{base}\n\nDetails: {detail}" if base else detail
        return base

    def print(self) -> None:
        print_error_file(self.error_description, **self.data)
        detail = str(self.data.get("detail") or "").strip()
        if detail:
            error(detail)

class DataNotPresent(AudiobookDLException):
    error_description = "data_not_present"

class FailedCombining(AudiobookDLException):
    error_description = "failed_combining"

class MissingDependency(AudiobookDLException):
    error_description = "missing_dependency"

class NoFilesFound(AudiobookDLException):
    error_description = "no_files_found"

class NoSourceFound(AudiobookDLException):
    error_description = "no_source_found"

    def print(self):
        import audiobookdl.sources as sources

        source_name_list = "\n".join([f" • {name}" for name in sources.get_source_names()])
        print_error_file(self.error_description, sources=source_name_list, **self.data)

class RequestError(AudiobookDLException):
    error_description = "request_error"

class UserNotAuthorized(AudiobookDLException):
    error_description = "user_not_authorized"

class CloudflareBlocked(AudiobookDLException):
    error_description = "cloudflare_blocked"

class MissingBookAccess(AudiobookDLException):
    error_description = "book_access"

class BookNotFound(AudiobookDLException):
    error_description = "book_not_found"

class BookNotReleased(AudiobookDLException):
    error_description = "book_not_released"

class BookHasNoAudiobook(AudiobookDLException):
    error_description = "book_has_no_audiobook"

class BookHasNoEbook(AudiobookDLException):
    error_description = "book_has_no_ebook"

class ConfigNotFound(AudiobookDLException):
    error_description = "config_not_found"

class GenericAudiobookDLException(AudiobookDLException):
    error_description: str = "generic"

    def __init__(self, heading: str, body: str | None = None) -> None:
        super().__init__(message=f"{heading}: {body}" if body else heading)
        self.data = {'heading': heading, 'body': body if body else ""}

class DownloadError(AudiobookDLException):
    error_description: str = "download_error"


class DownloadCancelled(AudiobookDLException):
    error_description: str = "cancelled"

    def print(self) -> None:
        from .logging import log
        log("Download cancelled")
