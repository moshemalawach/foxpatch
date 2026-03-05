"""Exception hierarchy for foxpatch."""


class AutoDevError(Exception):
    """Base exception for all foxpatch errors."""


class ConfigError(AutoDevError):
    """Configuration loading or validation error."""


class GitHubCLIError(AutoDevError):
    """Error invoking the gh CLI."""

    def __init__(self, message: str, returncode: int = -1, stderr: str = ""):
        super().__init__(message)
        self.returncode = returncode
        self.stderr = stderr


class ClaudeTimeoutError(AutoDevError):
    """Claude CLI process exceeded timeout."""


class ClaudeProcessError(AutoDevError):
    """Claude CLI process exited with an error."""

    def __init__(self, message: str, returncode: int = -1, stderr: str = ""):
        super().__init__(message)
        self.returncode = returncode
        self.stderr = stderr


class WorkspaceError(AutoDevError):
    """Error creating or managing workspaces."""
