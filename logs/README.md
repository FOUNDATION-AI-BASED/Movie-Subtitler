# Logs Folder

This folder contains logs and job state information for each video processing task. Each job has a `.json` file storing its status and metadata, and a `.log` file containing the output from the `auto-subtitle` command.

## OS Compatibility
This project is designed to be compatible with Unix-like operating systems, including macOS and Linux. While the core Python application might run on Windows, the `manage.sh` script and `ffmpeg` dependencies are primarily tested and supported on macOS and Linux environments.
