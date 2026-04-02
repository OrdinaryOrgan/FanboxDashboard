Set shell = CreateObject("WScript.Shell")
scriptPath = CreateObject("Scripting.FileSystemObject").GetParentFolderName(WScript.ScriptFullName)
shell.Run "powershell -ExecutionPolicy Bypass -File """ & scriptPath & "\reset_local_env.ps1""", 0, False
