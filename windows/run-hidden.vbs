Option Explicit
' run-hidden.vbs —— 任务计划的静默启动器
'
' 为什么需要它：任务计划里直接执行 powershell.exe，即使带 -WindowStyle Hidden，
' Windows 也会先创建控制台窗口再隐藏，全屏游戏时会闪一下并把游戏顶到后台。
' wscript.exe 属于 GUI 子系统，本身没有控制台；WshShell.Run 的第二个参数
' 用 0 表示以隐藏窗口方式启动子进程，第三个参数 True 表示等待其结束并取回退出码，
' 这样任务计划仍能拿到正确的 Last Run Result，也不会并发堆积。
'
' 调用约定：
'   wscript.exe //B //Nologo run-hidden.vbs <工作目录> <可执行文件> [参数...]

Dim sh, i, cmd, cwd, code
Set sh = CreateObject("WScript.Shell")

If WScript.Arguments.Count < 2 Then
    WScript.Echo "usage: run-hidden.vbs <cwd> <exe> [args...]"
    WScript.Quit 2
End If

cwd = WScript.Arguments(0)
cmd = Quote(WScript.Arguments(1))
For i = 2 To WScript.Arguments.Count - 1
    cmd = cmd & " " & Quote(WScript.Arguments(i))
Next

If cwd <> "" Then
    sh.CurrentDirectory = cwd
End If

code = sh.Run(cmd, 0, True)
WScript.Quit code

Function Quote(s)
    Dim t
    t = CStr(s)
    If InStr(t, " ") > 0 Or InStr(t, "&") > 0 Or InStr(t, "(") > 0 Then
        Quote = """" & t & """"
    Else
        Quote = t
    End If
End Function
