Option Explicit

Dim shell, fso, root, backendDir, frontendDir, frontendDist, appUrl, healthUrl
Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

root = fso.GetParentFolderName(WScript.ScriptFullName)
backendDir = root & "\backend"
frontendDir = root & "\frontend"
frontendDist = frontendDir & "\dist\index.html"
appUrl = "http://127.0.0.1:8000/"
healthUrl = "http://127.0.0.1:8000/health"

If Not EnsureFrontendBuild(shell, fso, frontendDir, frontendDist) Then
    WScript.Quit 1
End If

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

Function EnsureFrontendBuild(byVal shellObject, byVal filesystemObject, byVal frontendPath, byVal distPath)
    If filesystemObject.FileExists(distPath) Then
        EnsureFrontendBuild = True
        Exit Function
    End If

    Dim npmCommand
    npmCommand = ResolveNpmCommand(shellObject)
    If npmCommand = "" Then
        shell.Popup "npm not found in PATH, so the frontend build could not be created.", 5, "Fanbox Dashboard", 16
        EnsureFrontendBuild = False
        Exit Function
    End If

    Dim buildCommand
    buildCommand = "%ComSpec% /c cd /d """ & frontendPath & """ && "

    If Not filesystemObject.FolderExists(frontendPath & "\node_modules") Then
        buildCommand = buildCommand & npmCommand & " install && "
    End If

    buildCommand = buildCommand & npmCommand & " run build"

    Dim exitCode
    exitCode = shellObject.Run(buildCommand, 0, True)
    If exitCode <> 0 Then
        shell.Popup "Frontend preparation failed. Please run npm install and npm run build manually.", 5, "Fanbox Dashboard", 16
        EnsureFrontendBuild = False
        Exit Function
    End If

    EnsureFrontendBuild = filesystemObject.FileExists(distPath)
End Function

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

Function ResolveNpmCommand(byVal shellObject)
    Dim npmPath
    npmPath = FirstWhereResult(shellObject, "npm.cmd")
    If npmPath <> "" Then
        ResolveNpmCommand = """" & npmPath & """"
        Exit Function
    End If

    npmPath = FirstWhereResult(shellObject, "npm")
    If npmPath <> "" Then
        ResolveNpmCommand = """" & npmPath & """"
        Exit Function
    End If

    ResolveNpmCommand = ""
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
