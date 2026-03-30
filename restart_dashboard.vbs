Option Explicit

Dim shell, root, command
Set shell = CreateObject("WScript.Shell")

root = CreateObject("Scripting.FileSystemObject").GetParentFolderName(WScript.ScriptFullName)
command = "powershell -NoProfile -ExecutionPolicy Bypass -File """ & root & "\restart_dashboard.ps1"""

shell.Run command, 0, False
