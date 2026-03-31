Set shell = CreateObject("WScript.Shell")
scriptPath = CreateObject("Scripting.FileSystemObject").GetParentFolderName(WScript.ScriptFullName)
shell.Run "powershell -ExecutionPolicy Bypass -File """ & scriptPath & "\clean_for_push.ps1""", 0, False
