"""Security flags shared by the otherwise distinct proxy and reviewer containers."""

BASELINE_FLAGS = (
    "--pull=never",
    "--read-only",
    "--cap-drop=ALL",
    "--security-opt=no-new-privileges",
)
