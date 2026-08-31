; Установщик InstallerModels. Ставит для текущего пользователя, без прав администратора.
;
; Собирать через build.py. Вручную - makensis setup.nsi, но только после того,
; как build.py хоть раз положил рядом version.nsh: имя, версия и заголовок окна
; приходят оттуда, а туда - из констант в core.py. Раньше версия была записана
; и здесь, и в build.py, и в этой самой строке комментария, а заголовок окна -
; здесь и в gui.py. За тем, чтобы копии не разъехались, следили четыре отдельные
; проверки. Проверка вида «A совпадает с B» - это симптом: один и тот же факт
; записан дважды.

Unicode true

!include "version.nsh"   ; APP, VERSION, WINTITLE - создаётся build.py

!define PUBLISH "ALEXaloysha"
!define REGKEY  "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APP}"

!include "MUI2.nsh"
!include "FileFunc.nsh"

Name "${APP} ${VERSION}"
OutFile "dist\${APP}-Setup-${VERSION}.exe"
InstallDir "$LOCALAPPDATA\Programs\${APP}"
InstallDirRegKey HKCU "Software\${APP}" "InstallDir"
RequestExecutionLevel user
SetCompressor /SOLID lzma
ShowInstDetails show
ShowUnInstDetails show

VIProductVersion "${VERSION}.0"
VIAddVersionKey "ProductName" "${APP}"
VIAddVersionKey "FileDescription" "Установщик моделей для ComfyUI"
VIAddVersionKey "FileVersion" "${VERSION}"
VIAddVersionKey "LegalCopyright" "${PUBLISH}"

!define MUI_ICON "icon.ico"
!define MUI_UNICON "icon.ico"
!define MUI_ABORTWARNING
!define MUI_FINISHPAGE_RUN "$INSTDIR\${APP}.exe"
!define MUI_FINISHPAGE_RUN_TEXT "Запустить ${APP}"

!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_COMPONENTS
; Папку установки на этой странице можно поменять на любую - хоть на рабочий
; стол, хоть на папку с документами. Файлы легли бы туда поверх чужих, а
; деинсталлятор потом сносит папку установки целиком, вместе со всем, что
; человек в ней держал. Пускаем только в пустую папку или в свою же.
!define MUI_PAGE_CUSTOMFUNCTION_LEAVE CheckInstallDir
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH
!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES
!insertmacro MUI_LANGUAGE "Russian"

; Окно программы ищем по заголовку. Это надёжнее проверки блокировки файла:
; работает и в деинсталляторе, который запускается копией из %TEMP% и видит
; другой $INSTDIR. Сам заголовок (WINTITLE) приходит из version.nsh, то есть
; из той же строки в core.py, которую окно ставит себе при запуске.

!macro RunningCheck un
Function ${un}CheckNotRunning
  again:
    FindWindow $R0 "" "${WINTITLE}"
    IntCmp $R0 0 free
    IfSilent 0 ask
      Abort "${APP} запущен, закрой программу и повтори."
    ask:
    MessageBox MB_RETRYCANCEL|MB_ICONEXCLAMATION "${APP} сейчас запущен. Закрой окно программы и нажми «Повтор»." IDRETRY again
    Abort "${APP} запущен, операция отменена."
  free:
FunctionEnd
!macroend

!insertmacro RunningCheck ""
!insertmacro RunningCheck "un."

; Пусто ли в папке. FindFirst на несуществующей папке сразу отдаёт пустую
; строку, так что "такой ещё нет" считается пустой - это то, что нужно.
Function DirIsEmpty
  Exch $R0
  Push $R1
  Push $R2
  FindFirst $R1 $R2 "$R0\*.*"
  loop:
    StrCmp $R2 "" empty
    StrCmp $R2 "." next
    StrCmp $R2 ".." next
    FindClose $R1
    StrCpy $R0 "0"
    Goto done
  next:
    FindNext $R1 $R2
    Goto loop
  empty:
    FindClose $R1
    StrCpy $R0 "1"
  done:
  Pop $R2
  Pop $R1
  Exch $R0
FunctionEnd

Function CheckInstallDir
  ; Своя же папка от прошлой версии - это обычное обновление поверх.
  IfFileExists "$INSTDIR\${APP}.exe" ok
  Push $INSTDIR
  Call DirIsEmpty
  Pop $R0
  StrCmp $R0 "1" ok
  MessageBox MB_OK|MB_ICONSTOP "В папке$\n$\n$INSTDIR$\n$\nуже что-то лежит, и это не ${APP}.$\n$\nВыбери пустую или новую папку: при удалении программа сносит свою папку целиком, вместе со всем, что в ней окажется."
  Abort
  ok:
FunctionEnd

