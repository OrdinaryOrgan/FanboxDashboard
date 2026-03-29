Option Explicit

Dim shell, fso, root, backendDir, appUrl, healthUrl
Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

root = fso.GetParentFolderName(WScript.ScriptFullName)
backendDir = root & "\backend"
appUrl = "http://127.0.0.1:8000/"
healthUrl = "http://127.0.0.1:8000/health"

If Not IsBackendReady(healthUrl) Then
    Dim pythonCommand
    pythonCommand = ResolvePythonCommand(shell)

    If pythonCommand = "" Then
        shell.Popup "Python not found in PATH.", 5, "Fanbox Dashboard", 16
        WScript.Quit 1
    End If

    Dim launchCommand
    launchCommand = "%ComSpec% /c cd /d """ & backendDir & """ && " & pythonCommand & _
        " -m uvicorn app.main:app --host 127.0.0.1 --port 8000"

    shell.Run launchCommand, 0, False

    If Not WaitForBackend(healthUrl, 40, 500) Then
        shell.Popup "Backend failed to start within the expected time.", 5, "Fanbox Dashboard", 16
        WScript.Quit 1
    End If
End If

shell.Run appUrl, 0, False

Function ResolvePythonCommand(byVal shellObject)
    Dim pythonPath
    pythonPath = FirstWhereResult(shellObject, "python")
    If pythonPath <> "" Then
        ResolvePythonCommand = """" & pythonPath & """"
        Exit Function
    End If

    pythonPath = FirstWhereResult(shellObject, "py")
    If pythonPath <> "" Then
        ResolvePythonCommand = """" & pythonPath & """ -3"
        Exit Function
    End If

    ResolvePythonCommand = ""
End Function

Function FirstWhereResult(byVal shellObject, byVal commandName)
    On Error Resume Next

    Dim exec, output, lines
    Set exec = shellObject.Exec("%ComSpec% /c where " & commandName)

    If Err.Number <> 0 Then
        Err.Clear
        FirstWhereResult = ""
        On Error GoTo 0
        Exit Function
    End If

    Do While exec.Status = 0
        WScript.Sleep 50
    Loop

    output = Trim(exec.StdOut.ReadAll)
    If output = "" Then
        FirstWhereResult = ""
    Else
        lines = Split(output, vbCrLf)
        FirstWhereResult = Trim(lines(0))
    End If

    On Error GoTo 0
End Function

Function IsBackendReady(byVal url)
    On Error Resume Next

    Dim request
    Set request = CreateObject("MSXML2.XMLHTTP")
    request.Open "GET", url, False
    request.Send

    IsBackendReady = (Err.Number = 0 And request.Status = 200)

    Err.Clear
    On Error GoTo 0
End Function

Function WaitForBackend(byVal url, byVal attempts, byVal delayMs)
    Dim i
    For i = 1 To attempts
        If IsBackendReady(url) Then
            WaitForBackend = True
            Exit Function
        End If
        WScript.Sleep delayMs
    Next

    WaitForBackend = False
End Function
