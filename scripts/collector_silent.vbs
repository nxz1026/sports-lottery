' collector_silent.vbs - windowless launcher (wscript host, no console at all)
' Usage: wscript.exe ...collector_silent.vbs [offer|night]
'   offer (default)  = 7 fast topics (jczq_offer jclq_offer jczq_result jclq_result jc_issue jc_issue_result lottery_draw)
'   night            = 8 topics  (offer 7 + jc_odds_history)
' Launches python directly (no cmd, no powershell, no conhost window).
' All diagnostic output goes to logs\collector_<mode>.log via --mode-log.

Set args = WScript.Arguments
mode = "offer"
If args.Count >= 1 Then mode = args(0)

If mode = "night" Then
  topics = "jczq_offer jclq_offer jczq_result jclq_result jc_issue jc_issue_result lottery_draw jc_odds_history"
Else
  topics = "jczq_offer jclq_offer jczq_result jclq_result jc_issue jc_issue_result lottery_draw"
End If

Set oShell = CreateObject("WScript.Shell")
cmd = """C:\Python314\python.exe"" E:\2026Workplace\Code\collector-cn\collector.py --push-batch " & topics & " --mode-log " & mode
' Run(cmd, 0, True): 0 = SW_HIDE (no window), True = wait for python to finish
oShell.Run cmd, 0, True
WScript.Quit 0