Section "Программа" SecMain
  SectionIn RO
  Call CheckNotRunning

  ; Ставим поверх старой версии. File перезапишет только одноимённые файлы,
  ; а в _internal у PyInstaller имена библиотек меняются от сборки к сборке:
  ; чужие остатки оттуда роняют запуск. Сносим папку целиком.
  RMDir /r "$INSTDIR\_internal"

  ; Маска "*", а не "*.*": в _internal у PyInstaller 612 файлов из 988 идут без
  ; расширения (данные Tcl, таблицы часовых поясов). Проверено - makensis кладёт
  ; их в установщик при обеих масках, байт в байт, так что это не исправление,
  ; а просто маска, по которой сразу видно, что берётся всё.
  SetOutPath "$INSTDIR"
  File /r "dist\app\${APP}\*"

  WriteRegStr HKCU "Software\${APP}" "InstallDir" "$INSTDIR"
  WriteUninstaller "$INSTDIR\Uninstall.exe"

  CreateDirectory "$SMPROGRAMS\${APP}"
  CreateShortcut "$SMPROGRAMS\${APP}\${APP}.lnk" "$INSTDIR\${APP}.exe"
  CreateShortcut "$SMPROGRAMS\${APP}\Удалить ${APP}.lnk" "$INSTDIR\Uninstall.exe"

  ${GetSize} "$INSTDIR" "/S=0K" $0 $1 $2
  WriteRegStr   HKCU "${REGKEY}" "DisplayName"     "${APP}"
  WriteRegStr   HKCU "${REGKEY}" "DisplayVersion"  "${VERSION}"
  WriteRegStr   HKCU "${REGKEY}" "Publisher"       "${PUBLISH}"
  WriteRegStr   HKCU "${REGKEY}" "DisplayIcon"     "$INSTDIR\${APP}.exe"
  WriteRegStr   HKCU "${REGKEY}" "InstallLocation" "$INSTDIR"
  WriteRegStr   HKCU "${REGKEY}" "UninstallString" "$\"$INSTDIR\Uninstall.exe$\""
  WriteRegDWORD HKCU "${REGKEY}" "EstimatedSize"   "$0"
  WriteRegDWORD HKCU "${REGKEY}" "NoModify"        1
  WriteRegDWORD HKCU "${REGKEY}" "NoRepair"        1
SectionEnd

Section "Ярлык на рабочем столе" SecDesktop
  CreateShortcut "$DESKTOP\${APP}.lnk" "$INSTDIR\${APP}.exe"
SectionEnd

LangString DESC_SecMain    ${LANG_RUSSIAN} "Сама программа и список моделей."
LangString DESC_SecDesktop ${LANG_RUSSIAN} "Положить ярлык на рабочий стол."

!insertmacro MUI_FUNCTION_DESCRIPTION_BEGIN
  !insertmacro MUI_DESCRIPTION_TEXT ${SecMain}    $(DESC_SecMain)
  !insertmacro MUI_DESCRIPTION_TEXT ${SecDesktop} $(DESC_SecDesktop)
!insertmacro MUI_FUNCTION_DESCRIPTION_END

Section "Uninstall"
  Call un.CheckNotRunning

  Delete "$DESKTOP\${APP}.lnk"
  Delete "$SMPROGRAMS\${APP}\${APP}.lnk"
  Delete "$SMPROGRAMS\${APP}\Удалить ${APP}.lnk"
  RMDir  "$SMPROGRAMS\${APP}"

  ; Сносим папку целиком, но только убедившись, что это действительно наша папка:
  ; $INSTDIR приходит из реестра, и RMDir /r по чужому пути - это уже не удаление
  ; программы. Перечислять файлы поимённо мало: набор их меняется от версии к
  ; версии, а RMDir без /r на непустой папке молча ничего не делает, и огрызок
  ; оставался лежать навсегда - вместе с записью «удалено» в «Программах».
  IfFileExists "$INSTDIR\${APP}.exe" 0 +2
    RMDir /r "$INSTDIR"

  ; Если exe кто-то унёс руками, ограничиваемся тем, что клали сами.
  RMDir /r "$INSTDIR\_internal"
  RMDir /r "$INSTDIR\docs"
  Delete "$INSTDIR\${APP}.exe"
  Delete "$INSTDIR\models.json"
  Delete "$INSTDIR\README.md"
  Delete "$INSTDIR\Uninstall.exe"
  RMDir "$INSTDIR"

  ; Запомненная папка ComfyUI лежит вне $INSTDIR и переживала удаление,
  ; а потом всплывала при следующей установке как чужая настройка.
  Delete "$LOCALAPPDATA\${APP}\settings.json"
  RMDir  "$LOCALAPPDATA\${APP}"

  DeleteRegKey HKCU "${REGKEY}"
  DeleteRegKey HKCU "Software\${APP}"
SectionEnd
